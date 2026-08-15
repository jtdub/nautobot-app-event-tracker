"""Test the ingestion configuration and the faults it refuses to start with.

Every test here asserts on the *message*, not just the exception: an operator reading it at 03:00
is the only reason the validation exists at all.
"""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.ingestion import config
from nautobot_event_tracker.ingestion.constants import UNKNOWN_EVENT_TYPE_DROP
from nautobot_event_tracker.tests import fixtures

TOPIC = {
    **fixtures.INGESTION_TOPIC,
    "defaults": {**fixtures.INGESTION_TOPIC["defaults"], "severity": SeverityChoices.MINOR},
    "severity_map": {"3": SeverityChoices.MAJOR},
    "minimum_severity": SeverityChoices.INFO,
    "rate_limit": {"per_minute": 60, "burst": 120},
    "rules": [{"name": "lab", "action": "drop", "when": {"host": "^lab-"}}],
}


def settings_with(ingestion):
    """Build a PLUGINS_CONFIG override carrying this ingestion block."""
    return override_settings(PLUGINS_CONFIG={"nautobot_event_tracker": {"ingestion": ingestion}})


class TestDefaults(SimpleTestCase):
    """What an unconfigured deployment gets."""

    @settings_with({})
    def test_an_empty_block_consumes_nothing(self):
        """Installing the app must not start consuming anything on its own."""
        loaded = config.load()
        self.assertEqual(loaded.topics, {})
        self.assertEqual(loaded.topic_names, ())

    @settings_with({})
    def test_defaults_are_applied_key_by_key(self):
        """Nautobot merges only top-level keys, so the nested defaults are ours to apply."""
        loaded = config.load()
        self.assertEqual(loaded.stats_bucket_seconds, config.DEFAULTS["stats_bucket_seconds"])
        self.assertEqual(loaded.kafka["group_id"], config.DEFAULTS["kafka"]["group_id"])

    @settings_with({"kafka": {"bootstrap_servers": ["broker:9092"]}})
    def test_a_partial_nested_block_keeps_the_other_nested_defaults(self):
        """Setting one Kafka key must not silently drop the rest."""
        loaded = config.load()
        self.assertEqual(loaded.kafka["bootstrap_servers"], ["broker:9092"])
        self.assertEqual(loaded.kafka["group_id"], config.DEFAULTS["kafka"]["group_id"])

    @settings_with({"consumer_name": ""})
    def test_a_blank_consumer_name_becomes_host_and_pid(self):
        """Counters have to name a process a person can go and look at."""
        self.assertIn(":", config.load().consumer_name)


class TestTopicParsing(SimpleTestCase):
    """A well-formed topic becomes the values the pipeline reads."""

    @settings_with({"topics": {"network.events": TOPIC}})
    def test_a_valid_topic_parses(self):
        """The parsed topic carries what was configured."""
        topic = config.load().topics["network.events"]
        self.assertEqual(topic.name, "network.events")
        self.assertEqual(topic.dedup_key_template, "{event.type}:{host}")
        self.assertEqual(topic.minimum_severity, SeverityChoices.INFO)
        self.assertEqual(topic.rate_limit.per_minute, 60)
        self.assertEqual(topic.rate_limit.burst, 120)

    @settings_with({"topics": {"network.events": TOPIC}})
    def test_severity_map_keys_are_strings(self):
        """A syslog severity arrives as 3 and as \"3\"; both have to hit the same entry."""
        self.assertEqual(config.load().topics["network.events"].severity_map, {"3": SeverityChoices.MAJOR})

    @settings_with({"topics": {"network.events": TOPIC}})
    def test_rules_are_compiled_once(self):
        """Compiling per message would put a regex compile in the hot path."""
        rule = config.load().topics["network.events"].rules[0]
        self.assertEqual(rule.name, "lab")
        self.assertTrue(rule.when[0][1].search("lab-sw-01"))
        self.assertFalse(rule.when[0][1].search("prod-sw-01"))

    @settings_with({"topics": {"network.events": {**TOPIC, "rate_limit": {"per_minute": 30}}}})
    def test_burst_defaults_to_the_per_minute_rate(self):
        """A limit without a burst is still a usable limit."""
        self.assertEqual(config.load().topics["network.events"].rate_limit.burst, 30)

    @settings_with({"topics": {"network.events": {**TOPIC, "rate_limit": {}}}})
    def test_no_rate_limit_configured_means_no_bucket(self):
        """Most topics want no limit, and should pay nothing for one."""
        self.assertIsNone(config.load().topics["network.events"].rate_limit)

    @settings_with({"topics": {"network.events": {**TOPIC, "unknown_event_type": UNKNOWN_EVENT_TYPE_DROP}}})
    def test_unknown_event_type_policy_is_carried_through(self):
        """A deployment may refuse events it has no type for."""
        self.assertEqual(config.load().topics["network.events"].unknown_event_type, UNKNOWN_EVENT_TYPE_DROP)


class TestValidation(fixtures.RefusalAssertions, SimpleTestCase):
    """Every fault is reported, and reported in terms an operator can act on."""

    def assert_refuses(self, ingestion, *expected):
        """Assert that loading raises and that the message names each expected fault."""
        with settings_with(ingestion):
            with self.assertRaises(ImproperlyConfigured) as caught:
                config.load()
        return self.assert_names(str(caught.exception), expected)

    def test_field_map_must_name_the_event_type_and_title(self):
        """A ticket with no type and no title is not a ticket anyone can act on."""
        self.assert_refuses(
            {"topics": {"t": {"field_map": {"severity": "sev"}}}},
            "field_map is missing 'event_type'",
            "field_map is missing 'title'",
        )

    def test_a_severity_map_value_must_be_a_severity(self):
        """A typo here would silently write an invalid severity onto a ticket."""
        self.assert_refuses(
            {"topics": {"t": {**TOPIC, "severity_map": {"3": "catastrophic"}}}},
            "severity_map value 'catastrophic' is not a severity",
        )

    def test_a_default_severity_must_be_a_severity(self):
        """Same for the fallback."""
        self.assert_refuses(
            {"topics": {"t": {**TOPIC, "defaults": {"severity": "urgent"}}}},
            "default severity 'urgent' is not a severity",
        )

    def test_a_minimum_severity_must_be_a_severity(self):
        """A floor nobody can be above or below is not a floor."""
        self.assert_refuses(
            {"topics": {"t": {**TOPIC, "minimum_severity": "high"}}},
            "minimum_severity 'high' is not a severity",
        )

    def test_a_rule_pattern_must_compile(self):
        """This is the fault that would otherwise surface on the first matching message."""
        self.assert_refuses(
            {"topics": {"t": {**TOPIC, "rules": [{"name": "bad", "action": "drop", "when": {"host": "^(unclosed"}}]}}},
            "pattern for 'host' does not compile",
        )

    def test_a_rule_needs_a_name_and_a_known_action(self):
        """The name is the counter key; the action is the whole point of the rule."""
        self.assert_refuses(
            {"topics": {"t": {**TOPIC, "rules": [{"when": {"host": "^lab-"}}]}}},
            "needs a name",
            "action must be one of",
        )

    def test_rule_names_are_unique_within_a_topic(self):
        """Two rules sharing a name would add their drops together under one key."""
        self.assert_refuses(
            {
                "topics": {
                    "t": {
                        **TOPIC,
                        "rules": [
                            {"name": "same", "action": "drop", "when": {"host": "^a"}},
                            {"name": "same", "action": "drop", "when": {"host": "^b"}},
                        ],
                    }
                }
            },
            "name 'same' is already used in this topic",
        )

    def test_a_rule_must_have_at_least_one_clause(self):
        """A rule with no clauses matches everything, which is never what was meant."""
        self.assert_refuses(
            {"topics": {"t": {**TOPIC, "rules": [{"name": "everything", "action": "drop", "when": {}}]}}},
            "needs at least one 'when' clause",
        )

    def test_a_rate_limit_must_be_a_positive_integer(self):
        """Zero would drop everything; a string would fail on the first message."""
        self.assert_refuses(
            {"topics": {"t": {**TOPIC, "rate_limit": {"per_minute": 0}}}},
            "rate_limit per_minute must be a positive integer",
        )

    def test_interval_settings_must_be_positive_integers(self):
        """A zero bucket width would divide by zero on the first flush."""
        self.assert_refuses({"stats_bucket_seconds": 0}, "'stats_bucket_seconds' must be a positive integer")

    def test_every_fault_is_reported_at_once(self):
        """One restart should fix all of it, not the first of it."""
        message = self.assert_refuses(
            {
                "stats_flush_seconds": 0,
                "topics": {
                    "a": {"field_map": {}},
                    "b": {**TOPIC, "minimum_severity": "high"},
                },
            },
            "topic 'a'",
            "topic 'b'",
            "stats_flush_seconds",
        )
        self.assertGreaterEqual(len(message.splitlines()), 4)

    def test_naming_an_unconfigured_topic_is_a_fault(self):
        """Consuming nothing is the worst possible answer to a typo in --topics."""
        with settings_with({"topics": {"network.events": TOPIC}}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                config.load(topics=["netwrok.events"])
        self.assertIn("topic 'netwrok.events' is not configured", str(caught.exception))

    def test_topics_narrows_to_the_named_subset(self):
        """--topics is how an operator tries one topic without touching the others."""
        with settings_with({"topics": {"a": TOPIC, "b": TOPIC}}):
            self.assertEqual(config.load(topics=["a"]).topic_names, ("a",))


class TestDatabaseValidation(TestCase):
    """The half of validation that needs a query."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    @settings_with({"topics": {"t": TOPIC}})
    def test_a_default_event_type_must_exist(self):
        """The fallback has to be a type the catalogue holds, or the fallback is a hole."""
        self.assertEqual(config.database_problems(config.load()), [])

    @settings_with({"topics": {"t": {**TOPIC, "defaults": {"event_type": "No Such Type"}}}})
    def test_a_missing_default_event_type_is_reported(self):
        """Named so the operator can see which topic and which type."""
        problems = config.database_problems(config.load())
        self.assertEqual(len(problems), 1)
        self.assertIn("No Such Type", problems[0])
        self.assertIn("topic 't'", problems[0])

    @settings_with(
        {
            "topics": {
                "first": {**TOPIC, "defaults": {"event_type": "No Such Type"}},
                "second": {**TOPIC, "defaults": {"event_type": "No Such Type"}},
            }
        }
    )
    def test_two_topics_missing_the_same_type_are_two_problems(self):
        """Reporting one would leave the operator fixing this twice."""
        problems = config.database_problems(config.load())
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("first" in problem for problem in problems))
        self.assertTrue(any("second" in problem for problem in problems))

    @settings_with({"topics": {"t": {**TOPIC, "defaults": {}}}})
    def test_no_default_event_type_needs_no_query(self):
        """A topic that never falls back has nothing to check."""
        with self.assertNumQueries(0):
            self.assertEqual(config.database_problems(config.load()), [])
