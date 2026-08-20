"""Run the event consumer: a long-lived process, supervised like Nautobot's own.

Not a Job and not a Celery task. A Job is a unit of work with an end, and one JobResult for a
process that runs for weeks means nothing; a Celery task that never returns holds a worker slot
forever and starves everything else. See ADR 0005.
"""

import logging
import signal
import sys
import time

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection

from nautobot_event_tracker.ingestion import config as ingestion_config
from nautobot_event_tracker.ingestion.consumers import get_consumer_class
from nautobot_event_tracker.ingestion.pipeline import handle_message
from nautobot_event_tracker.ingestion.prefilter import PreFilter
from nautobot_event_tracker.ingestion.stats import NullStatsRecorder, StatsRecorder
from nautobot_event_tracker.ingestion.triage import TriageFilter

logger = logging.getLogger(__name__)

#: What a supervisor sees when the process could not carry on, as opposed to 1 for a refused
#: configuration and 0 for a shutdown it was asked for.
EXIT_UNRECOVERABLE = 2

#: Backoff between attempts at a message whose database write failed, in seconds.
RETRY_BACKOFF_SECONDS = 1.0

#: Backoff between attempts to reach a broker that has gone away: doubling from one second to a
#: minute, then holding there. A broker outage means no messages are arriving and nothing is at
#: stake in waiting, so this retries forever rather than exiting - unlike a database failure,
#: where messages are arriving with nowhere to put them.
RECONNECT_BACKOFF_SECONDS = 1.0
RECONNECT_BACKOFF_MAX_SECONDS = 60.0


class UnrecoverableError(Exception):
    """A failure the process cannot carry on through, and must not acknowledge past."""


def _drop_a_broken_connection():
    """Discard the database connection when it is the thing that broke.

    Retrying on a connection the server has already dropped just fails again. Guarded on
    `in_atomic_block` because a connection inside an enclosing transaction is not ours to close -
    which is the case under Django's test runner, where each test is wrapped in one.
    """
    if not connection.in_atomic_block:
        connection.close_if_unusable_or_obsolete()


class ConsumerRunner:  # pylint: disable=too-many-instance-attributes
    """The loop: poll, handle, acknowledge, flush, repeat.

    Kept as a class rather than a method so tests can drive it with a fake consumer and a fake
    clock, without installing signal handlers or going through `call_command`.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        consumer,
        settings,
        rules=None,
        recorder=None,
        triage=None,
        dry_run=False,
        max_messages=None,
        sleep=time.sleep,
        stdout=None,
    ):
        """Hold the pieces; nothing connects until `run()`."""
        self.consumer = consumer
        self.settings = settings
        self.rules = rules if rules is not None else PreFilter(settings)
        self.dry_run = dry_run
        self.recorder = recorder if recorder is not None else self._build_recorder(settings, dry_run)
        self.triage = triage if triage is not None else self._build_triage(settings, dry_run)
        self.max_messages = max_messages
        self._sleep = sleep
        self._stdout = stdout
        self.handled = 0
        self.stopping = False

    @staticmethod
    def _build_recorder(settings, dry_run):
        """The recorder this run needs: a real one, or one that counts nothing."""
        if dry_run:
            return NullStatsRecorder()
        return StatsRecorder(
            consumer_name=settings.consumer_name,
            bucket_seconds=settings.stats_bucket_seconds,
            flush_seconds=settings.stats_flush_seconds,
            retention_days=settings.stats_retention_days,
        )

    @staticmethod
    def _build_triage(settings, dry_run):
        """The triage step, or None when it must not run.

        None on a dry run whatever the configuration says (T9): a dry run writes nothing, rule L1
        forbids an unrecorded model call, so the model cannot be consulted.
        """
        if dry_run or not settings.triage.enabled:
            return None
        return TriageFilter(settings)

    def stop(self):
        """Ask the loop to finish the message in flight and come back."""
        self.stopping = True

    def run(self):
        """Consume until asked to stop, or until `max_messages` have been handled.

        Counters are flushed on the way out however the loop ends, so a clean shutdown does not
        throw away the interval it was in the middle of.
        """
        poll_timeout = self.settings.poll_timeout_seconds
        try:
            with self.consumer:
                while not self.stopping:
                    message = self._poll(poll_timeout)
                    if message is not None:
                        self._handle(message)
                        self.handled += 1
                        if self.max_messages is not None and self.handled >= self.max_messages:
                            break
                    self.recorder.maybe_flush()
        finally:
            self.recorder.flush()

    def _poll(self, timeout):
        """Ask the broker for a message, reconnecting for as long as it takes.

        Every broker client has its own exception hierarchy, and `confluent_kafka` and `redis` do
        not share one, so this catches broadly on purpose: whatever went wrong with the connection,
        the answer is the same.
        """
        backoff = RECONNECT_BACKOFF_SECONDS
        while not self.stopping:
            try:
                return self.consumer.poll(timeout)
            except Exception as error:  # pylint: disable=broad-except
                logger.warning("Lost the broker connection (%s); reconnecting in %ss", error, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)
                try:
                    self.consumer.reconnect()
                except Exception:  # pylint: disable=broad-except
                    logger.warning("Could not reconnect to the broker; will try again", exc_info=True)
        return None

    def _handle(self, message):
        """Handle one message, retrying a transient database failure before giving up.

        I1 - the message is acknowledged only after its ticket has committed, so a crash in between
        redelivers rather than loses, and rule S5 turns the duplicate into a recurrence.

        I5 - if the database will not take it after `max_retries`, the process gives up without
        acknowledging. Exiting is the honest response: the message is still on the broker and a
        supervisor will restart us, where acknowledging a message whose ticket was never written
        would lose it quietly.
        """
        # Counted once, out here: a retry re-enters `handle_message`, and a message counted twice
        # would break the invariant that received equals the outcomes.
        self.recorder.record(message.topic, received=1, message_time=message.timestamp)

        for attempt in range(1, self.settings.max_retries + 1):
            try:
                decision = handle_message(
                    message,
                    rules=self.rules,
                    recorder=self.recorder,
                    config=self.settings,
                    triage=self.triage,
                    write=not self.dry_run,
                )
                break
            except DatabaseError as error:
                _drop_a_broken_connection()
                if attempt >= self.settings.max_retries:
                    raise UnrecoverableError(
                        f"Giving up on a message from {message.topic} after {attempt} attempts: {error}. "
                        "It has not been acknowledged and will be redelivered."
                    ) from error
                logger.warning("Database error handling a message (attempt %s): %s", attempt, error)
                self._sleep(RETRY_BACKOFF_SECONDS * attempt)

        if self.dry_run:
            line = f"{message.topic}: {decision.action} {decision.reason}".rstrip()
            if self.settings.triage.enabled:
                # T9 - say that the model was not consulted, so a dry run's output is not read as
                # what triage would have decided.
                line += " (triage skipped: dry run)"
            self._report(line)
            return

        self.consumer.acknowledge(message)

    def _report(self, line):
        """Say something to whoever is watching, if anyone is."""
        if self._stdout is not None:
            self._stdout.write(line)


class Command(BaseCommand):
    """Consume network events from a broker and turn them into tickets."""

    help = __doc__

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument("--consumer", help="Override the configured broker implementation.")
        parser.add_argument("--topics", help="Comma-separated subset of the configured topics.")
        parser.add_argument("--max-messages", type=int, help="Exit cleanly after handling this many messages.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Decide and report, writing no ticket and no counter, and acknowledging nothing.",
        )

    def handle(self, *args, **options):
        """Validate the configuration, then run the loop until it is asked to stop."""
        settings = self._load(options)
        consumer_class = get_consumer_class(settings.consumer)

        consumer = consumer_class(settings=settings.consumer_settings, topics=settings.topic_names)
        runner = ConsumerRunner(
            consumer=consumer,
            settings=settings,
            dry_run=options["dry_run"],
            max_messages=options.get("max_messages"),
            stdout=self.stdout,
        )

        self._banner(settings, consumer, dry_run=options["dry_run"])
        self._install_signal_handlers(runner)

        try:
            runner.run()
        except UnrecoverableError as error:
            raise CommandError(str(error), returncode=EXIT_UNRECOVERABLE) from error
        except ImproperlyConfigured as error:
            # Connecting can find faults the settings alone could not - a missing client library, a
            # named external integration that no longer exists.
            raise CommandError(str(error)) from error

        self.stdout.write(self.style.SUCCESS(f"Stopped after handling {runner.handled} messages."))

    @staticmethod
    def _load(options):
        """Parse and validate the configuration, refusing to start on any fault.

        Everything answerable from the settings is checked in one pass, so a deployment with three
        faults sees three lines and needs one restart. The database check runs afterwards because
        it needs a query; its faults are rendered the same way. A dry run skips the triage half of
        it, because a dry run makes no model call to be wrong about.
        """
        topics = [topic.strip() for topic in options["topics"].split(",")] if options.get("topics") else None
        try:
            settings = ingestion_config.load(
                topics=topics,
                consumer=options.get("consumer"),
                require_topics=True,
            )
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from error

        problems = ingestion_config.database_problems(settings, check_triage=not options["dry_run"])
        if problems:
            raise CommandError(ingestion_config.render_problems(problems))
        return settings

    def _banner(self, settings, consumer, dry_run=False):
        """One line naming everything an operator would otherwise have to ask for."""
        if settings.triage.enabled and dry_run:
            # T9 - a dry run consults no model whatever the configuration says, and a banner that
            # named the model would promise decisions nothing is going to make.
            triage = "triage skipped (dry run)"
        elif settings.triage.enabled:
            triage = f"triage {settings.triage.provider}:{settings.triage.model}"
        else:
            triage = "triage off"
        self.stdout.write(
            f"Event Tracker consumer '{settings.consumer_name}' starting: "
            f"{type(consumer).__name__}, replay {'supported' if consumer.supports_replay else 'unsupported'}, "
            f"topics {', '.join(settings.topic_names)}, {triage}"
        )

    def _install_signal_handlers(self, runner):
        """Stop politely on the first signal, and immediately on the second.

        An operator who signals twice means it, and a process that ignores the second one is the
        reason people reach for `kill -9`.
        """

        def handler(signal_number, frame):  # pylint: disable=unused-argument
            if runner.stopping:
                self.stderr.write("Second signal received; exiting now.")
                sys.exit(1)
            self.stdout.write("Shutting down: finishing the message in flight.")
            runner.stop()

        for signal_number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signal_number, handler)
