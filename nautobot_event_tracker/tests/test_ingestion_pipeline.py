"""Test the pipeline: a message in, a ticket out, and the counters that say what happened.

Every ticket these tests find was written by `services.tickets`, never by the pipeline reaching
for the model. The rollback test is the one that proves it: a failure inside the transaction has
to leave neither a ticket nor an update.
"""

from unittest import mock

from django.db import DatabaseError
from django.test import TestCase, override_settings

from nautobot_event_tracker.choices import (
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.ingestion import config, pipeline, prefilter
from nautobot_event_tracker.ingestion.constants import (
    ACTION_ACCEPT,
    ACTION_DROP,
    ACTION_SUPPRESS,
    REASON_EVENT_TYPE_DISABLED,
    REASON_NOT_AN_OBJECT,
    REASON_UNDECODABLE,
    REASON_UNKNOWN_TOPIC,
)
from nautobot_event_tracker.ingestion.stats import StatsRecorder
from nautobot_event_tracker.models import EventTicket, IngestionStats, TicketUpdate
from nautobot_event_tracker.tests import fixtures
from nautobot_event_tracker.tests.test_ingestion_prefilter import FakeClock

TOPIC = fixtures.INGESTION_TOPIC


class PipelineTestCase(TestCase):
    """A configured pipeline, a recorder, and a way to push one message through."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def setUp(self):
        """Build the pieces the pipeline needs."""
        super().setUp()
        self.clock = FakeClock()
        self.recorder = StatsRecorder(
            consumer_name="test-consumer",
            bucket_seconds=300,
            flush_seconds=10,
            retention_days=30,
            clock=self.clock,
        )

    def handle(self, payload=None, *, topic_settings=None, topic="network.events", value=None, **kwargs):
        """Push one message through the pipeline and return the decision."""
        with override_settings(
            PLUGINS_CONFIG={
                "nautobot_event_tracker": {
                    "ingestion": {"topics": {"network.events": {**TOPIC, **(topic_settings or {})}}}
                }
            }
        ):
            loaded = config.load()
        rules = prefilter.PreFilter(loaded, clock=self.clock)
        message = (
            fixtures.broker_message(payload if payload is not None else self.payload(), topic=topic)
            if value is None
            else fixtures.BrokerMessage(topic=topic, value=value)
        )
        return pipeline.handle_message(message, rules=rules, recorder=self.recorder, config=loaded, **kwargs)

    payload = staticmethod(fixtures.event_payload)

    def counts(self, topic="network.events"):
        """Flush and return the counter row for this topic."""
        self.recorder.flush()
        return IngestionStats.objects.get(consumer_name="test-consumer", topic=topic)


class TestAccepting(PipelineTestCase):
    """The ordinary path: an event becomes a ticket."""

    def test_an_accepted_event_opens_a_ticket(self):
        """The whole point of the phase."""
        decision = self.handle()
        self.assertEqual(decision.action, ACTION_ACCEPT)
        ticket = EventTicket.objects.get()
        self.assertEqual(ticket.title, "Interface ethernet-1/1 is down")
        self.assertEqual(ticket.event_type.name, "Test Interface Down")
        self.assertEqual(ticket.severity, SeverityChoices.MAJOR)
        self.assertEqual(ticket.status, TicketStatusChoices.NEW)

    def test_the_ticket_is_attributed_to_the_system(self):
        """Nothing the consumer does may look as though a person did it."""
        self.handle()
        ticket = EventTicket.objects.get()
        self.assertEqual(ticket.source, TicketSourceChoices.SYSTEM)
        for update in ticket.updates.all():
            self.assertEqual(update.source, TicketSourceChoices.SYSTEM)
            self.assertIsNone(update.user)

    def test_the_ticket_carries_the_raw_event(self):
        """Phase 4's resolver reads this; until then it is the evidence.."""
        self.handle()
        self.assertEqual(EventTicket.objects.get().payload, self.payload())

    def test_a_created_update_is_written(self):
        """Rule S1: no mutation without a trail, whoever caused it."""
        self.handle()
        self.assertEqual(EventTicket.objects.get().updates.first().update_type, UpdateTypeChoices.CREATED)

    def test_the_event_types_default_severity_applies_when_the_payload_has_none(self):
        """The service settles it, using the same rule the pre-filter compared against."""
        self.handle(self.payload(event={"type": "Test Interface Down"}))
        self.assertEqual(EventTicket.objects.get().severity, SeverityChoices.MAJOR)


class TestDeduplication(PipelineTestCase):
    """Rule S5, driven from the broker rather than from a fixture."""

    def test_a_repeat_joins_the_open_ticket(self):
        """Redelivery is normal on an at-least-once broker; a second ticket is not."""
        self.handle()
        self.handle()
        ticket = EventTicket.objects.get()
        self.assertEqual(ticket.event_count, 2)
        self.assertEqual(ticket.updates.filter(update_type=UpdateTypeChoices.RECURRENCE).count(), 1)

    def test_a_different_key_opens_its_own_ticket(self):
        """The key is what joins events, and it is rendered from the payload."""
        self.handle()
        self.handle(self.payload(host="leaf-02"))
        self.assertEqual(EventTicket.objects.count(), 2)

    def test_opened_and_joined_are_counted_separately(self):
        """'Is anything actually new' is the question the stats page answers."""
        self.handle()
        self.handle()
        counts = self.counts()
        self.assertEqual(counts.tickets_opened, 1)
        self.assertEqual(counts.tickets_joined, 1)
        self.assertEqual(counts.received, 2)


class TestSuppression(PipelineTestCase):
    """A rule that says an event is noise worth recording."""

    RULE = {"name": "known-flapper", "action": ACTION_SUPPRESS, "when": {"host": "^leaf-01$"}}

    def test_a_suppressed_event_opens_a_suppressed_ticket(self):
        """This is what the suppressed state exists for."""
        decision = self.handle(topic_settings={"rules": [self.RULE]})
        self.assertEqual(decision.action, ACTION_SUPPRESS)
        self.assertEqual(EventTicket.objects.get().status, TicketStatusChoices.SUPPRESSED)

    def test_the_suppression_names_the_rule_in_the_trail(self):
        """A person asking why a ticket is suppressed should find the answer on the ticket."""
        self.handle(topic_settings={"rules": [self.RULE]})
        update = EventTicket.objects.get().updates.get(update_type=UpdateTypeChoices.STATUS_CHANGE)
        self.assertIn("known-flapper", update.message)
        self.assertEqual(update.source, TicketSourceChoices.SYSTEM)

    def test_a_suppression_rule_does_not_touch_a_ticket_it_joined(self):
        """A rule governs what a ticket starts as, not what it stays.

        Somebody triaged this ticket and started work. A later matching event must not pull it back
        out from under them.
        """
        self.handle()
        ticket = EventTicket.objects.get()
        user = fixtures.create_user()
        from nautobot_event_tracker.services import tickets as ticket_service  # pylint: disable=import-outside-toplevel

        ticket_service.transition(
            ticket=ticket,
            to_status=TicketStatusChoices.TRIAGED,
            source=TicketSourceChoices.HUMAN,
            user=user,
        )

        self.handle(topic_settings={"rules": [self.RULE]})
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatusChoices.TRIAGED)
        self.assertEqual(ticket.event_count, 2)

    def test_suppressed_is_counted_alongside_the_ticket_it_opened(self):
        """It is not a fourth outcome: the ticket exists, and the invariant still holds."""
        self.handle(topic_settings={"rules": [self.RULE]})
        counts = self.counts()
        self.assertEqual(counts.suppressed, 1)
        self.assertEqual(counts.tickets_opened, 1)
        self.assertEqual(counts.received, counts.accounted_for)


class TestRefusing(PipelineTestCase):
    """Everything that does not become a ticket, and the counter that says why."""

    def test_an_unconfigured_topic_is_dropped(self):
        """There is nothing to normalize a message against."""
        decision = self.handle(topic="something.else")
        self.assertEqual(decision.reason, REASON_UNKNOWN_TOPIC)
        self.assertFalse(EventTicket.objects.exists())

    def test_undecodable_json_is_counted_as_an_error(self):
        """A poison message is discarded, not retried; the counter is how anyone knows."""
        decision = self.handle(value=b"{not json")
        self.assertEqual(decision.reason, REASON_UNDECODABLE)
        self.assertEqual(self.counts().errored, 1)
        self.assertFalse(EventTicket.objects.exists())

    def test_a_json_list_is_counted_as_an_error(self):
        """A batch is a shape this pipeline does not handle."""
        decision = self.handle(value=b'[{"a": 1}]')
        self.assertEqual(decision.reason, REASON_NOT_AN_OBJECT)
        self.assertEqual(self.counts().errored, 1)

    def test_a_filtered_event_is_dropped_under_its_reason(self):
        """The reason is the counter key, which is where an operator looks first."""
        decision = self.handle(self.payload(event={"type": "Test Disabled Type"}))
        self.assertEqual(decision.action, ACTION_DROP)
        self.assertEqual(self.counts().drops_by_reason, {REASON_EVENT_TYPE_DISABLED: 1})

    def test_a_dropped_event_still_counts_as_received(self):
        """Received is every message the broker handed over, whatever became of it."""
        self.handle(self.payload(event={"type": "Test Disabled Type"}))
        counts = self.counts()
        self.assertEqual(counts.received, 1)
        self.assertEqual(counts.dropped, 1)
        self.assertEqual(counts.received, counts.accounted_for)


class TestDryRun(PipelineTestCase):
    """Tuning filters against live traffic, without consequences."""

    def test_a_dry_run_reaches_a_decision(self):
        """That is the whole output an operator wants."""
        self.assertEqual(self.handle(write=False).action, ACTION_ACCEPT)

    def test_a_dry_run_writes_no_ticket(self):
        """Nothing at all, so the same messages can be replayed afterwards."""
        self.handle(write=False)
        self.assertFalse(EventTicket.objects.exists())

    def test_a_dry_run_writes_no_counters(self):
        """Counters are a record of what the consumer did, and it did nothing."""
        self.handle(write=False)
        self.recorder.flush()
        self.assertFalse(IngestionStats.objects.exists())


class TestAtomicity(PipelineTestCase):
    """One message, one transaction."""

    def test_a_failure_leaves_neither_ticket_nor_update(self):
        """The suppression transition failing must roll the ticket back with it."""
        with mock.patch(
            "nautobot_event_tracker.services.tickets.transition",
            side_effect=DatabaseError("connection lost"),
        ):
            with self.assertRaises(DatabaseError):
                self.handle(topic_settings={"rules": [TestSuppression.RULE]})

        self.assertFalse(EventTicket.objects.exists())
        self.assertFalse(TicketUpdate.objects.exists())

    def test_a_database_failure_is_not_swallowed(self):
        """The loop has to see it, so that it can retry and then refuse to acknowledge."""
        with mock.patch(
            "nautobot_event_tracker.services.tickets.create_ticket",
            side_effect=DatabaseError("connection lost"),
        ):
            with self.assertRaises(DatabaseError):
                self.handle()
