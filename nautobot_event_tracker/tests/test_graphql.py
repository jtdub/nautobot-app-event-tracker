"""Test the Event Tracker GraphQL surface."""

from nautobot.apps.testing import TestCase
from nautobot.core.graphql import execute_query

from nautobot_event_tracker.choices import TicketSourceChoices
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.tests import fixtures


class EventTrackerGraphQLTest(TestCase):
    """All three models must be queryable through GraphQL."""

    def setUp(self):
        """Create a ticket with a trail and an attachment."""
        super().setUp()
        fixtures.create_event_types()
        self.ticket = fixtures.create_ticket(user=self.user, title="GraphQL ticket")
        self.location = fixtures.create_location()

        ticket_service.add_comment(
            ticket=self.ticket,
            message="a graphql comment",
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        ticket_service.attach_object(
            ticket=self.ticket,
            obj=self.location,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )

    def _query(self, query):
        """Run a GraphQL query as a superuser and assert it did not error."""
        self.user.is_superuser = True
        self.user.save()
        result = execute_query(query, user=self.user)
        self.assertIsNone(result.errors, f"GraphQL errors: {result.errors}")
        return result.data

    def test_event_types_are_queryable(self):
        """EventType is exposed through extras_features."""
        data = self._query("{ event_types { name default_severity enabled } }")
        self.assertTrue(data["event_types"])

    def test_event_tickets_are_queryable(self):
        """EventTicket is exposed through extras_features."""
        data = self._query("{ event_tickets { title status severity source event_count } }")
        titles = [ticket["title"] for ticket in data["event_tickets"]]
        self.assertIn("GraphQL ticket", titles)

    def test_ticket_updates_are_queryable(self):
        """TicketUpdate needs its own type because it is not a PrimaryModel."""
        data = self._query("{ ticket_updates { update_type source message } }")
        messages = [update["message"] for update in data["ticket_updates"]]
        self.assertIn("a graphql comment", messages)

    def test_ticket_updates_expose_the_related_object_type(self):
        """The generic pointer surfaces as a queryable ContentType relation."""
        data = self._query(
            "{ ticket_updates { update_type related_object_type { app_label model } related_object_id } }"
        )
        labels = {
            f"{update['related_object_type']['app_label']}.{update['related_object_type']['model']}"
            for update in data["ticket_updates"]
            if update["related_object_type"]
        }
        self.assertIn("dcim.location", labels)

    def test_nested_updates_on_a_ticket(self):
        """A ticket query can walk into its trail."""
        data = self._query("{ event_tickets { title updates { update_type message } } }")
        ticket = next(t for t in data["event_tickets"] if t["title"] == "GraphQL ticket")
        self.assertGreaterEqual(len(ticket["updates"]), 2)
