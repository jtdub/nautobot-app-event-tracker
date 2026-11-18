"""Test the test data command.

The command ships in the app package, so somebody will read it as an example of how to make a
ticket. These tests hold it to the same rule the rest of the app follows: every ticket goes through
the service layer, and a ticket in a terminal state got there by walking the graph.

Generating is the expensive part - fifty tickets is fifty service-layer transactions - so the
classes that only read what one run produced generate once in `setUpTestData` and share it, and
generate twelve rather than fifty wherever the number does not matter.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from nautobot.dcim.models import Cable, Device, Interface
from nautobot.ipam.models import IPAddress

from nautobot_event_tracker.choices import TicketStatusChoices, UpdateTypeChoices
from nautobot_event_tracker.management.commands.generate_nautobot_event_tracker_test_data import (
    FABRIC,
    FABRIC_CABLES,
    HOSTS,
    MANAGEMENT_INTERFACE,
    TEST_DATA_TAG,
)
from nautobot_event_tracker.models import EventTicket, IngestionStats
from nautobot_event_tracker.tests import fixtures


def generate(**options):
    """Run the command, returning what it printed."""
    out = StringIO()
    call_command("generate_nautobot_event_tracker_test_data", stdout=out, **options)
    return out.getvalue()


class TestOneSmallRun(TestCase):
    """What a single `--count 12` run produces: the tickets, and the estate they are about."""

    @classmethod
    def setUpTestData(cls):
        """One run, read by every test here."""
        generate(count=12)

    def test_it_creates_the_requested_number(self):
        """`--count` is exact, not approximate: the status mix rounds into it."""
        self.assertEqual(EventTicket.objects.count(), 12)

    def test_every_ticket_carries_the_tag(self):
        """The tag is what makes `--flush` able to delete exactly what this made."""
        self.assertEqual(EventTicket.objects.filter(tags__name=TEST_DATA_TAG).count(), 12)

    def test_every_ticket_has_a_created_update(self):
        """Which only the service layer writes - a direct ORM create would have none."""
        for ticket in EventTicket.objects.all():
            self.assertTrue(
                ticket.updates.filter(update_type=UpdateTypeChoices.CREATED).exists(),
                f"{ticket} has no created update, so it did not come from the service layer",
            )

    def test_no_ingestion_counters_are_invented(self):
        """Counters are a record of a consumer having run; fabricating them would be a lie."""
        self.assertFalse(IngestionStats.objects.exists())

    def test_it_creates_a_device_for_every_host(self):
        """`generate_test_data` populates a database; a ticket list about nothing is not that."""
        self.assertEqual(sorted(Device.objects.values_list("name", flat=True)), sorted(HOSTS))

    def test_the_devices_have_interfaces(self):
        """A ticket about an interface should be able to point at one, plus a management port."""
        for host, described in FABRIC.items():
            self.assertEqual(
                sorted(Interface.objects.filter(device__name=host).values_list("name", flat=True)),
                sorted([*described["interfaces"], MANAGEMENT_INTERFACE]),
                f"{host} does not hold the interfaces the topology gives it",
            )

    def test_it_tags_what_it_created(self):
        """The tag is what makes the estate explicable, and removable."""
        self.assertEqual(Device.objects.filter(tags__name=TEST_DATA_TAG).count(), len(HOSTS))

    def test_every_device_has_a_primary_address(self):
        """The first thing anybody asks about a device in a ticket is what its IP is."""
        for device in Device.objects.all():
            self.assertIsNotNone(device.primary_ip4, f"{device} has no primary address")

    def test_the_devices_are_cabled_to_each_other(self):
        """A device connected to nothing is a device the topology view cannot draw."""
        cabled = {(cable.termination_a.device.name, cable.termination_b.device.name) for cable in Cable.objects.all()}
        self.assertEqual(len(cabled), len(FABRIC_CABLES))
        for left, right in cabled:
            self.assertNotEqual(left, right, "a device is cabled to itself")

    def test_the_addresses_carry_the_tag(self):
        """Or `--flush` would leave the demo addresses behind and the next run would collide."""
        expected = sum(len(described["interfaces"]) + 1 for described in FABRIC.values())
        self.assertEqual(IPAddress.objects.filter(tags__name=TEST_DATA_TAG).count(), expected)

    def test_every_ticket_names_an_interface_its_device_has(self):
        """A ticket about `Gi0/0/1 on leaf-01` is a ticket whose first click is a dead end."""
        for ticket in EventTicket.objects.all():
            host, interface = ticket.payload["host"], ticket.payload["interface"]
            self.assertTrue(
                Interface.objects.filter(device__name=host, name=interface).exists(),
                f"{host} has no interface {interface}",
            )


class TestTheDefaultRun(TestCase):
    """The full fifty, which is the run a demo actually shows and the only one that covers the mix."""

    @classmethod
    def setUpTestData(cls):
        """One default run, read by every test here."""
        generate()

    def test_the_default_run_covers_every_status(self):
        """A demo list where a state is missing is a demo of the wrong thing."""
        present = set(EventTicket.objects.values_list("status", flat=True))
        self.assertEqual(present, set(TicketStatusChoices.values()))

    def test_a_terminal_ticket_walked_the_graph_to_get_there(self):
        """Its trail has to show every step, not a status that appeared from nowhere."""
        for ticket in EventTicket.objects.filter(status=TicketStatusChoices.CLOSED):
            steps = list(
                ticket.updates.filter(update_type=UpdateTypeChoices.STATUS_CHANGE).values_list(
                    "from_status", "to_status"
                )
            )
            self.assertTrue(steps, f"{ticket} is closed with no status change in its trail")
            self.assertEqual(steps[0][0], TicketStatusChoices.NEW)
            self.assertEqual(steps[-1][1], TicketStatusChoices.CLOSED)
            for (_, left), (right, _) in zip(steps, steps[1:]):
                self.assertEqual(left, right, f"{ticket} skipped a state")

    def test_resolved_tickets_carry_a_resolution(self):
        """The service requires one; this asserts the command supplies something readable."""
        for ticket in EventTicket.objects.filter(status=TicketStatusChoices.RESOLVED):
            self.assertTrue(ticket.resolution)

    def test_tickets_have_histories_worth_looking_at(self):
        """A demo whose tickets have a single line of trail demonstrates nothing."""
        comments = EventTicket.objects.filter(updates__update_type=UpdateTypeChoices.COMMENT).distinct().count()
        self.assertGreater(comments, 0)

    def test_every_ticket_is_attached_to_the_device_it_names(self):
        """An attachment to some other object would teach a reader that attachments mean nothing."""
        devices = {device.name: device for device in Device.objects.all()}
        for ticket in EventTicket.objects.all():
            update = ticket.updates.get(update_type=UpdateTypeChoices.OBJECT_ATTACHED)
            self.assertEqual(update.related_object_id, devices[ticket.payload["host"]].pk)


class TestDeterminism(TestCase):
    """The same seed produces the same data."""

    def test_the_same_seed_produces_the_same_titles(self):
        """Two people comparing screenshots should be looking at the same tickets."""
        generate(count=8, seed=99)
        first = list(EventTicket.objects.order_by("title").values_list("title", flat=True))

        generate(flush=True, count=8, seed=99)
        second = list(EventTicket.objects.order_by("title").values_list("title", flat=True))

        self.assertEqual(first, second)

    def test_a_different_seed_produces_different_data(self):
        """Otherwise the seed would be decoration."""
        generate(count=8, seed=1)
        first = list(EventTicket.objects.order_by("id").values_list("title", flat=True))

        generate(flush=True, count=8, seed=2)
        second = list(EventTicket.objects.order_by("id").values_list("title", flat=True))

        self.assertNotEqual(first, second)


class TestFlush(TestCase):
    """`--flush` removes what the command made, and nothing else."""

    def test_it_deletes_the_generated_tickets(self):
        """Running twice should not leave a hundred tickets behind."""
        generate(count=8)
        generate(flush=True, count=8)
        self.assertEqual(EventTicket.objects.count(), 8)

    def test_it_deletes_the_demo_devices(self):
        """The estate goes with the tickets, or a flushed database is still full of demo devices."""
        generate(count=8)
        generate(flush=True, count=4)
        self.assertEqual(Device.objects.filter(tags__name=TEST_DATA_TAG).count(), len(HOSTS))

    def test_it_leaves_a_device_somebody_else_made(self):
        """An untagged device of the same name is the lab's, and is not ours to delete."""
        existing = fixtures.create_device(name=HOSTS[0])
        generate(count=8)
        generate(flush=True, count=4)
        self.assertTrue(Device.objects.filter(pk=existing.pk).exists())

    def test_it_leaves_a_persons_own_tickets_alone(self):
        """This is the whole reason the tag exists."""
        mine = fixtures.create_ticket(title="Opened by a person")
        generate(count=8)

        generate(flush=True, count=4)

        self.assertTrue(EventTicket.objects.filter(pk=mine.pk).exists())
        self.assertEqual(EventTicket.objects.count(), 5)

    def test_it_reports_what_it_deleted(self):
        """An operator running this against a populated database wants the number."""
        generate(count=8)
        self.assertIn("Deleted 8", generate(flush=True, count=4))

    def test_count_zero_leaves_the_database_clean(self):
        """The one way to undo a run: no tickets to be about, so no estate either."""
        generate(count=8)
        generate(flush=True, count=0)
        self.assertEqual(EventTicket.objects.count(), 0)
        self.assertEqual(Device.objects.count(), 0)
        self.assertEqual(IPAddress.objects.filter(tags__name=TEST_DATA_TAG).count(), 0)
        self.assertEqual(Cable.objects.count(), 0)


class TestADeviceSomebodyElseMade(TestCase):
    """After the lab has been populated, `leaf-01` is the real thing and stays that way."""

    @classmethod
    def setUpTestData(cls):
        """A device of one of the command's own names, made by somebody else first."""
        cls.existing = fixtures.create_device(name=HOSTS[0])
        generate(count=12)

    def test_it_keeps_the_device(self):
        """`get_or_create`, so the ticket points at the device you can go and break."""
        self.assertEqual(Device.objects.filter(name=HOSTS[0]).count(), 1)
        self.assertEqual(Device.objects.get(name=HOSTS[0]).pk, self.existing.pk)

    def test_it_does_not_tag_the_device(self):
        """Otherwise `--flush` would delete the lab's devices along with its own."""
        self.assertNotIn(TEST_DATA_TAG, [tag.name for tag in Device.objects.get(name=HOSTS[0]).tags.all()])

    def test_it_creates_no_interfaces_on_it(self):
        """Its interfaces are the lab's business; this command's names would be fiction on it."""
        self.assertFalse(Interface.objects.filter(device=self.existing).exists())

    def test_it_cables_nothing_to_it(self):
        """A cable on somebody else's interface is a change to their inventory, not ours."""
        self.assertFalse(Cable.objects.filter(terminations__interface__device=self.existing).exists())

    def test_it_gives_it_no_address(self):
        """Its addressing is the lab's business too, and its primary IP is already right."""
        self.existing.refresh_from_db()
        self.assertIsNone(self.existing.primary_ip4)


class TestRunningTwice(TestCase):
    """A second run adds tickets and no second estate."""

    def test_it_creates_no_second_estate(self):
        """`get_or_create` throughout, so the inventory is a no-op the second time."""
        generate(count=12)
        generate(count=12)
        self.assertEqual(Device.objects.count(), len(HOSTS))
        self.assertEqual(EventTicket.objects.count(), 24)


class TestAttachments(TestCase):
    """What the tickets point at."""

    @override_settings(PLUGINS_CONFIG={"nautobot_event_tracker": {"attachable_object_types": ["dcim.location"]}})
    def test_it_attaches_nothing_where_devices_may_not_be_attached(self):
        """The service layer would refuse every one of them, and it would be right to."""
        generate(count=12)
        self.assertEqual(EventTicket.objects.count(), 12)
        self.assertFalse(EventTicket.objects.filter(updates__update_type=UpdateTypeChoices.OBJECT_ATTACHED).exists())


class TestRefusals(TestCase):
    """What the command will not do."""

    def test_a_second_database_is_refused(self):
        """The service layer holds its own transactions and writes to the default connection."""
        with self.assertRaises(CommandError) as caught:
            generate(count=1, database="other")
        self.assertIn("service layer", str(caught.exception))
