"""The deterministic pre-filter: what becomes a ticket, and what only becomes a number.

Rules run cheapest first, and the first one to decide wins. Nothing here calls a language model or
touches the network; the most expensive thing it does is a cached lookup of the event type
catalogue. Phase 3's triage runs after this, on what survives, so that noise never costs a token.
"""

import time
from dataclasses import dataclass

from nautobot_event_tracker.choices import SEVERITY_WEIGHTS
from nautobot_event_tracker.ingestion.constants import (
    ACTION_ACCEPT,
    ACTION_DROP,
    REASON_BELOW_SEVERITY_FLOOR,
    REASON_EVENT_TYPE_DISABLED,
    REASON_RATE_LIMITED,
    REASON_UNKNOWN_EVENT_TYPE,
    UNKNOWN_EVENT_TYPE_DROP,
)
from nautobot_event_tracker.ingestion.normalize import effective_severity, resolve_path


@dataclass(frozen=True)
class Decision:
    """What to do with a message, and the counter key that explains why.

    Phase 3's LLM triage returns this same type, so the pipeline's call site does not change when
    triage arrives - only what stands between the pre-filter and the ticket.
    """

    action: str
    reason: str = ""


@dataclass(frozen=True)
class FilterResult:
    """A decision, plus the event type it was reached through when there is one."""

    decision: Decision
    event_type: object = None


ACCEPT = Decision(ACTION_ACCEPT)


class EventTypeCache:
    """The event type catalogue, re-read occasionally rather than per message.

    F2 and F3 both need it, and two queries in front of every event is a poor trade for a table of
    ten rows that changes monthly. The cost is stated rather than hidden: disabling an event type
    takes up to `ttl_seconds` to take effect on a running consumer.
    """

    def __init__(self, *, ttl_seconds, clock=time.monotonic):
        """Hold the catalogue for this long between reads."""
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._by_name = {}
        self._read_at = None

    def get(self, name):
        """Return the event type of this name, or None."""
        if self._read_at is None or self._clock() - self._read_at >= self.ttl_seconds:
            self.refresh()
        return self._by_name.get(name)

    def refresh(self):
        """Re-read the catalogue now."""
        from nautobot_event_tracker.models import EventType  # pylint: disable=import-outside-toplevel

        self._by_name = {event_type.name: event_type for event_type in EventType.objects.all()}
        self._read_at = self._clock()


class TokenBucket:
    """A rate limit that admits bursts but not floods.

    Per process, so N instances admit N times as many - documented rather than coordinated, because
    a shared limit means a distributed rate limiter, which is real work to build before anyone has
    hit the limit.
    """

    def __init__(self, *, per_minute, burst, clock=time.monotonic):
        """Start full, so a quiet consumer is not throttled the moment traffic arrives."""
        self.per_second = per_minute / 60.0
        self.burst = burst
        self._clock = clock
        self._tokens = float(burst)
        self._checked_at = clock()

    def take(self):
        """Spend one token, or report that there was none to spend."""
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._checked_at) * self.per_second)
        self._checked_at = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True


class PreFilter:
    """Rules F1 to F6, holding the state they need between messages."""

    def __init__(self, config, *, clock=time.monotonic):
        """Build the caches and buckets the rules need."""
        self.config = config
        self.event_types = EventTypeCache(ttl_seconds=config.event_type_cache_seconds, clock=clock)
        self._buckets = {
            name: TokenBucket(per_minute=topic.rate_limit.per_minute, burst=topic.rate_limit.burst, clock=clock)
            for name, topic in config.topics.items()
            if topic.rate_limit is not None
        }

    def topic(self, name):
        """F1 - the topic's configuration, or None when it has none."""
        return self.config.topics.get(name)

    def decide(self, event, topic_config):
        """Rules F2 to F6, in order, on an event whose topic is already known."""
        event_type = self.event_types.get(event.event_type_name)

        # F2 - an event type we do not know about, per the topic's policy.
        if event_type is None:
            fallback = topic_config.defaults.get("event_type")
            if topic_config.unknown_event_type == UNKNOWN_EVENT_TYPE_DROP or not fallback:
                return FilterResult(Decision(ACTION_DROP, REASON_UNKNOWN_EVENT_TYPE))
            event_type = self.event_types.get(fallback)
            if event_type is None:
                return FilterResult(Decision(ACTION_DROP, REASON_UNKNOWN_EVENT_TYPE))

        # F3 - a disabled type is a type an operator has said to stop making tickets for.
        if not event_type.enabled:
            return FilterResult(Decision(ACTION_DROP, REASON_EVENT_TYPE_DISABLED), event_type)

        # F4 - the severity floor, weighed on the app's own scale rather than alphabetically.
        if topic_config.minimum_severity:
            severity = effective_severity(event, event_type)
            if SEVERITY_WEIGHTS[severity] < SEVERITY_WEIGHTS[topic_config.minimum_severity]:
                return FilterResult(Decision(ACTION_DROP, REASON_BELOW_SEVERITY_FLOOR), event_type)

        # F5 - the operator's own rules, in the order they wrote them.
        rule = self._first_matching_rule(topic_config, event.payload)
        if rule is not None:
            return FilterResult(Decision(rule.action, rule.name), event_type)

        # F6 - the flood guard, taken last so a dropped message has not already cost a token.
        bucket = self._buckets.get(topic_config.name)
        if bucket is not None and not bucket.take():
            return FilterResult(Decision(ACTION_DROP, REASON_RATE_LIMITED), event_type)

        return FilterResult(ACCEPT, event_type)

    @classmethod
    def _first_matching_rule(cls, topic_config, payload):
        """The first rule all of whose clauses match, or None."""
        for rule in topic_config.rules:
            if all(cls._clause_matches(payload, path, pattern) for path, pattern in rule.when):
                return rule
        return None

    @staticmethod
    def _clause_matches(payload, path, pattern):
        """One clause: the path has to resolve, and its value has to match.

        A path that resolves to nothing does not match, rather than matching an empty string - a
        rule about a field the payload does not carry should not fire on every payload that omits
        it.
        """
        value = resolve_path(payload, path)
        return value is not None and pattern.search(str(value)) is not None
