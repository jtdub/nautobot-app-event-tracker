"""Test that the lab's ingestion configuration and its syslog bridge agree.

`development/containerlab/fluent-bit.conf` and `classify.lua` decide the shape of a message;
`development/containerlab/nautobot_config_lab.py` reads it. Nothing else checks that the two agree,
and when they do not the symptom is a list of tickets titled after nothing in particular.

What this proves: the field map resolves against a payload of the shape the bridge is written to
produce, and the whole configuration passes the consumer's own startup validation. What it does not
prove: that SR Linux emits what the bridge expects. Only the lab itself can show that - which is
the point of running it, and of Phase 2.5.

`development/` is not shipped in the wheel, so these tests skip when the app is installed rather
than checked out. The skip is on the directory, not on the file: a source tree missing the file
itself is a failure worth seeing.
"""

import importlib.util
import unittest
from functools import lru_cache
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.ingestion import config
from nautobot_event_tracker.ingestion.normalize import normalize, resolve_path
from nautobot_event_tracker.models import EventType
from nautobot_event_tracker.tests import fixtures

DEVELOPMENT = Path(__file__).resolve().parents[2] / "development"
LAB_CONFIG_PATH = DEVELOPMENT / "containerlab" / "nautobot_config_lab.py"

in_a_source_checkout = unittest.skipUnless(DEVELOPMENT.is_dir(), "development/ is not part of the installed package")

#: What the bridge produces, per the shape documented at the top of `fluent-bit.conf`: the syslog
#: input supplies `host` and `message`, and `classify.lua` adds `event` and `interface`.
BRIDGE_PAYLOAD = {
    "host": "leaf-01",
    "message": "Interface ethernet-1/1 is down",
    "interface": "ethernet-1/1",
    "event": {"type": "Interface Down", "severity": "3"},
    "timestamp": "2026-08-15T03:14:00Z",
}


@lru_cache(maxsize=1)
def lab_ingestion():
    """Load the lab's ingestion block from `development/`, which is not an importable package."""
    spec = importlib.util.spec_from_file_location("nautobot_config_lab", LAB_CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LAB_INGESTION


def lab_config():
    """The lab's configuration as the consumer parses it at startup."""
    with override_settings(PLUGINS_CONFIG={"nautobot_event_tracker": {"ingestion": lab_ingestion()}}):
        return config.load(require_topics=True)


@in_a_source_checkout
class TestTheLabConfigurationIsUsable(TestCase):
    """The consumer would start with it."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def test_the_file_is_where_the_documentation_says(self):
        """Everything else here depends on it."""
        self.assertTrue(LAB_CONFIG_PATH.exists(), f"{LAB_CONFIG_PATH} is missing")

    def test_it_passes_startup_validation(self):
        """The same check `nautobot-server eventconsumer` runs before opening a socket."""
        loaded = lab_config()
        self.assertEqual(loaded.consumer, "kafka")
        self.assertEqual(loaded.topic_names, ("network.events",))

    def test_its_default_event_type_exists_in_the_catalogue(self):
        """`Unclassified` has to be a real type, or every unmatched message is dropped."""
        EventType.objects.get_or_create(name="Unclassified")
        self.assertEqual(config.database_problems(lab_config()), [])


@in_a_source_checkout
class TestTheFieldMapMatchesTheBridge(SimpleTestCase):
    """Every mapped path resolves against a message of the shape the bridge produces."""

    def setUp(self):
        """Parse the lab's topic configuration."""
        super().setUp()
        self.topic = lab_config().topics["network.events"]

    def test_every_mapped_path_resolves(self):
        """A path that resolves to nothing is a ticket field silently falling back or empty."""
        for field, path in self.topic.field_map.items():
            self.assertIsNotNone(
                resolve_path(BRIDGE_PAYLOAD, path),
                f"the lab field map's '{field}' path ({path}) finds nothing in a bridge payload",
            )

    def test_every_dedup_path_resolves(self):
        """An unresolvable template gives every event its own ticket, defeating recurrence."""
        event = normalize(BRIDGE_PAYLOAD, topic_config=self.topic)
        self.assertEqual(event.dedup_key, "Interface Down:leaf-01:ethernet-1/1")

    def test_the_bridge_severity_maps_onto_our_scale(self):
        """Syslog sends a number; a ticket needs one of five words."""
        event = normalize(BRIDGE_PAYLOAD, topic_config=self.topic)
        self.assertEqual(event.severity, SeverityChoices.MAJOR)

    def test_a_message_becomes_a_readable_title(self):
        """The whole point of the map: a person reading the ticket list learns something."""
        event = normalize(BRIDGE_PAYLOAD, topic_config=self.topic)
        self.assertEqual(event.title, "Interface ethernet-1/1 is down")
        self.assertEqual(event.event_type_name, "Interface Down")

    def test_the_boot_chatter_rule_matches_what_it_is_written_for(self):
        """It exists so the lab's first ticket list is not all start-up noise."""
        rule = next(rule for rule in self.topic.rules if rule.name == "srlinux-boot-chatter")
        path, pattern = rule.when[0]
        self.assertEqual(path, "message")
        self.assertTrue(pattern.search("Application sr_linux_mgr is now running"))
        self.assertFalse(pattern.search("Interface ethernet-1/1 is down"))
