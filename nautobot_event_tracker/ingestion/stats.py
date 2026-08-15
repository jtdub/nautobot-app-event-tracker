"""Counting what the consumer saw, without a database write per message.

Counts accumulate in memory and are written on a timer, so the write rate is bounded by the flush
interval no matter how fast messages arrive. A hard kill loses at most one interval's counts, which
is the right trade for a counter and the wrong one for a ticket - which is why tickets are written
inside the message's own transaction and these are not.
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone

from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Buckets are stamped in UTC; the UI renders them in the viewer's timezone.
utc = datetime_timezone.utc


@dataclass
class Counts:  # pylint: disable=too-many-instance-attributes
    """One bucket's worth of counts, before they reach the database."""

    received: int = 0
    errored: int = 0
    dropped: int = 0
    tickets_opened: int = 0
    tickets_joined: int = 0
    suppressed: int = 0
    drops_by_reason: dict = field(default_factory=dict)
    last_message_at: object = None

    def add_drop(self, reason):
        """Record a drop under the rule or filter that refused it."""
        self.dropped += 1
        self.drops_by_reason[reason] = self.drops_by_reason.get(reason, 0) + 1

    def saw_message_at(self, when):
        """Advance the newest-message time, which never goes backwards."""
        if when is not None and (self.last_message_at is None or when > self.last_message_at):
            self.last_message_at = when


class StatsRecorder:  # pylint: disable=too-many-instance-attributes
    """Accumulates counts and writes them to `IngestionStats` on a timer.

    The clock is injected in two pieces because the two jobs need different clocks: `monotonic`
    decides when to flush, since it cannot jump backwards, and wall time decides which bucket a
    count belongs to, since that is what a person reads off the page.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self, *, consumer_name, bucket_seconds, flush_seconds, retention_days, clock=time.monotonic, now=timezone.now
    ):
        """Hold the shape of the buckets and how often to write them."""
        self.consumer_name = consumer_name
        self.bucket_seconds = bucket_seconds
        self.flush_seconds = flush_seconds
        self.retention_days = retention_days
        self._clock = clock
        self._now = now
        self._counts = defaultdict(Counts)
        self._flushed_at = clock()
        self._pruned_bucket = None

    def bucket_for(self, when):
        """The start of the window this moment falls in.

        Floored on the epoch rather than on the hour, so buckets line up across processes started
        at different times and a flush from either finds the same row.
        """
        epoch_seconds = int(when.timestamp())
        return datetime.fromtimestamp(epoch_seconds - (epoch_seconds % self.bucket_seconds), tz=utc)

    def record(  # pylint: disable=too-many-arguments
        self, topic, *, received=0, errored=0, opened=0, joined=0, suppressed=0, drop_reason=None, message_time=None
    ):
        """Add to the current bucket's counts for this topic."""
        counts = self._counts[(topic, self.bucket_for(self._now()))]
        counts.received += received
        counts.errored += errored
        counts.tickets_opened += opened
        counts.tickets_joined += joined
        counts.suppressed += suppressed
        if drop_reason is not None:
            counts.add_drop(drop_reason)
        counts.saw_message_at(message_time)

    def maybe_flush(self):
        """Write the counts if the interval has elapsed. Called every time round the loop."""
        if self._clock() - self._flushed_at >= self.flush_seconds:
            self.flush()

    def flush(self):
        """Write every pending bucket, then prune anything past its retention.

        Numeric counts are applied with `F()` expressions, so a flush adds to whatever is in the
        row rather than overwriting it. Rows are keyed by consumer name, so two instances never
        write the same row and never contend.
        """
        self._flushed_at = self._clock()
        pending, self._counts = self._counts, defaultdict(Counts)

        for (topic, bucket_start), counts in sorted(pending.items()):
            try:
                self._write(topic, bucket_start, counts)
            except Exception:  # pylint: disable=broad-except
                # Counters must never take the consumer down: a ticket that was written and a
                # count that was lost is a far better outcome than the reverse.
                logger.exception("Could not write ingestion stats for %s at %s", topic, bucket_start)

        self._prune()

    def _write(self, topic, bucket_start, counts):
        """Add one bucket's counts to its row, creating the row if this is its first flush."""
        with transaction.atomic():
            from nautobot_event_tracker.models import IngestionStats  # pylint: disable=import-outside-toplevel

            row, _ = IngestionStats.objects.get_or_create(
                consumer_name=self.consumer_name,
                topic=topic,
                bucket_start=bucket_start,
            )
            merged = dict(row.drops_by_reason or {})
            for reason, count in counts.drops_by_reason.items():
                merged[reason] = merged.get(reason, 0) + count

            IngestionStats.objects.filter(pk=row.pk).update(
                received=F("received") + counts.received,
                errored=F("errored") + counts.errored,
                dropped=F("dropped") + counts.dropped,
                tickets_opened=F("tickets_opened") + counts.tickets_opened,
                tickets_joined=F("tickets_joined") + counts.tickets_joined,
                suppressed=F("suppressed") + counts.suppressed,
                drops_by_reason=merged,
                last_message_at=_newest(row.last_message_at, counts.last_message_at),
            )

    def _prune(self):
        """Delete rows past their retention, at most once per bucket.

        Retention needs no scheduled job of its own when the process that writes the rows can clean
        up behind itself. Pruning covers every consumer's rows, not just this one's, so that an
        instance that has stopped running does not leave its counters behind forever.
        """
        bucket = self.bucket_for(self._now())
        if self._pruned_bucket == bucket:
            return
        self._pruned_bucket = bucket

        from nautobot_event_tracker.models import IngestionStats  # pylint: disable=import-outside-toplevel

        cutoff = self._now() - timedelta(days=self.retention_days)
        deleted, _ = IngestionStats.objects.filter(bucket_start__lt=cutoff).delete()
        if deleted:
            logger.info("Pruned %s ingestion stats rows older than %s", deleted, cutoff)


def _newest(*times):
    """The latest of these times, ignoring the ones that are not set."""
    known = [when for when in times if when is not None]
    return max(known) if known else None
