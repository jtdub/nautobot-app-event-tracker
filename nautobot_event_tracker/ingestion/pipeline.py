"""One message in, one decision out - and, when the decision is to accept it, one ticket.

This is the only module in the package that causes a ticket to exist, and it does so by calling
`services.tickets`. It does not import `EventTicket` to write, it never assigns `status`, and
everything it does carries `source=system` with no user, so rule S4 holds and nothing the consumer
does can be attributed to a person.
"""

import logging

from django.db import transaction

from nautobot_event_tracker.choices import TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.ingestion.constants import ACTION_DROP, ACTION_SUPPRESS, REASON_UNKNOWN_TOPIC
from nautobot_event_tracker.ingestion.normalize import NormalizationError, decode, normalize
from nautobot_event_tracker.ingestion.prefilter import Decision
from nautobot_event_tracker.services import tickets as ticket_service

logger = logging.getLogger(__name__)


def handle_message(message, *, rules, recorder, config, write=True):
    """Decide what this message is, and write the ticket if it is one.

    Returns the `Decision` that was reached, which is what `--dry-run` prints and what the loop
    logs. `write=False` runs every step except the ticket and the counters, so an operator can tune
    filters against live traffic without consequences.

    Phase 3's LLM triage goes between the pre-filter and the ticket, returning this same `Decision`
    type. Nothing else here changes when it arrives.
    """
    if write:
        recorder.record(message.topic, received=1, message_time=message.timestamp)

    topic_config = rules.topic(message.topic)
    if topic_config is None:
        # F1. Both brokers subscribe only to configured topics, so this is what a topic removed
        # from the configuration mid-run looks like rather than an everyday occurrence.
        return _dropped(message.topic, REASON_UNKNOWN_TOPIC, recorder, write)

    try:
        event = normalize(
            decode(message.value),
            topic_config=topic_config,
            broker_timestamp=message.timestamp,
            max_payload_bytes=config.max_payload_bytes,
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
        if write:
            recorder.record(message.topic, errored=1)
        return Decision(ACTION_DROP, error.reason)

    result = rules.decide(event, topic_config)
    if result.decision.action == ACTION_DROP:
        return _dropped(message.topic, result.decision.reason, recorder, write)

    if write:
        _write_ticket(event, result, recorder)
    return result.decision


def _write_ticket(event, result, recorder):
    """Open or join the ticket this event belongs to, and suppress it if a rule said so.

    I2 - everything one message causes commits together: the ticket, its `created` update, and a
    suppression transition with its own `status_change`. The service's own atomic blocks nest
    inside this one as savepoints.
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
            payload=event.payload,
            occurred_at=event.occurred_at,
        )

        if suppress and ticket.was_created:
            # Only on a ticket this event opened. A rule firing on a later event is not grounds to
            # pull a ticket somebody has triaged and started working out from under them: the rule
            # governs what a ticket starts as, not what it stays.
            ticket_service.transition(
                ticket=ticket,
                to_status=TicketStatusChoices.SUPPRESSED,
                source=TicketSourceChoices.SYSTEM,
                message=f"Suppressed by ingestion rule '{result.decision.reason}'.",
            )

    recorder.record(
        event.topic,
        opened=1 if ticket.was_created else 0,
        joined=0 if ticket.was_created else 1,
        suppressed=1 if suppress else 0,
    )


def _dropped(topic, reason, recorder, write):
    """Count a drop and report it."""
    if write:
        recorder.record(topic, drop_reason=reason)
    return Decision(ACTION_DROP, reason)
