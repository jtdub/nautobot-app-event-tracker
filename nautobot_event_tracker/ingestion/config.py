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
    "kafka": {
        "bootstrap_servers": [],
        "group_id": "nautobot-event-tracker",
        "external_integration": "",
    },
    "redis": {
        "url": "",
        "external_integration": "",
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
    kafka: dict = field(default_factory=dict)
    redis: dict = field(default_factory=dict)
    topics: dict = field(default_factory=dict)

    @property
    def topic_names(self):
        """The topics to subscribe to."""
        return tuple(self.topics)


def get_settings():
    """Return the raw `ingestion` block from `PLUGINS_CONFIG`, with defaults filled in."""
    app_config = settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {})
    configured = app_config.get("ingestion") or {}
    merged = {**DEFAULTS, **configured}
    for nested in ("kafka", "redis"):
        merged[nested] = {**DEFAULTS[nested], **(configured.get(nested) or {})}
    return merged


def default_consumer_name():
    """Name this process the way an operator reading the stats page would want it named."""
    return f"{socket.gethostname()}:{os.getpid()}"


def load(*, topics=None):
    """Parse and validate the configuration, or raise `ImproperlyConfigured` listing every fault.

    `topics` restricts the result to a subset, for `--topics`. Naming a topic that is not
    configured is itself a fault: quietly consuming nothing is the worst way to answer a typo.
    """
    raw = get_settings()
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

    for key in (
        "max_payload_bytes",
        "event_type_cache_seconds",
        "max_retries",
        "stats_bucket_seconds",
        "stats_flush_seconds",
        "stats_retention_days",
    ):
        if not isinstance(raw.get(key), int) or isinstance(raw.get(key), bool) or raw[key] < 1:
            problems.append(f"'{key}' must be a positive integer, got {raw.get(key)!r}")

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
        kafka=dict(raw["kafka"]),
        redis=dict(raw["redis"]),
        topics=parsed_topics,
    )


def database_problems(config):
    """Return the faults that only a query can find: event types the catalogue does not hold."""
    from nautobot_event_tracker.models import EventType  # pylint: disable=import-outside-toplevel

    wanted = {
        topic.defaults["event_type"]: name for name, topic in config.topics.items() if topic.defaults.get("event_type")
    }
    if not wanted:
        return []
    known = set(EventType.objects.filter(name__in=wanted).values_list("name", flat=True))
    return [
        f"topic '{topic}': default event type '{event_type}' does not exist"
        for event_type, topic in sorted(wanted.items())
        if event_type not in known
    ]


def _parse_topic(name, topic_settings):
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
    )
    return topic, problems


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


def _parse_rate_limit(topic_name, rate_settings):
    """Parse the token bucket, if there is one."""
    if not rate_settings:
        return None, []

    problems = []
    per_minute = rate_settings.get("per_minute")
    burst = rate_settings.get("burst", per_minute)
    for label, value in (("per_minute", per_minute), ("burst", burst)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"topic '{topic_name}': rate_limit {label} must be a positive integer, got {value!r}")

    if problems:
        return None, problems
    return RateLimit(per_minute=per_minute, burst=burst), []


def render_problems(problems):
    """One message listing everything wrong, so a restart fixes all of it rather than the first."""
    lines = "\n".join(f"  - {problem}" for problem in problems)
    return f"Event Tracker ingestion configuration is invalid:\n{lines}"
