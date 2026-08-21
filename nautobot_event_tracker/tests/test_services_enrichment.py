"""The enrichment resolver: what it finds, what it refuses to guess at, and what it remembers.

Phase 4A rules E5 to E8. The resolver's seam is the ORM, so these tests create real objects -
there is nothing here worth faking, and faking it would test nothing.
"""

from unittest import mock

from django.db import DatabaseError
from django.test import TestCase

from nautobot_event_tracker.services import enrichment
from nautobot_event_tracker.services.enrichment import (
    MISS_AMBIGUOUS,
    MISS_ERROR,
    MISS_NO_MATCH,
    MISS_NOT_SCALAR,
    MISS_PATH_ABSENT,
    Resolver,
    ResolveRule,
)
from nautobot_event_tracker.tests import fixtures

DEVICE_RULE = ResolveRule(name="device", path="host", model="dcim.device", field="name")
INTERFACE_RULE = ResolveRule(
    name="interface",
    path="interface",
    model="dcim.interface",
    field="name",
    scope=(("device", "device"),),
)


class ResolverTestCase(TestCase):
    """A resolver with a clock a test moves by hand, as the house rules require."""

    def setUp(self):
        """Build the resolver and the estate its rules look in."""
        self.clock = fixtures.FakeClock()
        self.resolver = Resolver(ttl_seconds=300, max_entries=100, clock=self.clock)
        self.device = fixtures.create_device("leaf-01")
        self.interface = fixtures.create_interface(self.device)

    def resolve(self, payload, rules=(DEVICE_RULE,)):
        """Run these rules over this payload."""
        return self.resolver.resolve(payload, rules)


class TestFindingObjects(ResolverTestCase):
    """The ordinary case, and the one the design is actually about."""

    def test_a_hostname_finds_its_device(self):
        """The whole point: a string in a payload becomes an object on a ticket."""
        resolution = self.resolve({"host": "leaf-01"})
        self.assertEqual(list(resolution.objects), [self.device])
        self.assertEqual(resolution.misses, ())

    def test_an_interface_resolves_inside_its_device(self):
        """Every switch in the estate has an `ethernet-1/1`; only one of them has this one."""
        other = fixtures.create_device("leaf-02")
        fixtures.create_interface(other)

        resolution = self.resolve({"host": "leaf-01", "interface": "ethernet-1/1"}, (DEVICE_RULE, INTERFACE_RULE))

        self.assertEqual(list(resolution.objects), [self.device, self.interface])

    def test_the_wrong_devices_interface_is_not_offered(self):
        """The scope is the test: unscoped, this name matches both switches."""
        other = fixtures.create_device("leaf-02")
        fixtures.create_interface(other, name="ethernet-1/9")

        resolution = self.resolve({"host": "leaf-01", "interface": "ethernet-1/9"}, (DEVICE_RULE, INTERFACE_RULE))

        self.assertEqual(list(resolution.objects), [self.device])
        self.assertEqual(resolution.misses, (("interface", MISS_NO_MATCH),))

    def test_case_does_not_decide_whether_a_device_is_found(self):
        """A device logs its hostname in whatever case its own configuration holds."""
        resolution = self.resolve({"host": "LEAF-01"})
        self.assertEqual(list(resolution.objects), [self.device])

    def test_a_value_that_is_not_a_string_is_looked_up_as_one(self):
        """Collectors send a number and a string for the same field, sometimes in one stream."""
        device = fixtures.create_device("42")
        resolution = self.resolve({"host": 42})
        self.assertEqual(list(resolution.objects), [device])

    def test_surrounding_whitespace_is_not_part_of_a_name(self):
        """A syslog field arriving padded should not stop resolving because of it."""
        resolution = self.resolve({"host": " leaf-01 "})
        self.assertEqual(list(resolution.objects), [self.device])

    def test_one_object_is_attached_once(self):
        """Two rules finding the same row is one attachment, not two."""
        by_name = DEVICE_RULE
        by_asset = ResolveRule(name="again", path="also", model="dcim.device", field="name")

        resolution = self.resolve({"host": "leaf-01", "also": "leaf-01"}, (by_name, by_asset))

        self.assertEqual(list(resolution.objects), [self.device])

    def test_no_rules_means_no_work(self):
        """A topic that says nothing about resolution costs nothing."""
        with self.assertNumQueries(0):
            resolution = self.resolver.resolve({"host": "leaf-01"}, ())
        self.assertIs(resolution, enrichment.EMPTY)


class TestWhatItRefusesToGuess(ResolverTestCase):
    """E5 to E7: the answers that are not objects, and which of them are faults."""

    def test_an_unknown_hostname_is_a_miss(self):
        """A device that has not been onboarded yet is a normal state, and a counted one."""
        resolution = self.resolve({"host": "leaf-99"})
        self.assertEqual(resolution.objects, ())
        self.assertEqual(resolution.misses, (("device", MISS_NO_MATCH),))

    def test_two_matches_attach_neither(self):
        """E6 - picking one picks it by primary-key order, which is to say arbitrarily."""
        rule = ResolveRule(name="site", path="site", model="dcim.location", field="name")
        fixtures.create_location("Ambiguous")
        second = fixtures.create_location("ambiguous")
        self.assertIsNotNone(second.pk)

        resolution = self.resolve({"site": "AMBIGUOUS"}, (rule,))

        self.assertEqual(resolution.objects, ())
        self.assertEqual(resolution.misses, (("site", MISS_AMBIGUOUS),))

    def test_a_path_the_payload_does_not_carry_is_a_fault(self):
        """E7 - the rule and the payload disagree, and somebody should hear about it."""
        resolution = self.resolve({"something": "else"})
        self.assertEqual(resolution.misses, (("device", MISS_PATH_ABSENT),))

    def test_an_empty_value_is_an_answer_and_not_a_fault(self):
        """E7 - the lab's bridge writes `""` for a message that names no interface, on purpose."""
        resolution = self.resolve({"host": "leaf-01", "interface": ""}, (DEVICE_RULE, INTERFACE_RULE))

        self.assertEqual(list(resolution.objects), [self.device])
        self.assertEqual(resolution.misses, (), "an empty value must not be counted as a miss")

    def test_a_null_value_is_an_answer_too(self):
        """A producer that writes JSON null means the same thing as one that writes an empty string."""
        resolution = self.resolve({"host": "leaf-01", "interface": None}, (DEVICE_RULE, INTERFACE_RULE))
        self.assertEqual(resolution.misses, ())

    def test_a_list_attaches_nothing(self):
        """Spec 11.7 - supporting lists means multiplying the ambiguity rule by their length."""
        resolution = self.resolve({"host": ["leaf-01", "leaf-02"]})
        self.assertEqual(resolution.misses, (("device", MISS_NOT_SCALAR),))

    def test_a_scope_that_did_not_resolve_skips_rather_than_misses(self):
        """E7 - a consequence of the device's miss is not a second fault of the interface's own."""
        resolution = self.resolve({"host": "leaf-99", "interface": "ethernet-1/1"}, (DEVICE_RULE, INTERFACE_RULE))

        self.assertEqual(resolution.objects, ())
        self.assertEqual(resolution.misses, (("device", MISS_NO_MATCH),))

    def test_a_broken_query_is_a_miss_and_not_an_exception(self):
        """E5 - this runs immediately before the ticket write, and no lookup is worth an event."""
        with mock.patch(
            "nautobot_event_tracker.services.enrichment.ResolveRule.model_class",
            new_callable=mock.PropertyMock,
            side_effect=DatabaseError("connection lost"),
        ):
            resolution = self.resolve({"host": "leaf-01"})

        self.assertEqual(resolution.objects, ())
        self.assertEqual(resolution.misses, (("device", MISS_ERROR),))


class TestTheCache(ResolverTestCase):
    """E8: held for a while, misses included, and never without a bound."""

    def test_a_repeated_lookup_costs_one_query(self):
        """The consumer sees the same hostname on every message from that device."""
        self.resolve({"host": "leaf-01"})
        with self.assertNumQueries(0):
            resolution = self.resolve({"host": "leaf-01"})
        self.assertEqual(list(resolution.objects), [self.device])

    def test_a_repeated_miss_costs_one_query_too(self):
        """The Phase 3 lesson: a cache that holds only hits makes the failing case the expensive one."""
        self.resolve({"host": "leaf-99"})
        with self.assertNumQueries(0):
            resolution = self.resolve({"host": "leaf-99"})
        self.assertEqual(resolution.misses, (("device", MISS_NO_MATCH),))

    def test_an_answer_is_re_read_once_it_is_stale(self):
        """A device onboarded since the last read starts resolving within the TTL."""
        self.resolve({"host": "leaf-99"})
        fixtures.create_device("leaf-99")

        self.clock.advance(299)
        self.assertEqual(self.resolve({"host": "leaf-99"}).objects, ())

        self.clock.advance(2)
        self.assertEqual(len(self.resolve({"host": "leaf-99"}).objects), 1)

    def test_the_cache_does_not_grow_without_a_bound(self):
        """Its keys come out of the payload, which whoever emits the events controls."""
        resolver = Resolver(ttl_seconds=300, max_entries=3, clock=self.clock)
        for index in range(10):
            resolver.resolve({"host": f"unknown-{index}"}, (DEVICE_RULE,))

        self.assertEqual(len(resolver._cache), 3)  # pylint: disable=protected-access

    def test_the_bound_evicts_the_least_recently_used(self):
        """The hostname arriving on every message must not be the one thrown away."""
        resolver = Resolver(ttl_seconds=300, max_entries=2, clock=self.clock)
        resolver.resolve({"host": "leaf-01"}, (DEVICE_RULE,))
        resolver.resolve({"host": "unknown-1"}, (DEVICE_RULE,))
        resolver.resolve({"host": "leaf-01"}, (DEVICE_RULE,))
        resolver.resolve({"host": "unknown-2"}, (DEVICE_RULE,))

        with self.assertNumQueries(0):
            resolution = resolver.resolve({"host": "leaf-01"}, (DEVICE_RULE,))
        self.assertEqual(list(resolution.objects), [self.device])

    def test_two_devices_with_one_interface_name_are_cached_apart(self):
        """The scope is part of the key, or the second device would get the first one's interface."""
        other = fixtures.create_device("leaf-02")
        other_interface = fixtures.create_interface(other)
        rules = (DEVICE_RULE, INTERFACE_RULE)

        first = self.resolve({"host": "leaf-01", "interface": "ethernet-1/1"}, rules)
        second = self.resolve({"host": "leaf-02", "interface": "ethernet-1/1"}, rules)

        self.assertEqual(list(first.objects), [self.device, self.interface])
        self.assertEqual(list(second.objects), [other, other_interface])
