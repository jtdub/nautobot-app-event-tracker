"""One message in, one decision out - and, when the decision is to accept it, one ticket.

This is the only module in the package that causes a ticket to exist, and it does so by calling
`services.tickets`. It does not import `EventTicket` to write, it never assigns `status`, and
nothing it does carries a user, so rule S4 holds and nothing the consumer does can be attributed
to a person. The ticket write itself is `source=system`; the actions LLM triage decided - an
attach, a suppression - carry `source=ai`, so the trail says who decided what (T6).
"""

import logging
import time

from django.db import DatabaseError, transaction

from nautobot_event_tracker.choices import TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.ingestion.constants import (
    ACTION_ATTACH,
    ACTION_DROP,
    ACTION_SUPPRESS,
    REASON_TRIAGE,
    REASON_UNKNOWN_TOPIC,
)
from nautobot_event_tracker.ingestion.normalize import NormalizationError, capped, decode, normalize
from nautobot_event_tracker.ingestion.prefilter import Decision
from nautobot_event_tracker.services import llm as llm_service
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import TicketImmutableError

logger = logging.getLogger(__name__)

#: How often to repeat the warning about a dedup key template that will not resolve. Once per topic
#: per five minutes: often enough to notice, rarely enough that a misconfiguration cannot itself
#: become the flood it is warning about.
DEDUP_WARNING_INTERVAL_SECONDS = 300

_dedup_warned_at = {}


def handle_message(message, *, rules, recorder, config, triage=None, write=True):  # pylint: disable=too-many-arguments
    """Decide what this message is, and write the ticket if it is one.

    The caller counts the message as received before calling this - a retry after a database
    failure re-enters here, and counting it twice would break the invariant that every received
    message ends in exactly one outcome.

    Returns the `Decision` that was reached, which is what `--dry-run` prints and what the loop
    logs. `write=False` decides without applying, so an operator can tune filters against live
    traffic without consequences; a dry run also passes a recorder that counts nothing, so this
    function does not check the flag for anything but the ticket itself.

    `triage` is the LLM triage collaborator, or None when triage is disabled - which includes
    every dry run (T9: a dry run writes nothing, and rule L1 forbids an unrecorded model call).
    It runs here, between the pre-filter and the write, so no transaction ever spans it (T5).
    """
    topic_config = rules.topic(message.topic)
    if topic_config is None:
        # F1. Both brokers subscribe only to configured topics, so this is what a topic removed
        # from the configuration mid-run looks like rather than an everyday occurrence.
        return _dropped(message.topic, REASON_UNKNOWN_TOPIC, recorder)

    try:
        event = normalize(
            decode(message.value),
            topic_config=topic_config,
            broker_timestamp=message.timestamp,
        )
    except NormalizationError as error:
        # I4 - a poison message is counted, logged with enough to find it again, and dropped. It is
        # never retried: it will not parse on the second attempt either, and a consumer that keeps
        # trying stops consuming anything else.
        logger.warning(
            "Could not read a message from %s at offset %s (%s); discarding it",
            message.topic,
            message.offset,
            error.reason,
        )
        recorder.record(message.topic, errored=1)
        return Decision(ACTION_DROP, error.reason)

    _warn_about_an_unresolvable_dedup_key(event, topic_config)

    result = rules.decide(event, topic_config)
    if result.decision.action == ACTION_DROP:
        # T1 - a drop never reaches the model.
        return _dropped(message.topic, result.decision.reason, recorder)

    # Read off the triage result here rather than sniffed for further down: only this branch knows
    # whether `result` is the pre-filter's or the model's, and a missing attribute should be a
    # loud failure rather than a silently unattributed decision.
    usage_record_ids = ()
    ai_decided = False

    if triage is not None and write:
        result = triage.decide(event, topic_config, result, identity=message.identity)
        if result.triaged:
            if not result.from_memo:
                # Counted only when a call actually happened. `received` is counted outside the
                # retry loop for the same reason: a retry re-enters here, and `triaged` is the
                # number of model calls an operator is paying for.
                recorder.record(message.topic, triaged=1, triage_errors=1 if result.errored else 0)
            usage_record_ids = result.usage_record_ids
            # T4's fallback returns the pre-filter's own decision, so the model decided this one
            # only when the call actually produced an answer.
            ai_decided = not result.errored
        if result.decision.action == ACTION_DROP:
            # T8 - the counter key is fixed; the model's own reason goes to the log.
            logger.info("LLM triage dropped an event from %s: %s", message.topic, result.decision.reason)
            return _dropped(message.topic, REASON_TRIAGE, recorder)

    if write:
        ticket = _apply(event, result, recorder, config, ai_decided=ai_decided)
        _link_usage_records(usage_record_ids, ticket)
    return result.decision


def _link_usage_records(usage_record_ids, ticket):
    """Point this event's spend at the ticket it produced, without putting the ticket at risk.

    The link runs after the write's transaction, not inside it: a rolled-back ticket must leave
    the usage record standing (L1) with no ticket to point at. That places it after the commit but
    still inside the caller's retry loop, so it must not raise - a retry would re-run the whole
    message and, for an event with no dedup key, open a second ticket for it. An unlinked usage
    record is a gap in the cost report; a duplicate ticket is a gap in somebody's day.
    """
    if not usage_record_ids:
        return
    try:
        llm_service.link_usage_records(usage_record_ids, ticket)
    except DatabaseError:
        logger.warning(
            "Could not link LLM usage records %s to ticket %s; the spend is recorded but unattributed",
            ", ".join(str(record_id) for record_id in usage_record_ids),
            ticket.pk if ticket is not None else None,
            exc_info=True,
        )


def _warn_about_an_unresolvable_dedup_key(event, topic_config, clock=time.monotonic):
    """Say when a configured dedup template resolved to nothing.

    Without this the only symptom is a stream of near-identical tickets whose event count never
    leaves 1, which is a slow thing to notice and a confusing thing to diagnose.
    """
    if event.dedup_key or not topic_config.dedup_key_template:
        return

    now = clock()
    last = _dedup_warned_at.get(topic_config.name)
    if last is not None and now - last < DEDUP_WARNING_INTERVAL_SECONDS:
        return

    _dedup_warned_at[topic_config.name] = now
    logger.warning(
        "The dedup key template for %s (%r) did not resolve against this payload, so events on "
        "this topic are each opening their own ticket",
        topic_config.name,
        topic_config.dedup_key_template,
    )


def _apply(event, result, recorder, config, *, ai_decided=False):
    """Apply what was decided: open, join by key, suppress - or attach where triage said to.

    Returns the ticket the event ended up on, for the usage-record link.
    """
    if result.decision.action == ACTION_ATTACH:
        ticket = _attach(event, result, recorder)
        if ticket is not None:
            return ticket
        # The target vanished or reached a terminal status between shortlist and write. A ticket
        # too many beats an event lost, so the event falls through to open its own (spec 6.3).
        logger.info("LLM triage's attach target was gone; opening a ticket instead")

    return _write_ticket(event, result, recorder, config, ai_decided=ai_decided)


def _attach(event, result, recorder):
    """Join the ticket triage chose, as the AI actor that chose it (T6). None when it cannot be."""
    from nautobot_event_tracker.models import EventTicket  # pylint: disable=import-outside-toplevel

    # Read inside the transaction and locked, because `join_ticket` writes the whole row: an
    # instance fetched outside it would be saved back over whatever a human or a second consumer
    # did in between, reverting their status change and losing their `event_count`. This path
    # holds neither the advisory dedup lock nor a unique constraint, so the row lock is the only
    # thing serializing two consumers onto one ticket.
    #
    # Terminal status is not filtered out here: `join_ticket` refuses an AI actor on one (S3), and
    # that refusal is caught below. One gate, in the layer that owns the rule.
    try:
        with transaction.atomic():
            ticket = EventTicket.objects.select_for_update().filter(pk=result.target_ticket_id).first()
            if ticket is None:
                return None

            ticket = ticket_service.join_ticket(
                ticket=ticket,
                source=TicketSourceChoices.AI,
                occurred_at=event.occurred_at,
                message=f"Attached by LLM triage: {result.decision.reason}",
            )
    except TicketImmutableError:
        # S3 - the ticket reached a terminal status before this transaction took its lock.
        return None

    recorder.record(event.topic, joined=1, triage_attached=1)
    return ticket


def _write_ticket(event, result, recorder, config, *, ai_decided=False):
    """Open or join the ticket this event belongs to, and suppress it if something said so.

    I2 - everything one message causes commits together: the ticket, its `created` update, and a
    suppression transition with its own `status_change`. The service's own atomic blocks nest
    inside this one as savepoints.

    A suppression carries the source of whoever decided it (T6): a Phase 2 rule is the system
    speaking, a triage verdict is the model's own act, and the trail should say which.
    """
    suppress = result.decision.action == ACTION_SUPPRESS

    with transaction.atomic():
        ticket = ticket_service.create_ticket(
            title=event.title,
            event_type=result.event_type,
            source=TicketSourceChoices.SYSTEM,
            severity=event.severity or None,
            description=event.description,
            dedup_key=event.dedup_key,
            payload=capped(event.payload, config.max_payload_bytes),
            occurred_at=event.occurred_at,
        )

        if suppress and ticket.was_created:
            # Only on a ticket this event opened. A rule firing on a later event is not grounds to
            # pull a ticket somebody has triaged and started working out from under them: the rule
            # governs what a ticket starts as, not what it stays.
            ticket_service.transition(
                ticket=ticket,
                to_status=TicketStatusChoices.SUPPRESSED,
                source=TicketSourceChoices.AI if ai_decided else TicketSourceChoices.SYSTEM,
                message=(
                    f"Suppressed by LLM triage: {result.decision.reason}"
                    if ai_decided
                    else f"Suppressed by ingestion rule '{result.decision.reason}'."
                ),
            )

    recorder.record(
        event.topic,
        opened=1 if ticket.was_created else 0,
        joined=0 if ticket.was_created else 1,
        suppressed=1 if suppress else 0,
    )
    return ticket


def _dropped(topic, reason, recorder):
    """Count a drop and report it."""
    recorder.record(topic, drop_reason=reason)
    return Decision(ACTION_DROP, reason)
