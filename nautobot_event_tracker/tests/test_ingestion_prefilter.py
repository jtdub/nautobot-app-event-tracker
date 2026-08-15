"""Test the pre-filter: what it refuses, in what order, and under which counter key.

Time is injected rather than slept on. A test suite that sleeps to prove a token bucket refills is
a test suite that fails on a busy CI runner for reasons that have nothing to do with the code.
"""

from django.test import TestCase, override_settings

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.ingestion import config, prefilter
from nautobot_event_tracker.ingestion.constants import (
    ACTION_ACCEPT,
    ACTION_DROP,
    ACTION_SUPPRESS,
    REASON_BELOW_SEVERITY_FLOOR,
    REASON_EVENT_TYPE_DISABLED,
    REASON_RATE_LIMITED,
    REASON_UNKNOWN_EVENT_TYPE,
    UNKNOWN_EVENT_TYPE_DROP,
)
from nautobot_event_tracker.ingestion.normalize import normalize
from nautobot_event_tracker.models import EventType
from nautobot_event_tracker.tests import fixtures

TOPIC = {
    "field_map": {"event_type": "event.type", "title": "message", "severity": "event.severity"},
    "defaults": {"event_type": "Test Interface Down"},
    "dedup_key_template": "{event.type}:{host}",
}


class FakeClock:
    """A clock a test moves by hand."""

    def __init__(self):
        """Start at zero."""
        self.now = 0.0

    def __call__(self):
        """Read the clock, as `time.monotonic` would."""
        return self.now

    def advance(self, seconds):
        """Move time forward."""
        self.now += seconds


def settings_with(topics):
    """Build a PLUGINS_CONFIG override carrying these topics."""
    return override_settings(PLUGINS_CONFIG={"nautobot_event_tracker": {"ingestion": {"topics": topics}}})


class PreFilterTestCase(TestCase):
    """Shared machinery: a catalogue, a clock, and a way to ask for a decision."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def setUp(self):
        """Give each test its own clock."""
        super().setUp()
        self.clock = FakeClock()

    def decide(self, payload, topic_settings=None, topic_name="network.events"):
        """Run the pre-filter over one payload, returning its result."""
        with settings_with({topic_name: {**TOPIC, **(topic_settings or {})}}):
            loaded = config.load()
        rules = prefilter.PreFilter(loaded, clock=self.clock)
        topic_config = rules.topic(topic_name)
        event = normalize(payload, topic_config=topic_config, max_payload_bytes=65536)
        return rules.decide(event, topic_config)

    @staticmethod
    def payload(**overrides):
        """A payload the pre-filter would ordinarily accept."""
        base = {
            "event": {"type": "Test Interface Down", "severity": SeverityChoices.MAJOR},
            "message": "Interface ethernet-1/1 is down",
            "host": "leaf-01",
        }
        base.update(overrides)
        return base


class TestTopicLookup(PreFilterTestCase):
    """F1 - a topic nobody configured."""

    def test_a_configured_topic_is_found(self):
        """The ordinary case."""
        with settings_with({"network.events": TOPIC}):
            rules = prefilter.PreFilter(config.load(), clock=self.clock)
        self.assertIsNotNone(rules.topic("network.events"))

    def test_an_unconfigured_topic_has_no_configuration(self):
        """The pipeline turns this into a drop, because there is nothing to normalize against."""
        with settings_with({"network.events": TOPIC}):
            rules = prefilter.PreFilter(config.load(), clock=self.clock)
        self.assertIsNone(rules.topic("something.else"))


class TestEventTypeRules(PreFilterTestCase):
    """F2 and F3 - the event type catalogue."""

    def test_a_known_enabled_type_is_accepted(self):
        """The ordinary case."""
        result = self.decide(self.payload())
        self.assertEqual(result.decision.action, ACTION_ACCEPT)
        self.assertEqual(result.event_type.name, "Test Interface Down")

    def test_an_unknown_type_falls_back_to_the_default(self):
        """An unclassified ticket beats a silent hole."""
        result = self.decide(self.payload(event={"type": "Nothing We Know About"}))
        self.assertEqual(result.decision.action, ACTION_ACCEPT)
        self.assertEqual(result.event_type.name, "Test Interface Down")

    def test_an_unknown_type_can_be_dropped_instead(self):
        """A deployment that would rather refuse what it cannot classify may."""
        result = self.decide(
            self.payload(event={"type": "Nothing We Know About"}),
            {"unknown_event_type": UNKNOWN_EVENT_TYPE_DROP},
        )
        self.assertEqual(result.decision.action, ACTION_DROP)
        self.assertEqual(result.decision.reason, REASON_UNKNOWN_EVENT_TYPE)

    def test_an_unknown_type_with_no_default_is_dropped(self):
        """There is nothing else to do with it."""
        result = self.decide(self.payload(event={"type": "Nothing We Know About"}), {"defaults": {}})
        self.assertEqual(result.decision.reason, REASON_UNKNOWN_EVENT_TYPE)

    def test_a_disabled_type_is_dropped(self):
        """Disabling a type is how an operator says 'stop making these tickets'."""
        result = self.decide(self.payload(event={"type": "Test Disabled Type"}))
        self.assertEqual(result.decision.action, ACTION_DROP)
        self.assertEqual(result.decision.reason, REASON_EVENT_TYPE_DISABLED)


class TestSeverityFloor(PreFilterTestCase):
    """F4 - the severity floor, weighed rather than compared alphabetically."""

    def test_a_severity_above_the_floor_is_accepted(self):
        """Major is above minor, which alphabetical ordering would get wrong."""
        payload = self.payload(event={"type": "Test Interface Down", "severity": SeverityChoices.MAJOR})
        result = self.decide(payload, {"minimum_severity": SeverityChoices.MINOR})
        self.assertEqual(result.decision.action, ACTION_ACCEPT)

    def test_a_severity_at_the_floor_is_accepted(self):
        """The floor is inclusive: 'at or above'."""
        payload = self.payload(event={"type": "Test Interface Down", "severity": SeverityChoices.MINOR})
        result = self.decide(payload, {"minimum_severity": SeverityChoices.MINOR})
        self.assertEqual(result.decision.action, ACTION_ACCEPT)

    def test_a_severity_below_the_floor_is_dropped(self):
        """Info under a minor floor is the case the floor exists for."""
        payload = self.payload(event={"type": "Test Interface Down", "severity": SeverityChoices.INFO})
        result = self.decide(payload, {"minimum_severity": SeverityChoices.MINOR})
        self.assertEqual(result.decision.action, ACTION_DROP)
        self.assertEqual(result.decision.reason, REASON_BELOW_SEVERITY_FLOOR)

    def test_the_event_types_default_severity_is_what_gets_compared(self):
        """A payload with no severity is still weighed, using the type's default."""
        payload = self.payload(event={"type": "Test Interface Down"})
        result = self.decide(payload, {"minimum_severity": SeverityChoices.CRITICAL})
        self.assertEqual(result.decision.reason, REASON_BELOW_SEVERITY_FLOOR)

    def test_no_floor_configured_admits_everything(self):
        """Most topics want no floor and should pay nothing for one."""
        payload = self.payload(event={"type": "Test Interface Down", "severity": SeverityChoices.INFO})
        self.assertEqual(self.decide(payload).decision.action, ACTION_ACCEPT)


class TestMatchRules(PreFilterTestCase):
    """F5 - the operator's own rules."""

    DROP_LAB = {"name": "lab-estate", "action": ACTION_DROP, "when": {"host": "^lab-"}}
    SUPPRESS_FLAPPER = {
        "name": "known-flapper",
        "action": ACTION_SUPPRESS,
        "when": {"host": "^leaf-01$", "event.type": "Interface Down$"},
    }

    def test_a_matching_drop_rule_drops_under_its_own_name(self):
        """The rule name is the counter key, which is how an operator finds what ate their events."""
        result = self.decide(self.payload(host="lab-sw-01"), {"rules": [self.DROP_LAB]})
        self.assertEqual(result.decision.action, ACTION_DROP)
        self.assertEqual(result.decision.reason, "lab-estate")

    def test_a_matching_suppress_rule_accepts_and_names_itself(self):
        """Suppression is an acceptance: the ticket exists, it is just not work."""
        result = self.decide(self.payload(), {"rules": [self.SUPPRESS_FLAPPER]})
        self.assertEqual(result.decision.action, ACTION_SUPPRESS)
        self.assertEqual(result.decision.reason, "known-flapper")

    def test_every_clause_must_match(self):
        """A rule is an AND across its clauses, not an OR."""
        result = self.decide(self.payload(host="leaf-02"), {"rules": [self.SUPPRESS_FLAPPER]})
        self.assertEqual(result.decision.action, ACTION_ACCEPT)

    def test_a_clause_whose_path_is_missing_does_not_match(self):
        """A rule about a field the payload lacks must not fire on every payload that lacks it."""
        rule = {"name": "by-interface", "action": ACTION_DROP, "when": {"interface": ".*"}}
        self.assertEqual(self.decide(self.payload(), {"rules": [rule]}).decision.action, ACTION_ACCEPT)

    def test_the_first_matching_rule_decides(self):
        """Order is the operator's, and it is the only tie-break there is."""
        both = [
            {"name": "first", "action": ACTION_SUPPRESS, "when": {"host": "^leaf-01$"}},
            {"name": "second", "action": ACTION_DROP, "when": {"host": "^leaf-"}},
        ]
        self.assertEqual(self.decide(self.payload(), {"rules": both}).decision.reason, "first")

    def test_patterns_search_rather_than_match_from_the_start(self):
        """Documented as `re.search`, so an unanchored pattern finds a substring."""
        rule = {"name": "anywhere", "action": ACTION_DROP, "when": {"message": "ethernet-1/1"}}
        self.assertEqual(self.decide(self.payload(), {"rules": [rule]}).decision.reason, "anywhere")


class TestRateLimit(PreFilterTestCase):
    """F6 - the flood guard."""

    def test_a_burst_is_admitted_then_refused(self):
        """The bucket starts full, so a quiet consumer is not throttled by its first traffic."""
        with settings_with({"t": {**TOPIC, "rate_limit": {"per_minute": 60, "burst": 2}}}):
            loaded = config.load()
        rules = prefilter.PreFilter(loaded, clock=self.clock)
        topic_config = rules.topic("t")
        event = normalize(self.payload(), topic_config=topic_config, max_payload_bytes=65536)

        self.assertEqual(rules.decide(event, topic_config).decision.action, ACTION_ACCEPT)
        self.assertEqual(rules.decide(event, topic_config).decision.action, ACTION_ACCEPT)
        third = rules.decide(event, topic_config).decision
        self.assertEqual(third.action, ACTION_DROP)
        self.assertEqual(third.reason, REASON_RATE_LIMITED)

    def test_tokens_refill_over_time(self):
        """One per second at sixty a minute, so a second's wait buys one more message."""
        with settings_with({"t": {**TOPIC, "rate_limit": {"per_minute": 60, "burst": 1}}}):
            loaded = config.load()
        rules = prefilter.PreFilter(loaded, clock=self.clock)
        topic_config = rules.topic("t")
        event = normalize(self.payload(), topic_config=topic_config, max_payload_bytes=65536)

        self.assertEqual(rules.decide(event, topic_config).decision.action, ACTION_ACCEPT)
        self.assertEqual(rules.decide(event, topic_config).decision.action, ACTION_DROP)
        self.clock.advance(1)
        self.assertEqual(rules.decide(event, topic_config).decision.action, ACTION_ACCEPT)

    def test_refill_stops_at_the_burst_ceiling(self):
        """An hour of quiet must not buy an hour's worth of flood."""
        bucket = prefilter.TokenBucket(per_minute=60, burst=2, clock=self.clock)
        self.clock.advance(3600)
        self.assertTrue(bucket.take())
        self.assertTrue(bucket.take())
        self.assertFalse(bucket.take())

    def test_a_topic_without_a_limit_has_no_bucket(self):
        """Unlimited is the default, and it costs nothing."""
        for _ in range(20):
            self.assertEqual(self.decide(self.payload()).decision.action, ACTION_ACCEPT)


class TestRuleOrder(PreFilterTestCase):
    """When a payload would fail more than one rule, the earlier one is the reason recorded."""

    def test_a_disabled_type_beats_the_severity_floor(self):
        """F3 runs before F4, and the counter has to say so."""
        payload = self.payload(event={"type": "Test Disabled Type", "severity": SeverityChoices.INFO})
        result = self.decide(payload, {"minimum_severity": SeverityChoices.CRITICAL})
        self.assertEqual(result.decision.reason, REASON_EVENT_TYPE_DISABLED)

    def test_the_severity_floor_beats_a_match_rule(self):
        """F4 runs before F5: cheaper first, and a floor is not a rule anyone wrote by hand."""
        payload = self.payload(event={"type": "Test Interface Down", "severity": SeverityChoices.INFO})
        result = self.decide(
            payload,
            {"minimum_severity": SeverityChoices.MAJOR, "rules": [TestMatchRules.SUPPRESS_FLAPPER]},
        )
        self.assertEqual(result.decision.reason, REASON_BELOW_SEVERITY_FLOOR)

    def test_a_match_rule_beats_the_rate_limit(self):
        """F5 before F6, so a message the operator meant to drop never spends a token."""
        with settings_with(
            {
                "t": {
                    **TOPIC,
                    "rate_limit": {"per_minute": 60, "burst": 1},
                    "rules": [
                        {"name": "lab-estate", "action": ACTION_DROP, "when": {"host": "^lab-"}},
                    ],
                }
            }
        ):
            loaded = config.load()
        rules = prefilter.PreFilter(loaded, clock=self.clock)
        topic_config = rules.topic("t")
        dropped = normalize(self.payload(host="lab-sw-01"), topic_config=topic_config, max_payload_bytes=65536)
        kept = normalize(self.payload(), topic_config=topic_config, max_payload_bytes=65536)

        for _ in range(5):
            self.assertEqual(rules.decide(dropped, topic_config).decision.reason, "lab-estate")
        self.assertEqual(rules.decide(kept, topic_config).decision.action, ACTION_ACCEPT)


class TestEventTypeCache(PreFilterTestCase):
    """The catalogue is re-read occasionally rather than per message."""

    def test_repeated_decisions_do_not_re_query(self):
        """Two queries in front of every event is a poor trade for a table of ten rows."""
        with settings_with({"t": TOPIC}):
            loaded = config.load()
        rules = prefilter.PreFilter(loaded, clock=self.clock)
        topic_config = rules.topic("t")
        event = normalize(self.payload(), topic_config=topic_config, max_payload_bytes=65536)

        rules.decide(event, topic_config)
        with self.assertNumQueries(0):
            for _ in range(10):
                rules.decide(event, topic_config)

    def test_the_catalogue_is_re_read_once_the_ttl_passes(self):
        """Disabling a type takes effect within the documented window, not never."""
        with settings_with({"t": TOPIC}):
            loaded = config.load()
        rules = prefilter.PreFilter(loaded, clock=self.clock)
        topic_config = rules.topic("t")
        event = normalize(self.payload(), topic_config=topic_config, max_payload_bytes=65536)
        self.assertEqual(rules.decide(event, topic_config).decision.action, ACTION_ACCEPT)

        EventType.objects.filter(name="Test Interface Down").update(enabled=False)
        self.clock.advance(loaded.event_type_cache_seconds + 1)
        self.assertEqual(rules.decide(event, topic_config).decision.reason, REASON_EVENT_TYPE_DISABLED)
