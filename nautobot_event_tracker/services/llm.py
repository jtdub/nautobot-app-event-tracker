"""The LLM service layer: the sole caller of language models and sole writer of LLMUsageRecord.

Every model call in the app funnels through `complete()`, whatever it is for, so that provider
choice stays configuration (ADR 0006) and so that recording cannot be forgotten at a call site.

Rules implemented here, referenced by number from the Phase 3 spec:

* **L1** - every call writes exactly one LLMUsageRecord, success or failure, before the caller
  sees the result or the error.
* **L2** - litellm is imported only here, lazily; no module anywhere imports a provider SDK.
* **L3** - credentials resolve at call time from the provider's ExternalIntegration and its
  secrets group; nothing key-shaped lives in settings, on a model, or in a log line.
* **L4** - failures raise the LLMError family from `services.exceptions`, carrying the usage
  record; no litellm or provider exception escapes.
* **L5** - cost is computed here from the model's registered prices and the provider's reported
  token usage. A response with no usage block records zeros, never a guess.
* **L6** - every call passes a timeout to litellm.
* **L7** - usage records older than the retention window are pruned by the service itself, at
  most once per process per day.
* **L8** - a disabled provider or model is refused before any network traffic, with no usage
  record: rule L1 covers calls, and a refused call never left the process.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from nautobot.apps.choices import SecretsGroupSecretTypeChoices
from nautobot.apps.constants import CHARFIELD_MAX_LENGTH
from nautobot.apps.utils import deepmerge

from nautobot_event_tracker.choices import (
    LITELLM_PROVIDER_PREFIXES,
    PROVIDER_TYPES_REQUIRING_A_URL,
    LLMProviderTypeChoices,
)
from nautobot_event_tracker.models import LLMModel, LLMUsageRecord
from nautobot_event_tracker.secrets import read_secret
from nautobot_event_tracker.services.exceptions import LLMCallError, LLMConfigurationError, LLMResponseError

logger = logging.getLogger(__name__)

#: Defaults for the `llm` block, applied per key here rather than left to Nautobot's top-level
#: `PLUGINS_CONFIG` merge, for the reason `ingestion.config` documents.
DEFAULTS = {
    "usage_retention_days": 90,
}

#: Applied when the caller does not set a timeout. There is no unbounded wait in any request path
#: (rule L6).
DEFAULT_TIMEOUT_SECONDS = 30

#: Error text longer than this is truncated before it is recorded. The record exists to show that
#: and why a call failed, not to archive a stack trace.
ERROR_TEXT_CAP = 1000

#: Sent as the API key to an OpenAI-compatible endpoint whose integration configures no secret.
#: Deliberately not key-shaped: it is meant to be obvious in a log or a traceback that nobody
#: configured a credential, rather than to look like one that failed.
NO_CREDENTIAL_PLACEHOLDER = "not-required"

TOKENS_PER_MILLION = 1_000_000

#: The date (per process) on which `_maybe_prune` last ran, so retention costs one DELETE a day
#: rather than one per call. The same shape as `StatsRecorder._prune`'s bookkeeping.
_last_pruned_on = None  # pylint: disable=invalid-name

#: Consecutive failed prunes since the last success. A failure leaves the day unmarked so the next
#: call retries - right for a lock or a statement timeout, wrong for a missing privilege, which
#: will fail the same way every time and would otherwise cost a full DELETE attempt per model call
#: forever. After this many in a row the day is marked done and the retry waits for tomorrow.
_prune_failures = 0  # pylint: disable=invalid-name
MAX_PRUNE_FAILURES = 3


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked to call, read out of a provider-shaped response.

    `arguments` is a dictionary because the caller needs one; the provider sends a JSON string,
    and a string that will not parse is a failed response rather than a call anybody makes
    (section 8). `identifier` is the provider's own id for the call, which the next request has to
    echo back on the result, so it is carried rather than regenerated.
    """

    identifier: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    """What a successful call returns: the text, the tool calls, and the row that priced it.

    The numbers live on the record alone; the properties are conveniences, not copies, so a new
    accounting field is added in one place.
    """

    text: str
    record: LLMUsageRecord
    #: The tools the model asked for, in the order it asked. Empty for a plain completion, and
    #: empty for every call made without `tools`.
    tool_calls: tuple = ()

    @property
    def prompt_tokens(self):
        """Tokens the prompt cost, as recorded."""
        return self.record.prompt_tokens

    @property
    def completion_tokens(self):
        """Tokens the completion cost, as recorded."""
        return self.record.completion_tokens

    @property
    def cost(self):
        """The call's computed price, as recorded."""
        return self.record.cost

    @property
    def latency_ms(self):
        """How long the call took, as recorded."""
        return self.record.latency_ms

    @property
    def request_id(self):
        """The provider's response identifier, as recorded."""
        return self.record.request_id


def get_settings():
    """The `llm` settings block with defaults applied per key, refusing a value that cannot work.

    `usage_retention_days` is checked here rather than left to `app-config-schema.json`, which
    Nautobot does not enforce against `PLUGINS_CONFIG`. It is the one key where a wrong value is
    destructive rather than merely wrong: `_maybe_prune` would read 0 as "keep nothing" and empty
    the accounting table, including the record the call that triggered it had just written. The
    `bool` clause is the half that is easy to leave out - in Python `True` is an `int`, so without
    it `usage_retention_days: True` validates as one day. `ingestion.config` makes the same three
    checks for the same reason; they are restated rather than shared because a service must not
    depend on the ingestion package.
    """
    configured = django_settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {}).get("llm") or {}
    settings = deepmerge(DEFAULTS, configured)

    retention_days = settings["usage_retention_days"]
    if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days < 1:
        raise ImproperlyConfigured(
            f"nautobot_event_tracker: llm 'usage_retention_days' must be a positive integer, got {retention_days!r}"
        )
    return settings


def get_model(provider_name, model_name):
    """Resolve an enabled model on an enabled provider, or say exactly what is wrong (L8)."""
    try:
        model = LLMModel.objects.select_related("provider__external_integration__secrets_group").get(
            provider__name=provider_name, name=model_name
        )
    except ObjectDoesNotExist as error:
        raise LLMConfigurationError(f"No LLM model '{model_name}' exists on a provider '{provider_name}'.") from error
    _check_enabled(model)
    return model


def require_client():
    """Resolve the client now, so a missing `llm` extra is a startup fault rather than a crash loop.

    `complete()` resolves it per call and raises `ImproperlyConfigured` when litellm is absent -
    deliberately outside the `LLMError` family (section 5.2), which is the family triage fails open
    on. Nothing would catch it on the ingestion path, so a deployment that enabled triage without
    the extra would start cleanly and then die on its first accepted event. Startup validation
    calls this instead, and the operator reads one line before any of that happens.
    """
    _litellm_completion()


def complete(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    model,
    messages,
    purpose,
    ticket=None,
    max_tokens=None,
    timeout=None,
    response_format=None,
    tools=None,
    tool_choice=None,
    client=None,
):
    """One model call: refuse (L8), resolve credentials (L3), call (L6), record (L1), price (L5).

    `client` is the test seam: a callable `(model_string, messages, **kwargs)` returning a
    litellm-shaped response object. The default is litellm itself, resolved before anything is
    attempted so that a missing package is a plain `ImproperlyConfigured` - a deployment fault,
    not a failed call, and not on the record (section 5.2 of the spec).

    `tools` is a list of OpenAI-shaped tool definitions, which litellm translates per provider.
    The app builds them from `MCPTool.input_schema` and never from anything a model said (Phase 4B
    section 8). A call that asked for tools and one that did not are priced identically: L1 through
    L8 are untouched by this argument.

    Returns an `LLMResponse`. Raises `LLMConfigurationError` before any network traffic,
    `LLMCallError` when the call fails, and `LLMResponseError` when what came back is unusable -
    the latter two carrying the usage record already written for the attempt (L4).
    """
    _check_enabled(model)
    call = client if client is not None else _litellm_completion()

    # Filtered, not trusted. `LLMModel.clean()` already refuses everything outside the allowlist,
    # but a fixture, a migration or a direct ORM write never runs it, and one wrong key here sends
    # the call - and the credential with it - to an address the registry chose. The same tuple
    # backs both layers, so the two cannot drift apart.
    registry_parameters = model.default_parameters or {}
    call_kwargs = {key: value for key, value in registry_parameters.items() if key in LLMModel.ALLOWED_PARAMETERS}
    dropped = sorted(set(registry_parameters) - set(call_kwargs))
    if dropped:
        # `clean()` refuses these at save time, so a row carrying one arrived by fixture, migration
        # or direct ORM write. Dropping it silently is how an operator's parameter stops applying
        # with nothing to read; this is that missing line.
        logger.warning(
            "LLM model %s carries %s in default_parameters, which this app does not pass. Allowed: %s.",
            model,
            ", ".join(dropped),
            ", ".join(LLMModel.ALLOWED_PARAMETERS),
        )

    # L6 - a call always has a timeout, and it comes from the most specific place that named one:
    # this call, then the registry entry, then the integration, then the constant. `or` rather
    # than `is not None` on purpose: a stored `null` or `0` is not somebody asking for no limit,
    # it is a row that says nothing, and `setdefault` used to read it as an answer.
    call_kwargs["timeout"] = (
        timeout or call_kwargs.get("timeout") or _integration_timeout(model.provider) or DEFAULT_TIMEOUT_SECONDS
    )
    effective_max_tokens = max_tokens if max_tokens is not None else model.max_output_tokens
    if effective_max_tokens is not None:
        call_kwargs["max_tokens"] = effective_max_tokens
    if response_format is not None:
        call_kwargs["response_format"] = response_format
    if tools:
        call_kwargs["tools"] = tools
        if tool_choice is not None:
            call_kwargs["tool_choice"] = tool_choice

    # Resolved before the try, like the client above: a routing fault is a refusal, and inside
    # the block it would be recorded and re-raised as a failed call it never became.
    model_string = _model_string(model)

    # Splatted last and kept out of `call_kwargs`, so the endpoint and the key are decided here
    # and nowhere else (L3). A registry parameter colliding with one of them is a TypeError from
    # the call itself rather than a silent redirection - the allowlist above makes it unreachable.
    connection = _connection_kwargs(model.provider)

    started = time.monotonic()
    try:
        raw = call(model_string, messages, **call_kwargs, **connection)
    except Exception as error:  # pylint: disable=broad-except
        # L4 - whatever the client raised, the caller sees one family, and the failure is on the
        # record first (L1).
        record = _record_usage(
            model=model, ticket=ticket, purpose=purpose, latency_ms=_elapsed_ms(started), error=error
        )
        raise LLMCallError(f"LLM call to {model} failed: {error}", record=record) from error

    latency_ms = _elapsed_ms(started)
    prompt_tokens, completion_tokens = _token_usage(raw)
    text = _response_text(raw)
    tool_calls, tool_call_problem = _response_tool_calls(raw)

    # A model that asked for a tool sends no message content, and that is a complete answer rather
    # than an empty one. The "nothing came back" error therefore fires only when neither half is
    # there - and an unparsable set of arguments is its own fault, reported as itself, because
    # "the model returned no content" would send an operator looking in the wrong place.
    problem = tool_call_problem
    if problem is None and text is None and not tool_calls:
        problem = "The response carried no message content."

    record = _record_usage(
        model=model,
        ticket=ticket,
        purpose=purpose,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=_cost(model, prompt_tokens, completion_tokens),
        request_id=str(getattr(raw, "id", "") or ""),
        error=problem,
    )
    if problem is not None:
        raise LLMResponseError(f"LLM call to {model} returned no usable content: {problem}", record=record)

    return LLMResponse(text=text or "", record=record, tool_calls=tool_calls)


def link_usage_records(record_ids, ticket):
    """Point usage records at the ticket they turned out to be about.

    A triage call happens before its ticket exists, so its record is written with no ticket and
    linked here once the write has committed. This lives in the service so that the sole-writer
    rule (L1) stays literally true.
    """
    if not record_ids or ticket is None:
        return
    LLMUsageRecord.objects.filter(pk__in=list(record_ids)).update(ticket=ticket)


def _check_enabled(model):
    """L8 - the operator's off switch works everywhere at once, before any network traffic."""
    if not model.provider.enabled:
        raise LLMConfigurationError(f"LLM provider '{model.provider}' is disabled.")
    if not model.enabled:
        raise LLMConfigurationError(f"LLM model '{model}' is disabled.")


def _model_string(model):
    """The litellm model string for this registry entry. litellm routes on the prefix.

    An unmapped provider type is a configuration fault, not a crash: a bare KeyError would
    escape the LLMError family rule L4 promises, and triage's fail-open catches that family
    alone - so the consumer would die on the message rather than accept it.
    """
    prefix = LITELLM_PROVIDER_PREFIXES.get(model.provider.provider_type)
    if prefix is None:
        raise LLMConfigurationError(
            f"Provider type '{model.provider.provider_type}' has no litellm routing prefix. "
            "Add it to LITELLM_PROVIDER_PREFIXES beside the choice."
        )
    return f"{prefix}/{model.name}"


def _connection_kwargs(provider):
    """L3 - everything the ExternalIntegration says about this connection, read at call time.

    The endpoint, the key and the Headers. Reading `remote_url` raw was wrong twice over - Jinja2
    templating is supported on it, so a templated URL reached litellm as a literal `{{ ... }}`,
    and a templated endpoint went nowhere.

    Not the TLS fields: litellm takes no per-call TLS argument, and `_warn_about_unappliable_tls`
    explains what happens instead.

    A missing key is not an error here: an on-premises endpoint may not want one, and one that
    does will refuse the call itself, which the record then shows. Prefers the token secret type
    and falls back to the plain secret type, so either way an operator has modeled "the key"
    works. An OpenAI-compatible provider with no secret configured gets a placeholder rather than
    nothing, because litellm's OpenAI client refuses to make the call at all without one - see
    `NO_CREDENTIAL_PLACEHOLDER`. An Ollama provider needs no such workaround, since its litellm
    path builds no OpenAI client; a token still reaches it when one is configured, for an Ollama
    behind an authenticating proxy.

    `extra_config` is deliberately not passed. It is untyped operator JSON, and splatting it into
    the call would reopen, one door along, exactly the hole `ALLOWED_PARAMETERS` closed. If a
    provider ever needs something from it, it gets a named field here rather than a free splat.
    """
    integration = provider.external_integration
    kwargs = {}

    remote_url = _rendered(integration, "render_remote_url", provider)
    if remote_url:
        kwargs["api_base"] = remote_url
    elif provider.provider_type in PROVIDER_TYPES_REQUIRING_A_URL:
        # `clean()` demands this URL at save time, but a shared integration can be blanked
        # afterwards without revalidating the providers pointing at it. Refusing here matters
        # more than tidiness, and differently per type: with no api_base, litellm's `openai/`
        # prefix would send this deployment's key and its event payload to api.openai.com, and its
        # `ollama/` prefix would quietly try a loopback address that means nothing in a container.
        raise LLMConfigurationError(
            f"LLM provider '{provider}' is '{provider.get_provider_type_display()}' but its "
            "external integration has no remote URL."
        )

    for secret_type in (SecretsGroupSecretTypeChoices.TYPE_TOKEN, SecretsGroupSecretTypeChoices.TYPE_SECRET):
        key = read_secret(integration, secret_type)
        if key:
            kwargs["api_key"] = key
            break
    else:
        if integration.secrets_group_id is not None:
            # A group is configured and neither secret type resolved: either it holds no token or
            # secret at all, which is fine and common for a shared group, or the one it holds is
            # broken. Not raised, because the first case is legitimate and refusing it would break
            # working deployments - but said, because the second case otherwise ends as a silent
            # downgrade to no authentication against an endpoint that does not check.
            logger.warning(
                "LLM provider %s has secrets group '%s' but no token or secret resolved from it; "
                "calling without a credential.",
                provider,
                integration.secrets_group,
            )
        if provider.provider_type == LLMProviderTypeChoices.OPENAI_COMPATIBLE:
            # ADR 0006 makes an unauthenticated on-premises endpoint a first-class case, and this
            # is what it takes to actually be one: litellm builds an OpenAI client for the
            # `openai/` prefix, and that client raises "Missing credentials" locally, before any
            # request is made, when it has no key. So "no key" has to be spelled as a value.
            # Only for this provider type - a real OpenAI or Anthropic endpoint with no key
            # configured should fail with the provider's own message, on the record, rather than
            # with a placeholder this app invented.
            kwargs["api_key"] = NO_CREDENTIAL_PLACEHOLDER

    headers = _rendered(integration, "render_headers", provider)
    if headers:
        kwargs["extra_headers"] = headers

    _warn_about_unappliable_tls(provider, integration)

    return kwargs


def _warn_about_unappliable_tls(provider, integration):
    """Say so when an integration asks for TLS settings litellm cannot be given per call.

    This used to pass `ssl_verify` as a call keyword, which litellm does not accept: it swept the
    key into `extra_body` and sent it to the provider in the request JSON. So unticking *Verify
    SSL* did not disable verification, a *CA File Path* was never loaded, and an unexpected field
    went out with every request. The setting looked applied and was not.

    litellm resolves TLS from the `SSL_VERIFY` and `SSL_CERT_FILE` environment variables or from
    its own `litellm.ssl_verify` module global, and from nothing per call. Writing that global
    around each call is the obvious repair and the wrong one: a Celery worker runs calls for
    several providers at once in threads, so one provider with verification off would switch it
    off for another provider's call in flight. Quietly not applying a setting is bad; silently
    disabling verification on somebody else's connection is worse.

    So the app applies neither and says which ones it skipped. `services/mcp.py` has no such
    problem and does honour both: it builds the HTTP client itself, per call.
    """
    unappliable = []
    if not integration.verify_ssl:
        unappliable.append("Verify SSL (unticked)")
    if integration.ca_file_path:
        unappliable.append(f"CA File Path ({integration.ca_file_path})")
    if not unappliable:
        return

    logger.warning(
        "External integration '%s' for LLM provider %s sets %s, which litellm cannot be given per "
        "call - it reads TLS settings process-wide. They are NOT being applied. Set SSL_VERIFY or "
        "SSL_CERT_FILE in the environment of every process that calls a model instead.",
        integration,
        provider,
        " and ".join(unappliable),
    )


def _integration_timeout(provider):
    """The integration's Timeout, in seconds, or None when it has nothing useful to say.

    Nautobot defaults the field to 30, which is also this app's own default, so an operator who
    has never touched it sees no change and one who has raised it for a slow on-premises model
    gets what they asked for.
    """
    timeout = getattr(provider.external_integration, "timeout", None)
    return timeout if isinstance(timeout, (int, float)) and timeout > 0 else None


def _rendered(integration, method_name, provider):
    """One of the integration's Jinja2-templated fields, rendered rather than read raw.

    The template context is the provider, under `obj`, matching what Nautobot's own callers pass.
    A broken template is a configuration fault and is raised as one: rendering it to something
    half-formed would point the call at an address nobody chose.
    """
    try:
        return getattr(integration, method_name)({"obj": provider})
    except Exception as error:  # pylint: disable=broad-except
        raise LLMConfigurationError(
            f"External integration '{integration}' has a template that does not render: {error}"
        ) from error


def _litellm_completion():
    """The one place litellm exists (L2). Imported lazily so the app runs without the extra."""
    try:
        import litellm  # pylint: disable=import-outside-toplevel
    except ImportError as error:
        # The cause is in the message, not only chained onto the exception. "litellm is not
        # installed" is one explanation of an ImportError and not the only one: a release that is
        # installed and broken - 1.98.0 imported `typing.NotRequired` on Python 3.10 - reads
        # identically, and this line is what an operator or a CI log actually shows.
        raise ImproperlyConfigured(
            "litellm could not be imported, so no model can be called: "
            f"{type(error).__name__}: {error}. "
            "If it is not installed, install the app with the 'llm' extra: nautobot-event-tracker[llm]."
        ) from error
    return litellm.completion


def _token_usage(raw):
    """The token counts the provider reported, or zeros when it reported none (L5)."""
    usage = getattr(raw, "usage", None)
    if usage is None:
        return 0, 0
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)


def _cost(model, prompt_tokens, completion_tokens):
    """L5 - price the call from the registry's numbers, not litellm's price tables."""
    return (
        prompt_tokens * model.input_cost_per_million + completion_tokens * model.output_cost_per_million
    ) / TOKENS_PER_MILLION


def _response_text(raw):
    """The message content out of a litellm-shaped response, or None when there is none."""
    choices = getattr(raw, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if content is None:
        return None
    return str(content)


def _response_tool_calls(raw):
    """The tools the model asked for, and the reason the answer is unusable when it is.

    Returns `(tool_calls, problem)` rather than raising: the caller has to write the usage record
    before anything is raised (L1), and returning the fault lets it do that once for both halves
    of an unusable answer.

    Defensive throughout, because this is the shape a provider is most likely to differ on: a
    missing field reads as "no tool calls", which is a plain completion and always safe. The one
    thing that is not waved through is arguments that will not parse - a tool call the app cannot
    read is not a tool call it should guess at, and section 8 says it raises.
    """
    choices = getattr(raw, "choices", None)
    if not choices:
        return (), None
    message = getattr(choices[0], "message", None)
    raw_calls = getattr(message, "tool_calls", None) or []

    calls = []
    for raw_call in raw_calls:
        function = getattr(raw_call, "function", None)
        name = str(getattr(function, "name", "") or "")
        if not name:
            return (), "A tool call arrived with no tool name."

        raw_arguments = getattr(function, "arguments", None)
        if raw_arguments is None or raw_arguments == "":
            # An argument-less tool is ordinary, and providers spell "none" as an empty string, a
            # missing field or "{}" depending on the day.
            arguments = {}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                arguments = json.loads(raw_arguments)
            except (TypeError, ValueError):
                return (), f"The arguments for '{name}' are not valid JSON."
            if not isinstance(arguments, dict):
                return (), f"The arguments for '{name}' are not a JSON object."

        calls.append(ToolCall(identifier=str(getattr(raw_call, "id", "") or ""), name=name, arguments=arguments))

    return tuple(calls), None


def _record_usage(  # pylint: disable=too-many-arguments
    *,
    model,
    ticket,
    purpose,
    latency_ms,
    prompt_tokens=0,
    completion_tokens=0,
    cost=0,
    request_id="",
    error=None,
):
    """L1 - the one place a usage record is written; success and failure both land here.

    Commits on its own so the record survives what the caller does next: a ticket write that
    rolls back afterwards must not unwrite the money it spent. That holds because callers call
    the model *outside* a transaction (rule T5 says so for triage, and a network call inside one
    would be a fault of its own); called inside an enclosing atomic block this is only a
    savepoint, and the record would roll back with it. No `full_clean()`:
    every value is service-constructed or capped here, and validating the FKs the service just
    fetched would cost three queries per call on the consumer's hot path.
    """
    with transaction.atomic():
        record = LLMUsageRecord.objects.create(
            model=model,
            ticket=ticket,
            purpose=purpose,
            request_id=request_id[:CHARFIELD_MAX_LENGTH],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            latency_ms=latency_ms,
            success=error is None,
            error=str(error)[:ERROR_TEXT_CAP] if error is not None else "",
        )
    _maybe_prune()
    return record


def _maybe_prune():
    """L7 - delete usage records older than the retention window, at most once per process per day.

    Piggybacked on the write path, like `StatsRecorder._prune`, so retention needs no scheduled
    job and a deployment that never calls a model never pays for one either. Never raises: a
    failed cleanup must not turn the successful call it rode in on into an error.
    """
    global _last_pruned_on, _prune_failures  # pylint: disable=global-statement

    today = timezone.now().date()
    if _last_pruned_on == today:
        return

    try:
        retention_days = get_settings()["usage_retention_days"]
    except ImproperlyConfigured:
        # A settings fault does not repair itself between two calls. The day is marked done so the
        # operator reads this once rather than once per model call, and nothing is deleted.
        _last_pruned_on = today
        logger.exception("Could not prune LLM usage records")
        return

    try:
        cutoff = timezone.now() - timedelta(days=retention_days)
        deleted, _ = LLMUsageRecord.objects.filter(called_at__lt=cutoff).delete()
    except Exception:  # pylint: disable=broad-except
        # A lock or a statement timeout on the first prune of a large table is transient, so the
        # day is left unmarked and the next call tries again - but only so many times. A durable
        # fault fails identically every time, and an unbounded retry puts a whole-table DELETE on
        # the consumer's hot path for every event it triages.
        _prune_failures += 1
        if _prune_failures >= MAX_PRUNE_FAILURES:
            _last_pruned_on = today
            logger.exception(
                "Could not prune LLM usage records after %d attempts; not trying again today", _prune_failures
            )
        else:
            logger.exception("Could not prune LLM usage records; will retry on the next call")
        return

    _prune_failures = 0
    _last_pruned_on = today
    if deleted:
        logger.info("Pruned %d LLM usage records older than %d days", deleted, retention_days)


def _elapsed_ms(started):
    """Whole milliseconds since `started`."""
    return int((time.monotonic() - started) * 1000)
