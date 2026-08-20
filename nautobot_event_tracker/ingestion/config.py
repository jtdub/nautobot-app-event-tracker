"""The ingestion configuration: reading it, and refusing it when it is wrong.

Configuration is validated before the consumer opens a socket, and every problem found is
reported, not just the first. A bad regex discovered at startup costs a restart; the same regex
discovered lazily, on the first message that would have matched it, costs an outage in a process
nobody is watching.

Validation comes in two halves because they need different things. `load()` parses and checks
everything answerable from the settings alone. `database_problems()` checks what needs a query.
The command runs both; a test of the first needs no database.
"""

import os
import re
import socket
from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from nautobot.apps.utils import deepmerge

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.ingestion.constants import (
    RULE_ACTIONS,
    UNKNOWN_EVENT_TYPE_DEFAULT,
    UNKNOWN_EVENT_TYPE_POLICIES,
)

#: Defaults for the `ingestion` block. Nautobot merges `PLUGINS_CONFIG` over an app's
#: `default_settings` one top-level key at a time, so a deployment that sets `ingestion` at all
#: replaces this whole dict. Defaults are therefore applied per key here rather than left to that
#: merge, which would silently drop every key the deployment did not restate.
DEFAULTS = {
    "consumer": "redis",
    "consumer_name": "",
    "max_payload_bytes": 65536,
    "event_type_cache_seconds": 60,
    "max_retries": 5,
    "poll_timeout_seconds": 1.0,
    "stats_bucket_seconds": 300,
    "stats_flush_seconds": 10,
    "stats_retention_days": 30,
    # The enrichment resolver's cache (Phase 4A, rule E8). Bounded by entries as well as by age
    # because its keys come out of the payload, which whoever emits the events controls.
    "resolve_cache_seconds": 300,
    "resolve_cache_entries": 2000,
    "kafka": {
        "bootstrap_servers": [],
        "group_id": "nautobot-event-tracker",
        "external_integration": "",
        # Only consulted when the external integration supplies a username and password. A broker
        # reached over the public internet wants SASL_SSL; SASL_PLAINTEXT suits a private network.
        "security_protocol": "SASL_PLAINTEXT",
        "sasl_mechanism": "PLAIN",
    },
    "redis": {
        "url": "",
        "external_integration": "",
    },
    # LLM triage (Phase 3, spec section 3). Off by default: an app installed before anyone has
    # registered a provider must not try to call one.
    "triage": {
        "enabled": False,
        "provider": "",
        "model": "",
        "timeout_seconds": 15,
        "max_output_tokens": 256,
        "max_context_chars": 4000,
        "attach_candidates": 5,
        "model_cache_seconds": 60,
    },
    "topics": {},
}

#: Per-topic defaults, applied the same way and for the same reason.
TOPIC_DEFAULTS = {
    "field_map": {},
    "defaults": {},
    "severity_map": {},
    "dedup_key_template": "",
    "minimum_severity": "",
    "unknown_event_type": UNKNOWN_EVENT_TYPE_DEFAULT,
    "rate_limit": {},
    "rules": [],
    # The enrichment rules (Phase 4A, section 3.1). An empty list means the resolver has nothing
    # to do for this topic rather than that it is switched off, so the pipeline runs no query.
    "resolve": [],
    # Whether this topic's survivors go to LLM triage, when triage is enabled at all. On by
    # default so enabling triage means enabling it, and opting a sensitive topic out is explicit.
    "triage": True,
}

#: Field map keys a topic must provide. Everything else has a sensible fallback; these two do not,
#: because a ticket with no type and no title is not a ticket anyone can act on.
REQUIRED_FIELD_MAP_KEYS = ("event_type", "title")


@dataclass(frozen=True)
class Rule:
    """One match rule: when every clause matches, the action applies."""

    name: str
    action: str
    when: tuple  # ((path, compiled regex), ...)


@dataclass(frozen=True)
class RateLimit:
    """A token bucket's shape. Absent means no limit."""

    per_minute: int
    burst: int


@dataclass(frozen=True)
class TriageConfig:  # pylint: disable=too-many-instance-attributes
    """The LLM triage block, parsed and checked (spec section 3)."""

    enabled: bool
    provider: str
    model: str
    timeout_seconds: float
    max_output_tokens: int
    max_context_chars: int
    attach_candidates: int
    model_cache_seconds: int


#: What "triage is off" is, so that it is one value rather than a value and a `None`. Every reader
#: then asks `config.triage.enabled` and no reader has to defend against a state `load()` cannot
#: produce.
TRIAGE_OFF = TriageConfig(**DEFAULTS["triage"])


@dataclass(frozen=True)
class TopicConfig:  # pylint: disable=too-many-instance-attributes
    """Everything the pipeline needs to know about one topic."""

    name: str
    field_map: dict
    defaults: dict
    severity_map: dict
    dedup_key_template: str
    minimum_severity: str
    unknown_event_type: str
    rules: tuple = ()
    rate_limit: RateLimit = None
    resolve: tuple = ()
    triage: bool = True


@dataclass(frozen=True)
class IngestionConfig:  # pylint: disable=too-many-instance-attributes
    """The whole ingestion block, parsed and checked."""

    consumer: str
    consumer_name: str
    max_payload_bytes: int
    event_type_cache_seconds: int
    max_retries: int
    poll_timeout_seconds: float
    stats_bucket_seconds: int
    stats_flush_seconds: int
    stats_retention_days: int
    resolve_cache_seconds: int
    resolve_cache_entries: int
    kafka: dict = field(default_factory=dict)
    redis: dict = field(default_factory=dict)
    triage: TriageConfig = TRIAGE_OFF
    topics: dict = field(default_factory=dict)

    @property
    def topic_names(self):
        """The topics to subscribe to."""
        return tuple(self.topics)

    @property
    def consumer_settings(self):
        """The broker block belonging to the configured consumer - `kafka` or `redis`.

        Which block that is, is the consumer class's own answer (`settings_key`), so a third broker
        needs nothing here.
        """
        from nautobot_event_tracker.ingestion.consumers import CONSUMERS  # pylint: disable=import-outside-toplevel

        return getattr(self, CONSUMERS[self.consumer].settings_key, {})


def _positive_int_problem(label, value):
    """The faults this value has as a positive integer: one, or none at all.

    A list rather than an optional string so that every caller reads `problems += ...`, matching
    how the rest of this module accumulates. The `bool` clause is the half that is easy to leave
    out and hard to notice missing: in Python `True` is an `int`, so without it
    `attach_candidates: True` validates as 1. Written once so every block checks the same thing
    and words the fault the same way.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return [f"{label} must be a positive integer, got {value!r}"]
    return []


def _positive_number_problem(label, value):
    """The faults this value has as a positive number: one, or none at all."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return [f"{label} must be a positive number, got {value!r}"]
    return []


def get_settings():
    """Return the raw `ingestion` block from `PLUGINS_CONFIG`, with defaults filled in.

    Merged with Nautobot's own `deepmerge`, so a deployment that sets one key of the Kafka block
    keeps the defaults for the rest. Naming the nested blocks here instead would mean a third
    broker block silently losing its defaults the day someone forgot to add it to the list.
    """
    app_config = settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {})
    return deepmerge(DEFAULTS, app_config.get("ingestion") or {})


def default_consumer_name():
    """Name this process the way an operator reading the stats page would want it named."""
    return f"{socket.gethostname()}:{os.getpid()}"


def load(*, topics=None, consumer=None, require_topics=False):
    """Parse and validate the configuration, or raise `ImproperlyConfigured` listing every fault.

    `topics` and `consumer` are the command line's overrides, checked here with everything else so
    that one restart answers every fault rather than the first of them. Naming a topic that is not
    configured is itself a fault: quietly consuming nothing is the worst way to answer a typo.

    `require_topics` is what the consumer process passes: an app installed and left unconfigured is
    a perfectly good state to describe, and a poor one to start a consumer in.
    """
    from nautobot_event_tracker.ingestion.consumers import CONSUMERS  # pylint: disable=import-outside-toplevel

    raw = get_settings()
    if consumer is not None:
        raw = {**raw, "consumer": consumer}
    problems = []
    parsed_topics = {}

    for name, topic_settings in sorted((raw.get("topics") or {}).items()):
        topic, topic_problems = _parse_topic(name, topic_settings)
        problems.extend(topic_problems)
        if topic is not None:
            parsed_topics[name] = topic

    if topics is not None:
        unknown = [name for name in topics if name not in (raw.get("topics") or {})]
        problems.extend(f"topic '{name}' is not configured" for name in unknown)
        parsed_topics = {name: topic for name, topic in parsed_topics.items() if name in topics}

    if raw["consumer"] not in CONSUMERS:
        problems.append(f"consumer '{raw['consumer']}' is unknown. Available: {', '.join(sorted(CONSUMERS))}")

    if require_topics and not parsed_topics and not problems:
        # Not stacked with the others: when a topic failed to parse, "no topics" is a consequence
        # of that fault rather than a fault of its own, and reporting both would mislead.
        problems.append(
            "no topics are configured, so there is nothing to consume. "
            "Set PLUGINS_CONFIG['nautobot_event_tracker']['ingestion']['topics']."
        )

    for key in (
        "max_payload_bytes",
        "event_type_cache_seconds",
        "max_retries",
        "stats_bucket_seconds",
        "stats_flush_seconds",
        "stats_retention_days",
        "resolve_cache_seconds",
        "resolve_cache_entries",
    ):
        problems += _positive_int_problem(f"'{key}'", raw.get(key))

    problems += _positive_number_problem("'poll_timeout_seconds'", raw.get("poll_timeout_seconds"))

    triage, triage_problems = _parse_triage(raw.get("triage") or {})
    problems.extend(triage_problems)

    if problems:
        raise ImproperlyConfigured(render_problems(problems))

    return IngestionConfig(
        consumer=str(raw["consumer"]),
        consumer_name=str(raw["consumer_name"]) or default_consumer_name(),
        max_payload_bytes=raw["max_payload_bytes"],
        event_type_cache_seconds=raw["event_type_cache_seconds"],
        max_retries=raw["max_retries"],
        poll_timeout_seconds=float(raw["poll_timeout_seconds"]),
        stats_bucket_seconds=raw["stats_bucket_seconds"],
        stats_flush_seconds=raw["stats_flush_seconds"],
        stats_retention_days=raw["stats_retention_days"],
        resolve_cache_seconds=raw["resolve_cache_seconds"],
        resolve_cache_entries=raw["resolve_cache_entries"],
        kafka=dict(raw["kafka"]),
        redis=dict(raw["redis"]),
        triage=triage,
        topics=parsed_topics,
    )


def database_problems(config, *, check_triage=True):
    """Return the faults that only a query can find: missing event types, and the triage model.

    Also the `llm` settings the triage path will read, which are not this module's block but are
    this process's problem: they are checked here because this is where every other fault the
    consumer can start with is reported, in one pass, before the first message.

    `check_triage` is false for a dry run. `ConsumerRunner._build_triage` returns None for every
    dry run (T9), so a dry run never consults a model - and refusing to start one because the
    optional `llm` extra is absent would deny an operator the decide-only pass over live traffic
    that dry runs exist for. The event-type half still runs: a dry run reports decisions, and a
    decision naming a type that does not exist is a fault worth reading before the real run.
    """
    from nautobot_event_tracker.models import EventType  # pylint: disable=import-outside-toplevel

    problems = []

    # A list of pairs rather than a map keyed on the type: two topics naming the same missing type
    # are two faults, and this module's whole contract is that every fault is reported.
    wanted = [
        (name, topic.defaults["event_type"])
        for name, topic in config.topics.items()
        if topic.defaults.get("event_type")
    ]
    if wanted:
        known = set(
            EventType.objects.filter(name__in={event_type for _, event_type in wanted}).values_list("name", flat=True)
        )
        problems.extend(
            f"topic '{topic}': default event type '{event_type}' does not exist"
            for topic, event_type in sorted(wanted)
            if event_type not in known
        )

    if check_triage and config.triage.enabled:
        # Through the LLM service so "exists" and "enabled" are one definition (rule L8);
        # `services.llm` imports no litellm at module level, so neither does this check.
        from nautobot_event_tracker.services import llm as llm_service  # pylint: disable=import-outside-toplevel
        from nautobot_event_tracker.services.exceptions import (  # pylint: disable=import-outside-toplevel
            LLMConfigurationError,
        )

        try:
            llm_service.get_model(config.triage.provider, config.triage.model)
        except LLMConfigurationError as error:
            problems.append(f"triage: {error}")

        try:
            llm_service.require_client()
        except ImproperlyConfigured as error:
            # Not an LLMError, and so not something triage's fail-open would catch: without this
            # the consumer would start and then die on its first accepted event.
            problems.append(f"triage: {error}")

        try:
            llm_service.get_settings()
        except ImproperlyConfigured as error:
            # The `llm` block, not the `ingestion` one, and read here because this is the process
            # that will act on it: every triaged call prunes, and a retention window this module
            # never looks at would otherwise surface as one logged exception a day rather than as
            # a refusal to start. Checked only when triage is on, since nothing else calls a model.
            problems.append(str(error))

    return problems


def _parse_topic(name, topic_settings):  # pylint: disable=too-many-locals
    """Parse one topic, returning it and the problems found. Returns None when unusable."""
    problems = []
    if not isinstance(topic_settings, dict):
        return None, [f"topic '{name}': configuration must be a mapping"]

    merged = {**TOPIC_DEFAULTS, **topic_settings}
    field_map = merged["field_map"] or {}
    problems.extend(
        f"topic '{name}': field_map is missing '{key}'" for key in REQUIRED_FIELD_MAP_KEYS if not field_map.get(key)
    )

    severity_map = {str(key): str(value) for key, value in (merged["severity_map"] or {}).items()}
    problems.extend(
        f"topic '{name}': severity_map value '{value}' is not a severity"
        for value in sorted(set(severity_map.values()))
        if value not in SeverityChoices.values()
    )

    defaults = dict(merged["defaults"] or {})
    default_severity = defaults.get("severity")
    if default_severity and default_severity not in SeverityChoices.values():
        problems.append(f"topic '{name}': default severity '{default_severity}' is not a severity")

    floor = merged["minimum_severity"]
    if floor and floor not in SeverityChoices.values():
        problems.append(f"topic '{name}': minimum_severity '{floor}' is not a severity")

    policy = merged["unknown_event_type"]
    if policy not in UNKNOWN_EVENT_TYPE_POLICIES:
        problems.append(f"topic '{name}': unknown_event_type must be one of {', '.join(UNKNOWN_EVENT_TYPE_POLICIES)}")

    rules, rule_problems = _parse_rules(name, merged["rules"] or [])
    problems.extend(rule_problems)

    rate_limit, rate_problems = _parse_rate_limit(name, merged["rate_limit"] or {})
    problems.extend(rate_problems)

    resolve, resolve_problems = _parse_resolve(name, merged["resolve"] or [])
    problems.extend(resolve_problems)

    topic_triage = merged["triage"]
    if not isinstance(topic_triage, bool):
        problems.append(f"topic '{name}': 'triage' must be a boolean, got {topic_triage!r}")
        topic_triage = True

    topic = TopicConfig(
        name=name,
        field_map=dict(field_map),
        defaults=defaults,
        severity_map=severity_map,
        dedup_key_template=str(merged["dedup_key_template"]),
        minimum_severity=str(floor),
        unknown_event_type=str(policy),
        rules=rules,
        rate_limit=rate_limit,
        resolve=resolve,
        triage=topic_triage,
    )
    return topic, problems


def _parse_triage(triage_settings):
    """Parse the triage block, returning it and the problems found."""
    problems = []
    if not isinstance(triage_settings, dict):
        return None, ["'triage' must be a mapping"]

    merged = {**DEFAULTS["triage"], **triage_settings}
    enabled = merged["enabled"]
    if not isinstance(enabled, bool):
        problems.append(f"triage: 'enabled' must be a boolean, got {enabled!r}")
        enabled = False

    if enabled:
        for key in ("provider", "model"):
            if not merged.get(key):
                problems.append(f"triage: '{key}' is required when triage is enabled")

    for key in ("max_output_tokens", "max_context_chars", "attach_candidates", "model_cache_seconds"):
        problems += _positive_int_problem(f"triage: '{key}'", merged.get(key))

    problems += _positive_number_problem("triage: 'timeout_seconds'", merged.get("timeout_seconds"))

    if problems:
        return None, problems

    return (
        TriageConfig(
            enabled=enabled,
            provider=str(merged["provider"]),
            model=str(merged["model"]),
            timeout_seconds=float(merged["timeout_seconds"]),
            max_output_tokens=merged["max_output_tokens"],
            max_context_chars=merged["max_context_chars"],
            attach_candidates=merged["attach_candidates"],
            model_cache_seconds=merged["model_cache_seconds"],
        ),
        [],
    )


def _parse_rules(topic_name, rule_settings):
    """Compile the match rules, reporting every rule that will not compile."""
    problems = []
    rules = []
    seen = set()

    for index, entry in enumerate(rule_settings):
        label = f"topic '{topic_name}' rule {index}"
        if not isinstance(entry, dict):
            problems.append(f"{label}: must be a mapping")
            continue

        rule_name = entry.get("name")
        if not rule_name:
            problems.append(f"{label}: needs a name, which is the counter key its drops appear under")
        elif rule_name in seen:
            problems.append(f"{label}: name '{rule_name}' is already used in this topic")
        else:
            seen.add(rule_name)

        action = entry.get("action")
        if action not in RULE_ACTIONS:
            problems.append(f"{label}: action must be one of {', '.join(RULE_ACTIONS)}, got {action!r}")

        clauses = []
        when = entry.get("when") or {}
        if not when:
            problems.append(f"{label}: needs at least one 'when' clause, or it matches everything")
        for path, pattern in when.items():
            try:
                clauses.append((str(path), re.compile(str(pattern))))
            except re.error as error:
                problems.append(f"{label}: pattern for '{path}' does not compile: {error}")

        if rule_name and action in RULE_ACTIONS and clauses and len(clauses) == len(when):
            rules.append(Rule(name=str(rule_name), action=str(action), when=tuple(clauses)))

    return tuple(rules), problems


def _parse_resolve(topic_name, resolve_settings):
    """Parse the enrichment rules, reporting every rule that could never work.

    Everything here is answerable from the settings and the model registry, so all of it happens
    at startup and none of it needs a query. A rule naming a field that does not exist is the case
    that matters: left to run, it would match nothing forever and look exactly like an estate that
    has not been onboarded yet.

    A rule that fails to parse still contributes its name to `seen`, so a later rule scoping on it
    is not reported as a second fault - the same argument `load()` makes about "no topics are
    configured".
    """
    # Imported here rather than at module scope: the allowlist lives with the code that enforces
    # it, and this module is read by tests that have no database.
    from nautobot_event_tracker.services.tickets import (  # pylint: disable=import-outside-toplevel
        get_attachable_object_types,
    )

    problems = []
    rules = []
    seen = set()
    attachable = sorted(get_attachable_object_types())

    for index, entry in enumerate(resolve_settings):
        rule, rule_problems = _parse_resolve_rule(f"topic '{topic_name}' resolve rule {index}", entry, attachable, seen)
        problems.extend(rule_problems)
        if rule is not None:
            rules.append(rule)
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            seen.add(name)

    return tuple(rules), problems


def _parse_resolve_rule(label, entry, attachable, earlier_names):
    """Parse one enrichment rule, returning it and the problems found. None when unusable."""
    from nautobot_event_tracker.services.enrichment import ResolveRule  # pylint: disable=import-outside-toplevel

    if not isinstance(entry, dict):
        return None, [f"{label}: must be a mapping"]

    name = entry.get("name")
    path = entry.get("path")
    field_name = entry.get("field")
    model_label = str(entry.get("model") or "").lower()

    problems = []
    if not name:
        problems.append(f"{label}: needs a name, which is how a later rule scopes on it")
    elif name in earlier_names:
        problems.append(f"{label}: name '{name}' is already used in this topic")
    if not path:
        problems.append(f"{label}: needs a 'path' into the payload")
    if not field_name:
        problems.append(f"{label}: needs a 'field' to look the value up by")

    model, model_problems = _resolve_model(label, model_label, attachable)
    problems += model_problems
    if model is not None and field_name:
        problems += _field_problem(label, model, model_label, str(field_name))

    scope, scope_problems = _parse_scope(label, entry.get("scope"), model, model_label, earlier_names)
    problems += scope_problems

    if not (name and path and field_name and model is not None and not scope_problems):
        return None, problems

    return (
        ResolveRule(
            name=str(name),
            path=str(path),
            model=model_label,
            field=str(field_name),
            scope=scope,
        ),
        problems,
    )


def _resolve_model(label, model_label, attachable):
    """The model a rule names, and the faults naming it has.

    The allowlist is checked here rather than left to `attach_object()` because by then the call is
    inside the ticket write's transaction: a refused attachment would roll back the ticket, turning
    a misconfigured rule into lost events (E4).
    """
    from django.apps import apps  # pylint: disable=import-outside-toplevel

    if not model_label:
        return None, [f"{label}: needs a 'model' as an 'app_label.model' string"]
    if model_label not in attachable:
        return None, [
            f"{label}: objects of type '{model_label}' may not be attached to a ticket. "
            f"Permitted types: {', '.join(attachable) or 'none configured'}"
        ]
    try:
        return apps.get_model(model_label), []
    except (LookupError, ValueError) as error:
        return None, [f"{label}: model '{model_label}' does not exist ({error})"]


def _field_problem(label, model, model_label, field_name, *, kind="field"):
    """The faults this field has on this model: one, or none at all."""
    from django.core.exceptions import FieldDoesNotExist  # pylint: disable=import-outside-toplevel

    try:
        model._meta.get_field(field_name)  # pylint: disable=protected-access
    except FieldDoesNotExist:
        return [f"{label}: {kind} '{field_name}' is not a field on {model_label}"]
    return []


def _parse_scope(label, scope_settings, model, model_label, earlier_names):
    """Parse a rule's scope: which of its own fields to pin, and to which earlier rule's result.

    A rule may only scope on a rule defined before it. That is not a limitation worth lifting: it
    makes cycles impossible by construction rather than by detection, and reading the block top to
    bottom is reading the order it runs in.
    """
    if not scope_settings:
        return (), []
    if not isinstance(scope_settings, dict):
        return (), [f"{label}: 'scope' must be a mapping of field to the name of an earlier rule"]

    problems = []
    pairs = []
    for field_name, rule_name in scope_settings.items():
        if rule_name not in earlier_names:
            problems.append(
                f"{label}: scope '{field_name}' names '{rule_name}', which is not a rule defined earlier in this topic"
            )
        if model is not None:
            field_problems = _field_problem(label, model, model_label, str(field_name), kind="scope field")
            problems += field_problems
            if not field_problems and not model._meta.get_field(str(field_name)).is_relation:  # pylint: disable=protected-access
                problems.append(
                    f"{label}: scope field '{field_name}' is not a relation on {model_label}, "
                    "so there is nothing for an earlier rule's object to be"
                )
        pairs.append((str(field_name), str(rule_name)))

    return tuple(pairs), problems


def _parse_rate_limit(topic_name, rate_settings):
    """Parse the token bucket, if there is one."""
    if not rate_settings:
        return None, []

    problems = []
    per_minute = rate_settings.get("per_minute")
    burst = rate_settings.get("burst", per_minute)
    for label, value in (("per_minute", per_minute), ("burst", burst)):
        problems += _positive_int_problem(f"topic '{topic_name}': rate_limit {label}", value)

    if problems:
        return None, problems
    return RateLimit(per_minute=per_minute, burst=burst), []


def render_problems(problems):
    """One message listing everything wrong, so a restart fixes all of it rather than the first."""
    lines = "\n".join(f"  - {problem}" for problem in problems)
    return f"Event Tracker ingestion configuration is invalid:\n{lines}"
