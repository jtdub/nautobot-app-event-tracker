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

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from nautobot.apps.constants import CHARFIELD_MAX_LENGTH
from nautobot.apps.utils import deepmerge
from nautobot.extras.choices import SecretsGroupSecretTypeChoices

from nautobot_event_tracker.choices import LITELLM_PROVIDER_PREFIXES
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

TOKENS_PER_MILLION = 1_000_000

#: The date (per process) on which `_maybe_prune` last ran, so retention costs one DELETE a day
#: rather than one per call. The same shape as `StatsRecorder._prune`'s bookkeeping.
_last_pruned_on = None  # pylint: disable=invalid-name


@dataclass(frozen=True)
class LLMResponse:
    """What a successful call returns: the text, and the accounting row that priced it.

    The numbers live on the record alone; the properties are conveniences, not copies, so a new
    accounting field is added in one place.
    """

    text: str
    record: LLMUsageRecord

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
    """The `llm` settings block with defaults applied per key."""
    configured = django_settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {}).get("llm") or {}
    return deepmerge(DEFAULTS, configured)


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


def complete(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    model,
    messages,
    purpose,
    ticket=None,
    max_tokens=None,
    timeout=None,
    response_format=None,
    client=None,
):
    """One model call: refuse (L8), resolve credentials (L3), call (L6), record (L1), price (L5).

    `client` is the test seam: a callable `(model_string, messages, **kwargs)` returning a
    litellm-shaped response object. The default is litellm itself, resolved before anything is
    attempted so that a missing package is a plain `ImproperlyConfigured` - a deployment fault,
    not a failed call, and not on the record (section 5.2 of the spec).

    Returns an `LLMResponse`. Raises `LLMConfigurationError` before any network traffic,
    `LLMCallError` when the call fails, and `LLMResponseError` when what came back is unusable -
    the latter two carrying the usage record already written for the attempt (L4).
    """
    _check_enabled(model)
    call = client if client is not None else _litellm_completion()

    call_kwargs = dict(model.default_parameters or {})
    call_kwargs.update(_credential_kwargs(model.provider))
    call_kwargs["timeout"] = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS
    effective_max_tokens = max_tokens if max_tokens is not None else model.max_output_tokens
    if effective_max_tokens is not None:
        call_kwargs["max_tokens"] = effective_max_tokens
    if response_format is not None:
        call_kwargs["response_format"] = response_format

    started = time.monotonic()
    try:
        raw = call(_model_string(model), messages, **call_kwargs)
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

    record = _record_usage(
        model=model,
        ticket=ticket,
        purpose=purpose,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=_cost(model, prompt_tokens, completion_tokens),
        request_id=str(getattr(raw, "id", "") or ""),
        error=None if text is not None else "The response carried no message content.",
    )
    if text is None:
        raise LLMResponseError(f"LLM call to {model} returned no usable content.", record=record)

    return LLMResponse(text=text, record=record)


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
    """The litellm model string for this registry entry. litellm routes on the prefix."""
    return f"{LITELLM_PROVIDER_PREFIXES[model.provider.provider_type]}/{model.name}"


def _credential_kwargs(provider):
    """L3 - the endpoint and key, read from Nautobot at call time and passed straight through.

    A missing key is not an error here: an on-premises endpoint may not want one, and one that
    does will refuse the call itself, which the record then shows. Prefers the token secret type
    and falls back to the plain secret type, so either way an operator has modeled "the key"
    works.
    """
    integration = provider.external_integration
    kwargs = {}
    if integration.remote_url:
        kwargs["api_base"] = integration.remote_url
    for secret_type in (SecretsGroupSecretTypeChoices.TYPE_TOKEN, SecretsGroupSecretTypeChoices.TYPE_SECRET):
        key = read_secret(integration, secret_type)
        if key:
            kwargs["api_key"] = key
            break
    return kwargs


def _litellm_completion():
    """The one place litellm exists (L2). Imported lazily so the app runs without the extra."""
    try:
        import litellm  # pylint: disable=import-outside-toplevel
    except ImportError as error:
        raise ImproperlyConfigured(
            "litellm is not installed. Install the app with the 'llm' extra: nautobot-app-event-tracker[llm]."
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
    global _last_pruned_on  # pylint: disable=global-statement

    today = timezone.now().date()
    if _last_pruned_on == today:
        return
    _last_pruned_on = today

    try:
        retention_days = int(get_settings()["usage_retention_days"])
        cutoff = timezone.now() - timedelta(days=retention_days)
        deleted, _ = LLMUsageRecord.objects.filter(called_at__lt=cutoff).delete()
        if deleted:
            logger.info("Pruned %d LLM usage records older than %d days", deleted, retention_days)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not prune LLM usage records")


def _elapsed_ms(started):
    """Whole milliseconds since `started`."""
    return int((time.monotonic() - started) * 1000)
