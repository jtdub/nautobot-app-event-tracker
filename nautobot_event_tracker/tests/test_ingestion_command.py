"""Test the consumer process: what it refuses to start with, and how it stops.

The loop is driven directly through `ConsumerRunner` with a fake consumer, so nothing here waits
on a broker or on a signal being delivered for real.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import TestCase, override_settings

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.ingestion import config
from nautobot_event_tracker.management.commands.eventconsumer import (
    EXIT_UNRECOVERABLE,
    ConsumerRunner,
    UnrecoverableError,
)
from nautobot_event_tracker.models import EventTicket, IngestionStats
from nautobot_event_tracker.tests import fixtures

TOPIC = {
    "field_map": {"event_type": "event.type", "title": "message", "severity": "event.severity"},
    "defaults": {"event_type": "Test Interface Down"},
    "dedup_key_template": "{event.type}:{host}",
}


def ingestion(**overrides):
    """Build a PLUGINS_CONFIG override for these tests."""
    settings = {"consumer": "redis", "topics": {"network.events": TOPIC}, **overrides}
    return override_settings(PLUGINS_CONFIG={"nautobot_event_tracker": {"ingestion": settings}})


def payload(**overrides):
    """A payload the pipeline would turn into a ticket."""
    base = {
        "event": {"type": "Test Interface Down", "severity": SeverityChoices.MAJOR},
        "message": "Interface ethernet-1/1 is down",
        "host": "leaf-01",
    }
    base.update(overrides)
    return base


class RunnerTestCase(TestCase):
    """A runner driven by a fake consumer."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def build(self, messages=(), **kwargs):
        """A runner over these queued messages."""
        with ingestion():
            settings = config.load()
        consumer = fixtures.FakeEventConsumer(topics=settings.topic_names, messages=list(messages))
        runner = ConsumerRunner(consumer=consumer, settings=settings, sleep=lambda _seconds: None, **kwargs)
        return runner, consumer


class TestTheLoop(RunnerTestCase):
    """Polling, handling, acknowledging."""

    def test_it_handles_every_queued_message(self):
        """The ordinary case: three events, three tickets, and it stops when the queue runs dry."""
        messages = [fixtures.broker_message(payload(host=f"leaf-0{index}")) for index in range(1, 4)]
        runner, _ = self.build(messages, max_messages=3)
        runner.run()
        self.assertEqual(runner.handled, 3)
        self.assertEqual(EventTicket.objects.count(), 3)

    def test_a_message_is_acknowledged_only_after_its_ticket(self):
        """I1: acknowledging first would turn a crash into a lost event."""
        runner, consumer = self.build([fixtures.broker_message(payload())], max_messages=1)
        runner.run()
        self.assertEqual(len(consumer.acknowledged), 1)
        self.assertTrue(EventTicket.objects.exists())

    def test_an_empty_poll_does_not_count_as_a_message(self):
        """`max_messages` counts events, not trips round the loop."""
        runner, _ = self.build([], max_messages=1)
        runner.stop()
        runner.run()
        self.assertEqual(runner.handled, 0)

    def test_the_consumer_is_connected_and_closed(self):
        """The loop owns the connection's lifetime, on every path out."""
        runner, consumer = self.build([fixtures.broker_message(payload())], max_messages=1)
        runner.run()
        self.assertTrue(consumer.connected)
        self.assertTrue(consumer.closed)

    def test_counters_are_flushed_on_the_way_out(self):
        """A clean shutdown must not throw away the interval it was in the middle of."""
        runner, _ = self.build([fixtures.broker_message(payload())], max_messages=1)
        runner.run()
        self.assertEqual(IngestionStats.objects.get().received, 1)


class TestStopping(RunnerTestCase):
    """Shutdown."""

    def test_stopping_ends_the_loop(self):
        """The flag is what a signal handler sets; the loop reads it between messages."""
        runner, _ = self.build([fixtures.broker_message(payload())])
        runner.stop()
        runner.run()
        self.assertEqual(runner.handled, 0)

    def test_the_message_in_flight_is_finished_first(self):
        """I3: stop between messages, not in the middle of one."""
        runner, consumer = self.build([fixtures.broker_message(payload())])

        original = runner._handle  # pylint: disable=protected-access

        def handle_then_stop(message):
            """Ask the loop to stop while a message is being handled."""
            runner.stop()
            original(message)

        runner._handle = handle_then_stop  # pylint: disable=protected-access
        runner.run()

        self.assertEqual(len(consumer.acknowledged), 1)
        self.assertTrue(EventTicket.objects.exists())

    def test_the_consumer_is_closed_even_when_handling_raises(self):
        """A broker connection left open outlives the process that owned it."""
        runner, consumer = self.build([fixtures.broker_message(payload())])
        with mock.patch(
            "nautobot_event_tracker.management.commands.eventconsumer.handle_message",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                runner.run()
        self.assertTrue(consumer.closed)


class TestDatabaseFailure(RunnerTestCase):
    """I5: retried, then fatal, and never acknowledged."""

    def test_a_transient_failure_is_retried(self):
        """One bad attempt should not cost the message."""
        runner, consumer = self.build([fixtures.broker_message(payload())], max_messages=1)
        attempts = []

        def flaky(*args, **kwargs):
            """Fail once, then work."""
            attempts.append(1)
            if len(attempts) == 1:
                raise DatabaseError("connection lost")
            return mock.DEFAULT

        with mock.patch(
            "nautobot_event_tracker.management.commands.eventconsumer.handle_message",
            side_effect=flaky,
            return_value=mock.Mock(action="accept", reason=""),
        ):
            runner.run()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(consumer.acknowledged), 1)

    def test_giving_up_does_not_acknowledge(self):
        """The message stays on the broker, which is the whole reason to exit rather than continue."""
        runner, consumer = self.build([fixtures.broker_message(payload())], max_messages=1)
        with mock.patch(
            "nautobot_event_tracker.management.commands.eventconsumer.handle_message",
            side_effect=DatabaseError("connection lost"),
        ):
            with self.assertRaises(UnrecoverableError):
                runner.run()
        self.assertEqual(consumer.acknowledged, [])

    def test_giving_up_says_the_message_will_come_back(self):
        """The operator reading this log line needs to know nothing was lost."""
        runner, _ = self.build([fixtures.broker_message(payload())], max_messages=1)
        with mock.patch(
            "nautobot_event_tracker.management.commands.eventconsumer.handle_message",
            side_effect=DatabaseError("connection lost"),
        ):
            with self.assertRaises(UnrecoverableError) as caught:
                runner.run()
        self.assertIn("redelivered", str(caught.exception))


class TestDryRun(RunnerTestCase):
    """Tuning filters against live traffic, without consequences."""

    def test_it_writes_nothing_and_acknowledges_nothing(self):
        """Acknowledging nothing is what lets the same messages be replayed afterwards."""
        stdout = StringIO()
        runner, consumer = self.build(
            [fixtures.broker_message(payload())],
            max_messages=1,
            dry_run=True,
            stdout=stdout,
        )
        runner.run()

        self.assertFalse(EventTicket.objects.exists())
        self.assertFalse(IngestionStats.objects.exists())
        self.assertEqual(consumer.acknowledged, [])

    def test_it_reports_the_decision_per_message(self):
        """That report is the entire output an operator wants from a dry run."""
        stdout = StringIO()
        runner, _ = self.build([fixtures.broker_message(payload())], max_messages=1, dry_run=True, stdout=stdout)
        runner.run()
        self.assertIn("accept", stdout.getvalue())
        self.assertIn("network.events", stdout.getvalue())


class TestStartupValidation(TestCase):
    """What the command refuses to start with, before it opens a socket."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def assert_refuses(self, *expected, **settings):
        """Assert the command refuses to start, naming each expected fault."""
        with ingestion(**settings):
            with self.assertRaises(CommandError) as caught:
                call_command("eventconsumer", stdout=StringIO(), stderr=StringIO())
        message = str(caught.exception)
        for fragment in expected:
            self.assertIn(fragment, message)
        return message

    def test_an_invalid_configuration_is_refused(self):
        """And the process never reaches the broker."""
        self.assert_refuses(
            "does not compile",
            topics={
                "network.events": {
                    **TOPIC,
                    "rules": [{"name": "bad", "action": "drop", "when": {"host": "^(unclosed"}}],
                }
            },
        )

    def test_a_missing_default_event_type_is_refused(self):
        """The fault only a query can find, checked at startup like the rest."""
        self.assert_refuses(
            "does not exist",
            topics={"network.events": {**TOPIC, "defaults": {"event_type": "No Such Type"}}},
        )

    def test_no_topics_configured_is_refused(self):
        """Starting a consumer that subscribes to nothing is never what was meant."""
        self.assert_refuses("nothing to consume", topics={})

    def test_an_unknown_consumer_is_refused(self):
        """With the names that do exist, since that is the next thing the operator needs."""
        self.assert_refuses("rabbitmq", "kafka", "redis", consumer="rabbitmq")

    def test_naming_an_unconfigured_topic_is_refused(self):
        """`--topics` with a typo would otherwise consume nothing, silently."""
        with ingestion():
            with self.assertRaises(CommandError) as caught:
                call_command("eventconsumer", topics="netwrok.events", stdout=StringIO(), stderr=StringIO())
        self.assertIn("netwrok.events", str(caught.exception))


class TestCommandWiring(TestCase):
    """The command builds the right consumer and reports what it is doing."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def run_command(self, **kwargs):
        """Run the command with the loop stubbed out, returning what it printed."""
        stdout = StringIO()
        with ingestion(**kwargs.pop("settings", {})):
            with mock.patch.object(ConsumerRunner, "run", return_value=None):
                call_command("eventconsumer", stdout=stdout, stderr=StringIO(), **kwargs)
        return stdout.getvalue()

    def test_the_banner_names_what_an_operator_would_have_to_ask_for(self):
        """Which implementation, whether it can replay, and which topics."""
        output = self.run_command()
        self.assertIn("RedisEventConsumer", output)
        self.assertIn("replay unsupported", output)
        self.assertIn("network.events", output)

    def test_the_consumer_can_be_overridden_on_the_command_line(self):
        """For trying Kafka against a lab broker without editing settings."""
        output = self.run_command(consumer="kafka")
        self.assertIn("KafkaEventConsumer", output)
        self.assertIn("replay supported", output)

    def test_an_unrecoverable_failure_exits_with_its_own_code(self):
        """A supervisor distinguishes 'could not carry on' from 'refused to start'."""
        with ingestion():
            with mock.patch.object(ConsumerRunner, "run", side_effect=UnrecoverableError("database is gone")):
                with self.assertRaises(CommandError) as caught:
                    call_command("eventconsumer", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(caught.exception.returncode, EXIT_UNRECOVERABLE)
        self.assertIn("database is gone", str(caught.exception))
