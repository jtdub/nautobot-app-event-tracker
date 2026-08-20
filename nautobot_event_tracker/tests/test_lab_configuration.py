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

import yaml
from django.test import SimpleTestCase, TestCase

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.ingestion import config
from nautobot_event_tracker.ingestion.normalize import normalize, resolve_path
from nautobot_event_tracker.management.commands.generate_nautobot_event_tracker_test_data import (
    FABRIC,
    FABRIC_CABLES,
    MANAGEMENT_INTERFACE,
)
from nautobot_event_tracker.models import EventType
from nautobot_event_tracker.tests import fixtures

DEVELOPMENT = Path(__file__).resolve().parents[2] / "development"
LAB_CONFIG_PATH = DEVELOPMENT / "containerlab" / "nautobot_config_lab.py"
TOPOLOGY_PATH = DEVELOPMENT / "containerlab" / "topology.clab.yml"
POPULATE_PATH = DEVELOPMENT / "containerlab" / "populate_nautobot.py"

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


@lru_cache(maxsize=2)
def from_development(name, path):
    """Load a module from `development/`, which is not an importable package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lab_ingestion():
    """The lab's ingestion configuration block."""
    return from_development("nautobot_config_lab", LAB_CONFIG_PATH).LAB_INGESTION


@lru_cache(maxsize=1)
def topology():
    """The containerlab topology, as `(nodes, links)`, links being `[(node, interface), ...]` pairs."""
    populate = from_development("populate_nautobot", POPULATE_PATH)
    document = yaml.safe_load(TOPOLOGY_PATH.read_text(encoding="utf-8"))["topology"]

    links = [
        [(node, populate.interface_name(port)) for node, _, port in (end.partition(":") for end in link["endpoints"])]
        for link in document.get("links", [])
    ]
    return document["nodes"], links


def lab_config():
    """The lab's configuration as the consumer parses it at startup.

    Through the fixture, so the app's own defaults are underneath it as they are in a deployment.
    The lab's resolve rules are checked against `attachable_object_types`, and a bare override
    would leave that empty and refuse every one of them.
    """
    with fixtures.app_settings(ingestion=lab_ingestion()):
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

    def test_every_resolve_path_resolves(self):
        """A resolve rule reading a path the bridge does not produce is a miss on every event."""
        for rule in self.topic.resolve:
            self.assertIsNotNone(
                resolve_path(BRIDGE_PAYLOAD, rule.path),
                f"the lab resolve rule '{rule.name}' reads {rule.path}, which a bridge payload does not carry",
            )

    def test_the_resolve_rules_name_the_estate_the_lab_creates(self):
        """The device names in Nautobot are the hostnames the devices log, which is the whole point.

        Phase 2.5 section 6 made them match so that this phase would have real data to hit. This
        is the test that says they still do: the hostname in a bridge payload is a device in the
        fabric, and the interface the bridge pulls out of the message is one that device has.
        """
        host = resolve_path(BRIDGE_PAYLOAD, "host")
        interface = resolve_path(BRIDGE_PAYLOAD, "interface")

        self.assertIn(host, FABRIC)
        self.assertIn(interface, FABRIC[host]["interfaces"])

    def test_the_interface_rule_is_scoped_by_the_device(self):
        """Every node in this fabric has an `ethernet-1/1`; unscoped, the rule matches three."""
        interface_rule = next(rule for rule in self.topic.resolve if rule.model == "dcim.interface")
        self.assertEqual(interface_rule.scope, (("device", "device"),))

        shared = [name for name, node in FABRIC.items() if "ethernet-1/1" in node["interfaces"]]
        self.assertGreater(len(shared), 1, "if the fabric stopped sharing a name the scope would look unnecessary")

    def test_the_boot_chatter_rule_matches_what_it_is_written_for(self):
        """It exists so the lab's first ticket list is not all start-up noise."""
        rule = next(rule for rule in self.topic.rules if rule.name == "srlinux-boot-chatter")
        path, pattern = rule.when[0]
        self.assertEqual(path, "message")
        self.assertTrue(pattern.search("Application sr_linux_mgr is now running"))
        self.assertFalse(pattern.search("Interface ethernet-1/1 is down"))


@in_a_source_checkout
class TestTheGeneratedEstateMatchesTheTopology(SimpleTestCase):
    """The test data command's fabric is the lab's fabric, and stays that way.

    They share a database and three device names. If they described different networks, running
    both would leave `leaf-01` holding whichever set of interfaces was created first, and a ticket
    naming one the device does not have. This is the check that keeps them the same fabric: the
    command's constants against the topology file and the nodes' own startup configurations.
    """

    def setUp(self):
        """The topology, as nodes and links, and the script that reads it."""
        super().setUp()
        self.nodes, self.links = topology()
        self.populate = from_development("populate_nautobot", POPULATE_PATH)

    def test_it_covers_every_node_that_is_a_device(self):
        """A node missing from the command is a device the lab has and the demo does not."""
        in_topology = {name for name, node in self.nodes.items() if self.populate.is_a_device(node)}
        self.assertEqual(set(FABRIC), in_topology)

    def test_each_device_has_the_interfaces_its_links_describe(self):
        """Right down to the naming: `e1-1` in the topology is `ethernet-1/1` on the device."""
        for host, described in FABRIC.items():
            from_links = {name for link in self.links for node, name in link if node == host}
            self.assertEqual(set(described["interfaces"]), from_links, f"{host}'s interfaces differ")

    def test_the_addresses_are_the_ones_the_devices_configure(self):
        """Read from the same `.cli` files the population script reads, and the devices run."""
        for host, described in FABRIC.items():
            self.assertEqual(
                described["interfaces"], self.populate.startup_addresses(host), f"{host}'s addressing differs"
            )

    def test_the_management_addresses_are_the_ones_the_topology_pins(self):
        """The topology pins them so they survive a redeploy; this is the same list."""
        for host, described in FABRIC.items():
            self.assertEqual(described["management"], f"{self.nodes[host]['mgmt-ipv4']}/24")

    def test_the_cables_are_the_links_between_two_devices(self):
        """Excluding the client's, which has no Nautobot interface to be the second termination."""
        expected = {frozenset(link) for link in self.links if all(node in FABRIC for node, _ in link)}
        described = {
            frozenset(((left, left_interface), (right, right_interface)))
            for left, left_interface, right, right_interface in FABRIC_CABLES
        }
        self.assertEqual(described, expected)

    def test_the_management_interface_is_not_one_of_the_fabric_interfaces(self):
        """It is created separately, as `mgmt_only`, and the topology's links never mention it."""
        for host, described in FABRIC.items():
            self.assertNotIn(MANAGEMENT_INTERFACE, described["interfaces"], f"{host} lists its management port twice")
