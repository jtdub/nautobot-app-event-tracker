"""Test the consumer process: what it refuses to start with, and how it stops.

The loop is driven directly through `ConsumerRunner` with a fake consumer, so nothing here waits
on a broker or on a signal being delivered for real.
"""

from io import StringIO
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import TestCase

from nautobot_event_tracker.ingestion import config
from nautobot_event_tracker.management.commands.eventconsumer import (
    EXIT_UNRECOVERABLE,
    ConsumerRunner,
    UnrecoverableError,
)
from nautobot_event_tracker.models import EventTicket, IngestionStats
from nautobot_event_tracker.tests import fixtures

TOPIC = fixtures.INGESTION_TOPIC


def ingestion(**overrides):
    """A PLUGINS_CONFIG override for these tests, defaulting to the Redis consumer."""
    return fixtures.ingestion_settings(**{"consumer": "redis", **overrides})


payload = fixtures.event_payload

#: The one settings override the triage-wiring tests need.
TRIAGE_ON = {"triage": fixtures.TRIAGE_SETTINGS}


class RunnerTestCase(TestCase):
    """A runner driven by a fake consumer."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def build(self, messages=(), *, settings_overrides=None, **kwargs):
        """A runner over these queued messages, on the default settings or the named overrides."""
        with ingestion(**(settings_overrides or {})):
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

    def test_a_retried_message_is_counted_as_received_once(self):
        """Counting it twice would break the invariant that received equals the outcomes."""
        runner, _ = self.build([fixtures.broker_message(payload())], max_messages=1)
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

        self.assertEqual(IngestionStats.objects.get().received, 1)

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


class TestBrokerLoss(RunnerTestCase):
    """A broker that goes away is waited for, not exited over."""

    def test_a_poll_failure_reconnects_and_carries_on(self):
        """Unlike a database failure, nothing is at stake in waiting: no messages are arriving."""
        runner, consumer = self.build([fixtures.broker_message(payload())], max_messages=1)
        failures = []

        original_poll = consumer.poll

        def flaky_poll(timeout):
            """Fail twice, then behave."""
            if len(failures) < 2:
                failures.append(1)
                raise ConnectionError("broker went away")
            return original_poll(timeout)

        consumer.poll = flaky_poll
        runner.run()

        self.assertEqual(len(failures), 2)
        self.assertEqual(runner.handled, 1)
        self.assertTrue(EventTicket.objects.exists())

    def test_reconnecting_reopens_the_connection(self):
        """Closing and reopening is the consumer's business; when to do it is the loop's."""
        runner, consumer = self.build([], max_messages=1)
        reconnects = []
        consumer.reconnect = lambda: reconnects.append(1)

        def failing_poll(timeout):  # pylint: disable=unused-argument
            """Fail once, then ask the loop to stop."""
            runner.stop()
            raise ConnectionError("broker went away")

        consumer.poll = failing_poll
        runner.run()
        self.assertEqual(len(reconnects), 1)


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

    def test_it_resolves_and_says_what_it_would_attach(self):
        """E1 - unlike triage, a dry run resolves, because these rules are what it is for.

        Named rather than counted: a rule pointing at the wrong field attaches something
        plausible, and a number would not show that.
        """
        device = fixtures.create_device("leaf-01")
        fixtures.create_interface(device)
        stdout = StringIO()
        runner, _ = self.build(
            [fixtures.broker_message(payload(interface="ethernet-1/1"))],
            settings_overrides={"topics": {"network.events": {**TOPIC, "resolve": fixtures.INGESTION_RESOLVE}}},
            max_messages=1,
            dry_run=True,
            stdout=stdout,
        )
        runner.run()

        self.assertIn("attaching leaf-01, ethernet-1/1", stdout.getvalue())
        self.assertFalse(EventTicket.objects.exists())


class TestStartupValidation(fixtures.RefusalAssertions, TestCase):
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
        return self.assert_names(str(caught.exception), expected)

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


class TestDryRunStartup(TestCase):
    """A dry run makes no model call, so it must not be refused for want of the means to make one."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()
        fixtures.create_llmmodel()

    def run_command(self, **kwargs):
        """Run the command with triage on, the `llm` extra absent, and the loop stubbed out."""
        from nautobot_event_tracker.services import llm as llm_service  # pylint: disable=import-outside-toplevel

        stdout = StringIO()
        with ingestion(**TRIAGE_ON):
            with mock.patch.object(
                llm_service, "require_client", side_effect=ImproperlyConfigured("litellm is not installed")
            ):
                with mock.patch.object(ConsumerRunner, "run", return_value=None):
                    call_command("eventconsumer", stdout=stdout, stderr=StringIO(), **kwargs)
        return stdout.getvalue()

    def test_a_dry_run_starts_without_the_llm_extra(self):
        """`_build_triage` returns None for every dry run (T9), so there is nothing to check.

        Refusing here would deny an operator the decide-only pass over live traffic that dry runs
        exist for, on a box that never intended to call a model.
        """
        output = self.run_command(dry_run=True)
        self.assertIn("triage skipped", output.lower())

    def test_a_real_run_is_still_refused(self):
        """The check that matters is the one before a run that will actually call a model."""
        from nautobot_event_tracker.services import llm as llm_service  # pylint: disable=import-outside-toplevel

        with ingestion(**TRIAGE_ON):
            with mock.patch.object(
                llm_service, "require_client", side_effect=ImproperlyConfigured("litellm is not installed")
            ):
                with self.assertRaises(CommandError) as caught:
                    call_command("eventconsumer", stdout=StringIO(), stderr=StringIO())
        self.assertIn("litellm is not installed", str(caught.exception))

    def test_a_dry_run_still_refuses_a_missing_event_type(self):
        """Only the triage half is skipped: a dry run reports decisions, and those name types."""
        with fixtures.ingestion_settings(
            consumer="redis",
            topics={"network.events": {**TOPIC, "defaults": {"event_type": "No Such Type"}}},
        ):
            with self.assertRaises(CommandError) as caught:
                call_command("eventconsumer", dry_run=True, stdout=StringIO(), stderr=StringIO())
        self.assertIn("does not exist", str(caught.exception))


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


class TestTriageWiring(RunnerTestCase):
    """How the runner builds - and refuses to build - its triage step."""

    def test_triage_defaults_to_none_when_disabled(self):
        """No configuration, no model, no collaborator."""
        runner, _ = self.build()
        self.assertIsNone(runner.triage)

    def test_an_enabled_configuration_builds_a_triage_filter(self):
        """The runner wires the step itself, from the same settings everything else uses."""
        from nautobot_event_tracker.ingestion.triage import TriageFilter  # pylint: disable=import-outside-toplevel

        runner, _ = self.build(settings_overrides=TRIAGE_ON)
        self.assertIsInstance(runner.triage, TriageFilter)

    def test_a_dry_run_never_builds_triage(self):
        """T9 - a dry run writes nothing, and rule L1 forbids an unrecorded model call."""
        runner, _ = self.build(settings_overrides=TRIAGE_ON, dry_run=True)
        self.assertIsNone(runner.triage)

    def test_a_dry_run_says_triage_was_skipped(self):
        """The printed decision must not read as what triage would have decided."""
        out = StringIO()
        runner, _ = self.build(
            messages=[fixtures.broker_message(payload())],
            settings_overrides=TRIAGE_ON,
            dry_run=True,
            max_messages=1,
            stdout=out,
        )
        runner.run()
        self.assertIn("(triage skipped: dry run)", out.getvalue())

    def test_a_plain_dry_run_line_is_unchanged(self):
        """A deployment without triage sees the Phase 2 line, word for word."""
        out = StringIO()
        runner, _ = self.build(messages=[fixtures.broker_message(payload())], dry_run=True, max_messages=1, stdout=out)
        runner.run()
        self.assertIn("network.events: accept", out.getvalue())
        self.assertNotIn("triage", out.getvalue())
