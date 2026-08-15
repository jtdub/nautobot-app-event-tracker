"""One message in, one decision out - and, when the decision is to accept it, one ticket.

This is the only module in the package that causes a ticket to exist, and it does so by calling
`services.tickets`. It does not import `EventTicket` to write, it never assigns `status`, and
everything it does carries `source=system` with no user, so rule S4 holds and nothing the consumer
does can be attributed to a person.
"""

import logging
import time

from django.db import transaction

from nautobot_event_tracker.choices import TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.ingestion.constants import ACTION_DROP, ACTION_SUPPRESS, REASON_UNKNOWN_TOPIC
from nautobot_event_tracker.ingestion.normalize import NormalizationError, capped, decode, normalize
from nautobot_event_tracker.ingestion.prefilter import Decision
from nautobot_event_tracker.services import tickets as ticket_service

logger = logging.getLogger(__name__)

#: How often to repeat the warning about a dedup key template that will not resolve. Once per topic
#: per five minutes: often enough to notice, rarely enough that a misconfiguration cannot itself
#: become the flood it is warning about.
DEDUP_WARNING_INTERVAL_SECONDS = 300

_dedup_warned_at = {}


def handle_message(message, *, rules, recorder, config, write=True):
    """Decide what this message is, and write the ticket if it is one.

    The caller counts the message as received before calling this - a retry after a database
    failure re-enters here, and counting it twice would break the invariant that every received
    message ends in exactly one outcome.

    Returns the `Decision` that was reached, which is what `--dry-run` prints and what the loop
    logs. `write=False` decides without applying, so an operator can tune filters against live
    traffic without consequences; a dry run also passes a recorder that counts nothing, so this
    function does not check the flag for anything but the ticket itself.

    Phase 3's LLM triage goes between `rules.decide()` below and the write, returning a `Decision`
    the same dispatch reads. Its extra `attach` action becomes one more branch in `_apply()`.
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
        return _dropped(message.topic, result.decision.reason, recorder)

    if write:
        _write_ticket(event, result, recorder, config)
    return result.decision


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


def _write_ticket(event, result, recorder, config):
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
                source=TicketSourceChoices.SYSTEM,
                message=f"Suppressed by ingestion rule '{result.decision.reason}'.",
            )

    recorder.record(
        event.topic,
        opened=1 if ticket.was_created else 0,
        joined=0 if ticket.was_created else 1,
        suppressed=1 if suppress else 0,
    )


def _dropped(topic, reason, recorder):
    """Count a drop and report it."""
    recorder.record(topic, drop_reason=reason)
    return Decision(ACTION_DROP, reason)
