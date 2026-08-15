"""Test the counters: their arithmetic, their buckets, and their housekeeping.

Both clocks are injected. Bucket rollover and retention are things that happen over hours, and a
test that waited for them would not be a test.
"""

from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from unittest import mock

from django.db import DatabaseError
from django.test import TestCase

from nautobot_event_tracker.ingestion.stats import StatsRecorder
from nautobot_event_tracker.models import IngestionStats
from nautobot_event_tracker.tests import fixtures

TOPIC = "network.events"


class StatsTestCase(TestCase):
    """A recorder with both clocks under the test's control."""

    def setUp(self):
        """Build the recorder."""
        super().setUp()
        self.clock = fixtures.FakeClock()
        self.wall = fixtures.FakeWallClock()
        self.recorder = self.build()

    def build(self, **overrides):
        """A recorder, with this test's clocks."""
        settings = {
            "consumer_name": "consumer-1",
            "bucket_seconds": 300,
            "flush_seconds": 10,
            "retention_days": 30,
            "clock": self.clock,
            "now": self.wall,
        }
        return StatsRecorder(**{**settings, **overrides})

    def row(self, **kwargs):
        """The single counter row, or the one matching these terms."""
        return IngestionStats.objects.get(**kwargs) if kwargs else IngestionStats.objects.get()


class TestArithmetic(StatsTestCase):
    """What a flush writes."""

    def test_a_flush_writes_what_was_counted(self):
        """The ordinary case."""
        self.recorder.record(TOPIC, received=1, opened=1)
        self.recorder.flush()
        row = self.row()
        self.assertEqual(row.received, 1)
        self.assertEqual(row.tickets_opened, 1)

    def test_a_second_flush_adds_rather_than_overwrites(self):
        """`F()` expressions, so the row accumulates across the process's lifetime."""
        for _ in range(3):
            self.recorder.record(TOPIC, received=1, opened=1)
            self.recorder.flush()
        row = self.row()
        self.assertEqual(row.received, 3)
        self.assertEqual(row.tickets_opened, 3)

    def test_counting_many_messages_writes_one_row(self):
        """The write rate is bounded by the flush interval, not the message rate."""
        for _ in range(100):
            self.recorder.record(TOPIC, received=1, opened=1)
        self.recorder.flush()
        self.assertEqual(IngestionStats.objects.count(), 1)
        self.assertEqual(self.row().received, 100)

    def test_drop_reasons_accumulate_by_key(self):
        """This is the map an operator reads to find what ate their events."""
        self.recorder.record(TOPIC, received=1, drop_reason="lab-estate")
        self.recorder.record(TOPIC, received=1, drop_reason="lab-estate")
        self.recorder.record(TOPIC, received=1, drop_reason="below_severity_floor")
        self.recorder.flush()
        row = self.row()
        self.assertEqual(row.drops_by_reason, {"lab-estate": 2, "below_severity_floor": 1})
        self.assertEqual(row.dropped, 3)

    def test_drop_reasons_merge_across_flushes(self):
        """A reason counted before the flush and after it is still one key."""
        self.recorder.record(TOPIC, received=1, drop_reason="lab-estate")
        self.recorder.flush()
        self.recorder.record(TOPIC, received=1, drop_reason="lab-estate")
        self.recorder.flush()
        self.assertEqual(self.row().drops_by_reason, {"lab-estate": 2})

    def test_the_counting_invariant_holds(self):
        """Every message ends in exactly one of four places."""
        self.recorder.record(TOPIC, received=1, opened=1)
        self.recorder.record(TOPIC, received=1, joined=1)
        self.recorder.record(TOPIC, received=1, drop_reason="lab-estate")
        self.recorder.record(TOPIC, received=1, errored=1)
        self.recorder.flush()
        row = self.row()
        self.assertEqual(row.received, 4)
        self.assertEqual(row.received, row.accounted_for)

    def test_suppressed_is_not_a_fifth_place(self):
        """A suppressed message opened a ticket, and is counted there too."""
        self.recorder.record(TOPIC, received=1, opened=1, suppressed=1)
        self.recorder.flush()
        row = self.row()
        self.assertEqual(row.suppressed, 1)
        self.assertEqual(row.received, row.accounted_for)

    def test_the_newest_message_time_never_goes_backwards(self):
        """Out-of-order delivery must not make the page report an older 'last seen'."""
        newer = datetime(2026, 8, 15, 3, 20, tzinfo=datetime_timezone.utc)
        older = datetime(2026, 8, 15, 3, 10, tzinfo=datetime_timezone.utc)
        self.recorder.record(TOPIC, received=1, message_time=newer)
        self.recorder.record(TOPIC, received=1, message_time=older)
        self.recorder.flush()
        self.assertEqual(self.row().last_message_at, newer)

    def test_a_later_flush_advances_the_newest_message_time(self):
        """And it does advance when there is something newer."""
        first = datetime(2026, 8, 15, 3, 15, tzinfo=datetime_timezone.utc)
        second = datetime(2026, 8, 15, 3, 16, tzinfo=datetime_timezone.utc)
        self.recorder.record(TOPIC, received=1, message_time=first)
        self.recorder.flush()
        self.recorder.record(TOPIC, received=1, message_time=second)
        self.recorder.flush()
        self.assertEqual(self.row().last_message_at, second)

    def test_nothing_counted_writes_no_row(self):
        """An idle consumer should not litter the table with empty buckets."""
        self.recorder.flush()
        self.assertFalse(IngestionStats.objects.exists())


class TestFlushTiming(StatsTestCase):
    """When a flush happens."""

    def test_no_flush_before_the_interval(self):
        """The whole point is to bound the write rate."""
        self.recorder.record(TOPIC, received=1)
        self.recorder.maybe_flush()
        self.assertFalse(IngestionStats.objects.exists())

    def test_a_flush_once_the_interval_passes(self):
        """And it does happen, without waiting for traffic to stop."""
        self.recorder.record(TOPIC, received=1)
        self.clock.advance(10)
        self.recorder.maybe_flush()
        self.assertEqual(self.row().received, 1)

    def test_counts_are_not_written_twice(self):
        """Pending counts are handed over to the write, not copied."""
        self.recorder.record(TOPIC, received=1)
        self.recorder.flush()
        self.recorder.flush()
        self.assertEqual(self.row().received, 1)

    def test_a_failed_write_does_not_take_the_consumer_down(self):
        """A ticket written and a count lost beats the reverse."""
        self.recorder.record(TOPIC, received=1)
        with mock.patch.object(StatsRecorder, "_write", side_effect=DatabaseError("gone")):
            self.recorder.flush()
        self.assertFalse(IngestionStats.objects.exists())


class TestBuckets(StatsTestCase):
    """Which row a count lands in."""

    def test_a_bucket_is_floored_to_its_width(self):
        """Floored on the epoch, so two processes agree on where a bucket starts."""
        bucket = self.recorder.bucket_for(datetime(2026, 8, 15, 3, 14, 59, tzinfo=datetime_timezone.utc))
        self.assertEqual(bucket, datetime(2026, 8, 15, 3, 10, tzinfo=datetime_timezone.utc))

    def test_counts_in_one_window_share_a_row(self):
        """Half a minute apart, both inside the window starting at 03:10."""
        self.recorder.record(TOPIC, received=1)
        self.wall.advance(seconds=30)
        self.recorder.record(TOPIC, received=1)
        self.recorder.flush()
        self.assertEqual(IngestionStats.objects.count(), 1)
        self.assertEqual(self.row().received, 2)

    def test_crossing_a_boundary_starts_a_new_row(self):
        """Which is what makes throughput visible rather than a single running total."""
        self.recorder.record(TOPIC, received=1)
        self.wall.advance(minutes=6)
        self.recorder.record(TOPIC, received=1)
        self.recorder.flush()
        self.assertEqual(IngestionStats.objects.count(), 2)

    def test_each_topic_gets_its_own_row(self):
        """'Which topic is quiet' is a question the page has to answer."""
        self.recorder.record(TOPIC, received=1)
        self.recorder.record("other.events", received=1)
        self.recorder.flush()
        self.assertEqual(IngestionStats.objects.count(), 2)

    def test_two_consumers_write_disjoint_rows(self):
        """Instances never contend, because the consumer name is part of the key."""
        other = self.build(consumer_name="consumer-2")
        self.recorder.record(TOPIC, received=1)
        other.record(TOPIC, received=2)
        self.recorder.flush()
        other.flush()
        self.assertEqual(self.row(consumer_name="consumer-1").received, 1)
        self.assertEqual(self.row(consumer_name="consumer-2").received, 2)


class TestRetention(StatsTestCase):
    """Housekeeping, done by the process that made the mess."""

    def test_rows_past_retention_are_pruned(self):
        """No scheduled job needed when the writer can clean up behind itself."""
        IngestionStats.objects.create(
            consumer_name="consumer-1",
            topic=TOPIC,
            bucket_start=self.wall() - timedelta(days=31),
        )
        self.recorder.record(TOPIC, received=1)
        self.recorder.flush()
        self.assertEqual(IngestionStats.objects.count(), 1)
        self.assertGreater(self.row().bucket_start, self.wall() - timedelta(days=1))

    def test_rows_within_retention_are_kept(self):
        """Thirty days of history is the point of keeping any."""
        IngestionStats.objects.create(
            consumer_name="consumer-1",
            topic=TOPIC,
            bucket_start=self.wall() - timedelta(days=29),
        )
        self.recorder.record(TOPIC, received=1)
        self.recorder.flush()
        self.assertEqual(IngestionStats.objects.count(), 2)

    def test_another_consumers_old_rows_are_pruned_too(self):
        """An instance that has stopped running must not leave its counters forever."""
        IngestionStats.objects.create(
            consumer_name="consumer-2",
            topic=TOPIC,
            bucket_start=self.wall() - timedelta(days=31),
        )
        self.recorder.record(TOPIC, received=1)
        self.recorder.flush()
        self.assertEqual(IngestionStats.objects.filter(consumer_name="consumer-2").count(), 0)

    def test_pruning_happens_once_per_bucket(self):
        """Ten flushes a bucket should not mean ten delete queries."""
        self.recorder.record(TOPIC, received=1)
        self.recorder.flush()
        with mock.patch.object(IngestionStats.objects, "filter", wraps=IngestionStats.objects.filter) as filtering:
            self.recorder.record(TOPIC, received=1)
            self.recorder.flush()
        self.assertFalse(
            any("bucket_start__lt" in str(call) for call in filtering.call_args_list),
            "pruned twice within one bucket",
        )
