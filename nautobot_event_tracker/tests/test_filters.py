"""Test the Event Tracker filtersets."""

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from nautobot.dcim.models import Location

from nautobot_event_tracker.choices import SeverityChoices, TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.filters import EventTicketFilterSet, EventTypeFilterSet, TicketUpdateFilterSet
from nautobot_event_tracker.models import EventTicket, EventType, TicketUpdate
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.tests import fixtures


class EventTypeFilterTest(TestCase):
    """Filters for EventType."""

    queryset = EventType.objects.all()
    filterset = EventTypeFilterSet

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def test_q_matches_name(self):
        """Search matches the name."""
        params = {"q": "Interface"}
        self.assertTrue(self.filterset(params, self.queryset).qs.filter(name__contains="Interface").exists())

    def test_q_matches_description(self):
        """Search also matches the description."""
        EventType.objects.create(name="Searchable", description="a very distinctive phrase")
        params = {"q": "distinctive"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_name(self):
        """Exact name filter."""
        params = {"name": ["Test Interface Down"]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_default_severity(self):
        """Severity filter."""
        params = {"default_severity": [SeverityChoices.CRITICAL]}
        self.assertTrue(self.filterset(params, self.queryset).qs.exists())

    def test_enabled(self):
        """Enabled flag filter, both ways."""
        self.assertFalse(self.filterset({"enabled": False}, self.queryset).qs.filter(enabled=True).exists())
        self.assertFalse(self.filterset({"enabled": True}, self.queryset).qs.filter(enabled=False).exists())


class EventTicketFilterTest(TestCase):
    """Filters for EventTicket."""

    queryset = EventTicket.objects.all()
    filterset = EventTicketFilterSet

    @classmethod
    def setUpTestData(cls):
        """Create tickets across several statuses, severities and assignments."""
        cls.user = fixtures.create_user()
        cls.other_user = fixtures.create_user("someone-else")
        cls.event_types = fixtures.create_event_types()
        cls.location = fixtures.create_location()

        cls.new_ticket = fixtures.create_ticket(
            user=cls.user, event_type=cls.event_types[0], title="Alpha ticket", dedup_key="alpha"
        )
        cls.resolved_ticket = fixtures.create_ticket_in_status(
            TicketStatusChoices.RESOLVED, user=cls.user, title="Beta ticket"
        )
        cls.closed_ticket = fixtures.create_ticket_in_status(
            TicketStatusChoices.CLOSED, user=cls.user, title="Gamma ticket"
        )

        ticket_service.assign(
            ticket=cls.new_ticket,
            assignee=cls.other_user,
            source=TicketSourceChoices.HUMAN,
            user=cls.user,
        )
        ticket_service.attach_object(
            ticket=cls.new_ticket,
            obj=cls.location,
            source=TicketSourceChoices.HUMAN,
            user=cls.user,
        )

    def _filter(self, params):
        return self.filterset(params, self.queryset).qs

    def test_q_matches_title(self):
        """Search matches the title."""
        self.assertEqual(self._filter({"q": "Alpha"}).count(), 1)

    def test_q_matches_event_type_name(self):
        """Search reaches through to the event type name."""
        self.assertTrue(self._filter({"q": "Interface"}).exists())

    def test_status(self):
        """Status filter accepts multiple values."""
        results = self._filter({"status": [TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED]})
        self.assertEqual(results.count(), 2)

    def test_severity(self):
        """Severity filter."""
        self.assertTrue(self._filter({"severity": [SeverityChoices.MAJOR]}).exists())

    def test_source(self):
        """Source filter."""
        self.assertEqual(self._filter({"source": [TicketSourceChoices.HUMAN]}).count(), 3)
        self.assertEqual(self._filter({"source": [TicketSourceChoices.AI]}).count(), 0)

    def test_event_type_by_name(self):
        """The natural key filter takes a name."""
        self.assertTrue(self._filter({"event_type": ["Test Interface Down"]}).exists())

    def test_assigned_to_by_username(self):
        """Assignment filter takes a username."""
        results = self._filter({"assigned_to": ["someone-else"]})
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first().pk, self.new_ticket.pk)

    def test_has_assignee(self):
        """Membership filter, both directions."""
        self.assertEqual(self._filter({"has_assignee": True}).count(), 1)
        self.assertEqual(self._filter({"has_assignee": False}).count(), 2)

    def test_is_open(self):
        """is_open must agree with the terminal status set."""
        self.assertEqual(self._filter({"is_open": True}).count(), 1)
        self.assertEqual(self._filter({"is_open": False}).count(), 2)

    def test_dedup_key(self):
        """Dedup key filter."""
        self.assertEqual(self._filter({"dedup_key": ["alpha"]}).count(), 1)

    def test_event_count(self):
        """Event count filter."""
        self.assertEqual(self._filter({"event_count": [1]}).count(), 3)

    def test_last_seen(self):
        """Date filters are wired up."""
        self.assertTrue(self._filter({"last_seen__gte": ["2000-01-01T00:00:00Z"]}).exists())

    def test_resolved_at_and_closed_at(self):
        """Terminal timestamps are filterable."""
        self.assertEqual(self._filter({"resolved_at__gte": ["2000-01-01T00:00:00Z"]}).count(), 2)
        self.assertEqual(self._filter({"closed_at__gte": ["2000-01-01T00:00:00Z"]}).count(), 1)

    def test_related_object_type_matches_attached(self):
        """A ticket with a location attached matches the location content type."""
        content_type = ContentType.objects.get_for_model(Location)
        results = self._filter({"related_object_type": [content_type.pk]})
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first().pk, self.new_ticket.pk)

    def test_related_object_type_excludes_detached(self):
        """An object attached and then detached must stop matching.

        This is the case a plain join would get wrong, which is why the filter replays the trail.
        """
        ticket_service.detach_object(
            ticket=self.new_ticket,
            obj=self.location,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        content_type = ContentType.objects.get_for_model(Location)
        self.assertEqual(self._filter({"related_object_type": [content_type.pk]}).count(), 0)


class TicketUpdateFilterTest(TestCase):
    """Filters for TicketUpdate."""

    queryset = TicketUpdate.objects.all()
    filterset = TicketUpdateFilterSet

    @classmethod
    def setUpTestData(cls):
        """Create a ticket with a varied trail."""
        cls.user = fixtures.create_user()
        fixtures.create_event_types()
        cls.ticket = fixtures.create_ticket(user=cls.user)
        ticket_service.add_comment(
            ticket=cls.ticket,
            message="a distinctive human comment",
            source=TicketSourceChoices.HUMAN,
            user=cls.user,
        )
        ticket_service.add_comment(ticket=cls.ticket, message="an AI note", source=TicketSourceChoices.AI)

    def _filter(self, params):
        return self.filterset(params, self.queryset).qs

    def test_q_matches_message(self):
        """Search matches the message body."""
        self.assertEqual(self._filter({"q": "distinctive"}).count(), 1)

    def test_ticket(self):
        """Filter by ticket."""
        self.assertEqual(self._filter({"ticket": [self.ticket.pk]}).count(), 3)

    def test_update_type(self):
        """Filter by update type."""
        self.assertEqual(self._filter({"update_type": ["comment"]}).count(), 2)

    def test_source(self):
        """Filter by source."""
        self.assertEqual(self._filter({"source": [TicketSourceChoices.AI]}).count(), 1)

    def test_user_by_username(self):
        """Filter by acting username; AI rows have no user and must not match."""
        results = self._filter({"user": [self.user.username]})
        self.assertTrue(results.exists())
        self.assertFalse(results.filter(source=TicketSourceChoices.AI).exists())
