"""Test the test data command.

The command ships in the app package, so somebody will read it as an example of how to make a
ticket. These tests hold it to the same rule the rest of the app follows: every ticket goes through
the service layer, and a ticket in a terminal state got there by walking the graph.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from nautobot_event_tracker.choices import TicketStatusChoices, UpdateTypeChoices
from nautobot_event_tracker.management.commands.generate_nautobot_event_tracker_test_data import TEST_DATA_TAG
from nautobot_event_tracker.models import EventTicket, IngestionStats
from nautobot_event_tracker.tests import fixtures


def generate(**options):
    """Run the command, returning what it printed."""
    out = StringIO()
    call_command("generate_nautobot_event_tracker_test_data", stdout=out, **options)
    return out.getvalue()


class TestGeneratedTickets(TestCase):
    """What one run produces."""

    def test_it_creates_the_requested_number(self):
        """`--count` is exact, not approximate: the status mix rounds into it."""
        generate(count=12)
        self.assertEqual(EventTicket.objects.count(), 12)

    def test_every_ticket_carries_the_tag(self):
        """The tag is what makes `--flush` able to delete exactly what this made."""
        generate(count=12)
        self.assertEqual(EventTicket.objects.filter(tags__name=TEST_DATA_TAG).count(), 12)

    def test_every_ticket_has_a_created_update(self):
        """Which only the service layer writes - a direct ORM create would have none."""
        generate(count=12)
        for ticket in EventTicket.objects.all():
            self.assertTrue(
                ticket.updates.filter(update_type=UpdateTypeChoices.CREATED).exists(),
                f"{ticket} has no created update, so it did not come from the service layer",
            )

    def test_the_default_run_covers_every_status(self):
        """A demo list where a state is missing is a demo of the wrong thing."""
        generate()
        present = set(EventTicket.objects.values_list("status", flat=True))
        self.assertEqual(present, set(TicketStatusChoices.values()))

    def test_a_terminal_ticket_walked_the_graph_to_get_there(self):
        """Its trail has to show every step, not a status that appeared from nowhere."""
        generate()
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
        generate()
        for ticket in EventTicket.objects.filter(status=TicketStatusChoices.RESOLVED):
            self.assertTrue(ticket.resolution)

    def test_tickets_have_histories_worth_looking_at(self):
        """A demo whose tickets have a single line of trail demonstrates nothing."""
        generate()
        comments = EventTicket.objects.filter(updates__update_type=UpdateTypeChoices.COMMENT).distinct().count()
        self.assertGreater(comments, 0)

    def test_no_ingestion_counters_are_invented(self):
        """Counters are a record of a consumer having run; fabricating them would be a lie."""
        generate(count=12)
        self.assertFalse(IngestionStats.objects.exists())


class TestDeterminism(TestCase):
    """The same seed produces the same data."""

    def test_the_same_seed_produces_the_same_titles(self):
        """Two people comparing screenshots should be looking at the same tickets."""
        generate(count=8, seed=99)
        first = list(EventTicket.objects.order_by("title").values_list("title", flat=True))

        call_command("generate_nautobot_event_tracker_test_data", flush=True, count=8, seed=99, stdout=StringIO())
        second = list(EventTicket.objects.order_by("title").values_list("title", flat=True))

        self.assertEqual(first, second)

    def test_a_different_seed_produces_different_data(self):
        """Otherwise the seed would be decoration."""
        generate(count=8, seed=1)
        first = list(EventTicket.objects.order_by("id").values_list("title", flat=True))

        call_command("generate_nautobot_event_tracker_test_data", flush=True, count=8, seed=2, stdout=StringIO())
        second = list(EventTicket.objects.order_by("id").values_list("title", flat=True))

        self.assertNotEqual(first, second)


class TestFlush(TestCase):
    """`--flush` removes what the command made, and nothing else."""

    def test_it_deletes_the_generated_tickets(self):
        """Running twice should not leave a hundred tickets behind."""
        generate(count=8)
        call_command("generate_nautobot_event_tracker_test_data", flush=True, count=8, stdout=StringIO())
        self.assertEqual(EventTicket.objects.count(), 8)

    def test_it_leaves_a_persons_own_tickets_alone(self):
        """This is the whole reason the tag exists."""
        mine = fixtures.create_ticket(title="Opened by a person")
        generate(count=8)

        call_command("generate_nautobot_event_tracker_test_data", flush=True, count=4, stdout=StringIO())

        self.assertTrue(EventTicket.objects.filter(pk=mine.pk).exists())
        self.assertEqual(EventTicket.objects.count(), 5)

    def test_it_reports_what_it_deleted(self):
        """An operator running this against a populated database wants the number."""
        generate(count=8)
        out = StringIO()
        call_command("generate_nautobot_event_tracker_test_data", flush=True, count=4, stdout=out)
        self.assertIn("Deleted 8", out.getvalue())


class TestAttachments(TestCase):
    """What the tickets point at."""

    def test_it_creates_no_network_objects_of_its_own(self):
        """A ticketing app inventing devices is how a demo database becomes inexplicable."""
        from nautobot.dcim.models import Device  # pylint: disable=import-outside-toplevel

        before = Device.objects.count()
        generate(count=12)
        self.assertEqual(Device.objects.count(), before)

    def test_it_attaches_objects_that_already_exist(self):
        """Where the database holds something attachable, the tickets point at it."""
        fixtures.create_location()
        generate()
        attached = EventTicket.objects.filter(updates__update_type=UpdateTypeChoices.OBJECT_ATTACHED).distinct()
        self.assertTrue(attached.exists())


class TestRefusals(TestCase):
    """What the command will not do."""

    def test_a_second_database_is_refused(self):
        """The service layer holds its own transactions and writes to the default connection."""
        with self.assertRaises(CommandError) as caught:
            generate(count=1, database="other")
        self.assertIn("service layer", str(caught.exception))
