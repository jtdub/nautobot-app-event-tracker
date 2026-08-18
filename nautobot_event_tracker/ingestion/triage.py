"""LLM triage: what an accepted event deserves, asked of a model that is never trusted blindly.

This is the step Phase 2 section 8.4 reserved a seam for: it sits between the pre-filter and the
ticket write, takes the pre-filter's `FilterResult`, and returns the same `Decision` shape with
one extra possible action - `attach`. It imports `services.llm`, never litellm (rule L2), and
writes nothing: the pipeline applies decisions.

Rules implemented here, referenced by number from the Phase 3 spec:

* **T1** - only pre-filter survivors reach the model (the pipeline calls this after the drop
  dispatch, and rule-decided suppressions pass through untouched).
* **T2** - an event whose dedup key matches an open ticket skips the model; S5 will join it.
* **T3** - the model's answer is constrained to the four actions and the app-built shortlist;
  anything else reads as accept.
* **T4** - any failure yields accept and is counted; the consumer never crashes on a model.
* **T5** - the model runs outside any transaction, and a database retry of the same message
  reuses the memoized decision rather than paying twice.
* **T7** - the prompt carries the capped payload and the shortlist, nothing else.
"""

import json
import logging
from dataclasses import dataclass

from nautobot_event_tracker.choices import TERMINAL_STATUSES, LLMPurposeChoices
from nautobot_event_tracker.ingestion.constants import (
    ACTION_ACCEPT,
    ACTION_ATTACH,
    TRIAGE_ACTIONS,
)
from nautobot_event_tracker.ingestion.prefilter import Decision
from nautobot_event_tracker.services import llm as llm_service
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import LLMError

logger = logging.getLogger(__name__)

#: The instruction the model works under. The vocabulary and the shortlist rule are restated to
#: the model here, and enforced in `_parse` regardless of whether it listened (T3).
SYSTEM_PROMPT = (
    "You triage network events for a ticketing system. Answer with one JSON object and nothing "
    'else: {"action": "accept" | "attach" | "suppress" | "drop", "reason": "<one short sentence>", '
    '"ticket": <index from the open-ticket list, required with "attach">}. '
    "Choose accept to open a new ticket for a real problem; attach when the event is the same "
    "underlying problem as one of the listed open tickets; suppress for a real but unactionable "
    "event, which opens a ticket nobody is paged for; drop only for pure noise. "
    "When unsure, choose accept."
)


@dataclass(frozen=True)
class TriageResult:
    """The pre-filter's `FilterResult` shape, extended with what applying a triage verdict needs."""

    decision: Decision
    event_type: object = None
    #: The ticket an `attach` decision points at. Always one the shortlist offered (T3).
    target_ticket_id: object = None
    #: Usage records written for this decision, linked to the ticket after its write commits.
    usage_record_ids: tuple = ()
    #: Whether the model actually ran - false for pass-throughs and short-circuits.
    triaged: bool = False
    #: Whether T4's fallback happened.
    errored: bool = False


def _passthrough(filter_result):
    """The pre-filter's decision, unchanged."""
    return TriageResult(decision=filter_result.decision, event_type=filter_result.event_type)


class TriageFilter:  # pylint: disable=too-few-public-methods
    """The triage step, shaped like `PreFilter`: built once, `decide()` called per event."""

    def __init__(self, config, *, complete=None):
        """Hold the configuration and the call seam; resolve the model lazily.

        `complete` is the test seam, defaulting to `services.llm.complete`. The model is resolved
        on first use rather than here: startup validation (`config.database_problems`) has already
        checked it exists, and resolving lazily keeps construction database-free for tests.
        """
        self.triage = config.triage
        self._complete = complete if complete is not None else llm_service.complete
        self._model = None
        self._memo_key = None
        self._memo_result = None

    def decide(self, event, topic_config, filter_result, *, offset=None):
        """One survivor in, one `TriageResult` out. Never raises (T4)."""
        if not topic_config.triage:
            return _passthrough(filter_result)

        if filter_result.decision.action != ACTION_ACCEPT:
            # A rule-decided suppression stands: the operator wrote that rule, and a model does
            # not get to overrule it. Only clean accepts are worth a token.
            return _passthrough(filter_result)

        # T2 - a recurrence is S5's job, and free. This is also what makes at-least-once
        # redelivery of a committed message cost nothing: its ticket is there to match.
        if event.dedup_key and self._open_ticket_exists(event.dedup_key):
            return _passthrough(filter_result)

        # T5 - a database retry re-enters the pipeline with the same message; the decision has
        # been paid for once and is not paid for again.
        memo_key = (event.topic, offset)
        if offset is not None and self._memo_key == memo_key:
            return self._memo_result

        result = self._ask_the_model(event, filter_result)
        if offset is not None:
            self._memo_key, self._memo_result = memo_key, result
        return result

    def _ask_the_model(self, event, filter_result):
        """Build the prompt, make the call, and read the answer with appropriate suspicion."""
        shortlist = self._shortlist(event)
        try:
            response = self._complete(
                model=self._get_model(),
                messages=self._messages(event, shortlist),
                purpose=LLMPurposeChoices.TRIAGE,
                timeout=self.triage.timeout_seconds,
                max_tokens=self.triage.max_output_tokens,
                response_format={"type": "json_object"},
            )
        except LLMError as error:
            # T4 - fail open. The call is already on the usage record (L1); the counter and the
            # log line are the operator-facing symptoms.
            logger.warning("LLM triage failed (%s); accepting the event", error)
            record = getattr(error, "record", None)
            return TriageResult(
                decision=filter_result.decision,
                event_type=filter_result.event_type,
                usage_record_ids=(record.pk,) if record is not None else (),
                triaged=True,
                errored=True,
            )

        action, reason, target = self._parse(response.text, shortlist)
        return TriageResult(
            decision=Decision(action, reason),
            event_type=filter_result.event_type,
            target_ticket_id=target,
            usage_record_ids=(response.record.pk,),
            triaged=True,
        )

    def _get_model(self):
        """The configured LLMModel, resolved once and cached for the life of the process."""
        if self._model is None:
            self._model = llm_service.get_model(self.triage.provider, self.triage.model)
        return self._model

    @staticmethod
    def _open_ticket_exists(dedup_key):
        """Whether S5 would join this event to an open ticket. One indexed existence query.

        The lookup itself belongs to the write layer, so this asks it rather than restating it.
        """
        return ticket_service.open_tickets_for_dedup_key(dedup_key).exists()

    def _shortlist(self, event):
        """The open tickets the model may attach to (spec 6.4): bounded and boring on purpose.

        Same event type first; the newest open tickets overall when the type has none. Returns a
        list of (ticket_id, description line) pairs; the model sees only the line's index.
        """
        from nautobot_event_tracker.models import EventTicket  # pylint: disable=import-outside-toplevel

        # Only the five columns the description line uses: a ticket row carries a payload of up to
        # `max_payload_bytes`, and reading five of those per triaged event to print a title is the
        # largest avoidable cost on this path.
        open_tickets = (
            EventTicket.objects.exclude(status__in=TERMINAL_STATUSES)
            .select_related("event_type")
            .only("title", "severity", "last_seen", "event_type__name")
            .order_by("-last_seen")
        )
        candidates = list(open_tickets.filter(event_type__name=event.event_type_name)[: self.triage.attach_candidates])
        if not candidates:
            candidates = list(open_tickets[: self.triage.attach_candidates])
        return [
            (
                ticket.pk,
                f"{index}: [{ticket.event_type.name}] {ticket.title} "
                f"(severity {ticket.severity}, last seen {ticket.last_seen:%Y-%m-%d %H:%M})",
            )
            for index, ticket in enumerate(candidates)
        ]

    def _messages(self, event, shortlist):
        """T7 - the capped event and the shortlist, and nothing else."""
        payload_json = json.dumps(event.payload, default=str)
        if len(payload_json) > self.triage.max_context_chars:
            payload_json = payload_json[: self.triage.max_context_chars] + "…(truncated)"

        lines = [
            f"Topic: {event.topic}",
            f"Event type: {event.event_type_name}",
            f"Title: {event.title}",
            f"Severity: {event.severity or 'unspecified'}",
        ]
        if event.description:
            lines.append(f"Description: {event.description}")
        lines.append(f"Payload: {payload_json}")
        if shortlist:
            lines.append("Open tickets:")
            lines.extend(line for _, line in shortlist)
        else:
            lines.append("Open tickets: none (attach is not available).")

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ]

    @staticmethod
    def _parse(text, shortlist):
        """T3 - read the model's answer; anything out of bounds is an accept.

        Returns `(action, reason, target_ticket_id)`. The reason is capped: it becomes a ticket
        message, not an essay.
        """
        try:
            start = text.index("{")
            answer, _ = json.JSONDecoder().raw_decode(text[start:])
        except (ValueError, TypeError):
            logger.warning("LLM triage answered unparsably (%.120r); accepting the event", text)
            return ACTION_ACCEPT, "", None

        action = answer.get("action") if isinstance(answer, dict) else None
        if action not in TRIAGE_ACTIONS:
            logger.warning("LLM triage chose an unknown action (%r); accepting the event", action)
            return ACTION_ACCEPT, "", None

        reason = str(answer.get("reason", ""))[:200]

        if action == ACTION_ATTACH:
            index = answer.get("ticket")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(shortlist):
                logger.warning("LLM triage attached outside the shortlist (%r); accepting the event", index)
                return ACTION_ACCEPT, "", None
            return ACTION_ATTACH, reason, shortlist[index][0]

        return action, reason, None
