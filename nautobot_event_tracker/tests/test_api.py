"""Test the Event Tracker REST API."""

# pylint: disable=too-many-ancestors,duplicate-code

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from nautobot.apps.choices import CustomFieldTypeChoices
from nautobot.apps.testing import APITestCase, APIViewTestCases
from nautobot.extras.models import CustomField

from nautobot_event_tracker.choices import SeverityChoices, TicketStatusChoices, UpdateTypeChoices
from nautobot_event_tracker.models import EventTicket, EventType
from nautobot_event_tracker.tests import fixtures


class EventTypeAPITest(APIViewTestCases.APIViewTestCase):
    """Standard API test cases for EventType."""

    model = EventType
    bulk_update_data = {"description": "Bulk updated"}
    choices_fields = ["default_severity"]

    create_data = [
        {"name": "API Type One", "default_severity": SeverityChoices.MAJOR},
        {"name": "API Type Two", "default_severity": SeverityChoices.MINOR},
        {"name": "API Type Three", "default_severity": SeverityChoices.INFO},
    ]

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()


class EventTicketAPITest(APITestCase):
    """Behavioural tests for the ticket API. Every mutation must route through the service layer."""

    def setUp(self):
        """Create a ticket and the objects the actions need."""
        super().setUp()
        fixtures.create_event_types()
        self.event_type = EventType.objects.get(name="Test Interface Down")
        self.ticket = fixtures.create_ticket(user=self.user, event_type=self.event_type)
        self.location = fixtures.create_location()
        self.detail_url = reverse("plugins-api:nautobot_event_tracker-api:eventticket-detail", args=[self.ticket.pk])

    def _url(self, action):
        return reverse(f"plugins-api:nautobot_event_tracker-api:eventticket-{action}", args=[self.ticket.pk])

    # --- direct status writes are rejected, not ignored ---

    def test_patch_status_is_rejected(self):
        """A PATCH carrying status must fail loudly, not silently drop the field."""
        self.add_permissions("nautobot_event_tracker.change_eventticket")
        response = self.client.patch(
            self.detail_url,
            {"status": TicketStatusChoices.CLOSED},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, 400)
        self.assertIn("status", response.data)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.NEW)

    def test_patch_other_service_owned_fields_is_rejected(self):
        """The same protection covers the other service-owned fields."""
        self.add_permissions("nautobot_event_tracker.change_eventticket")
        for field, value in [
            ("resolved_at", "2026-01-01T00:00:00Z"),
            ("closed_at", "2026-01-01T00:00:00Z"),
            ("resolution", "sneaky"),
            ("event_count", 99),
        ]:
            with self.subTest(field):
                response = self.client.patch(self.detail_url, {field: value}, format="json", **self.header)
                self.assertHttpStatus(response, 400)
                self.assertIn(field, response.data)

    def test_patch_allowed_field_still_works(self):
        """Ordinary ticket content remains editable."""
        self.add_permissions("nautobot_event_tracker.change_eventticket")
        response = self.client.patch(self.detail_url, {"description": "updated"}, format="json", **self.header)
        self.assertHttpStatus(response, 200)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.description, "updated")

    # --- transition action ---

    def test_transition_get_reports_allowed_states(self):
        """A client can read the legal next states rather than keeping its own graph."""
        self.add_permissions("nautobot_event_tracker.view_eventticket")
        response = self.client.get(self._url("transition"), **self.header)
        self.assertHttpStatus(response, 200)
        self.assertEqual(
            set(response.data["allowed_transitions"]),
            {TicketStatusChoices.TRIAGED, TicketStatusChoices.SUPPRESSED, TicketStatusChoices.CLOSED},
        )

    def test_transition_requires_the_custom_permission(self):
        """change_eventticket alone is not enough to move a ticket."""
        self.add_permissions("nautobot_event_tracker.change_eventticket")
        response = self.client.post(
            self._url("transition"), {"to_status": TicketStatusChoices.TRIAGED}, format="json", **self.header
        )
        self.assertHttpStatus(response, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.NEW)

    def test_transition_succeeds_with_permission(self):
        """With the permission, the transition goes through the service layer."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.transition_eventticket",
        )
        response = self.client.post(
            self._url("transition"), {"to_status": TicketStatusChoices.TRIAGED}, format="json", **self.header
        )
        self.assertHttpStatus(response, 200)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.TRIAGED)
        self.assertTrue(self.ticket.updates.filter(update_type=UpdateTypeChoices.STATUS_CHANGE).exists())

    def test_illegal_transition_returns_409(self):
        """A move off the graph is a conflict, not a bad request."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.transition_eventticket",
        )
        response = self.client.post(
            self._url("transition"), {"to_status": TicketStatusChoices.RESOLVED}, format="json", **self.header
        )
        self.assertHttpStatus(response, 409)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.NEW)

    def test_resolving_without_resolution_returns_400(self):
        """A missing resolution is a validation problem."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.transition_eventticket",
        )
        self.client.post(
            self._url("transition"), {"to_status": TicketStatusChoices.TRIAGED}, format="json", **self.header
        )
        response = self.client.post(
            self._url("transition"), {"to_status": TicketStatusChoices.RESOLVED}, format="json", **self.header
        )
        self.assertHttpStatus(response, 400)

    # --- comment action ---

    def test_comment_appends_an_update(self):
        """Commenting writes one row through the service layer."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        response = self.client.post(self._url("comment"), {"message": "from the API"}, format="json", **self.header)
        self.assertHttpStatus(response, 201)
        self.assertTrue(
            self.ticket.updates.filter(update_type=UpdateTypeChoices.COMMENT, message="from the API").exists()
        )

    def test_comment_requires_change_permission(self):
        """Appending to a ticket's trail is a change to the ticket."""
        self.add_permissions("nautobot_event_tracker.view_eventticket")
        response = self.client.post(self._url("comment"), {"message": "nope"}, format="json", **self.header)
        self.assertHttpStatus(response, 403)

    # --- attach and detach ---

    def test_attach_and_detach_round_trip(self):
        """Attaching then detaching leaves the object off the ticket but both rows in the trail."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        payload = {"object_type": "dcim.location", "object_id": str(self.location.pk)}

        response = self.client.post(self._url("attach"), payload, format="json", **self.header)
        self.assertHttpStatus(response, 201)

        response = self.client.post(self._url("detach"), payload, format="json", **self.header)
        self.assertHttpStatus(response, 201)

        self.assertEqual(
            self.ticket.updates.filter(
                update_type__in=[UpdateTypeChoices.OBJECT_ATTACHED, UpdateTypeChoices.OBJECT_DETACHED]
            ).count(),
            2,
        )

    def test_attaching_a_disallowed_type_returns_400(self):
        """The allowlist is enforced at the API boundary too."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        response = self.client.post(
            self._url("attach"),
            {"object_type": "nautobot_event_tracker.eventtype", "object_id": str(self.event_type.pk)},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, 400)

    def test_attaching_an_unknown_type_returns_400(self):
        """A bogus content type is a validation error."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        response = self.client.post(
            self._url("attach"),
            {"object_type": "nope.nothing", "object_id": str(self.location.pk)},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, 400)

    def test_attaching_twice_returns_204(self):
        """A no-op attach reports "nothing happened" rather than inventing a row."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        payload = {"object_type": "dcim.location", "object_id": str(self.location.pk)}
        self.client.post(self._url("attach"), payload, format="json", **self.header)
        response = self.client.post(self._url("attach"), payload, format="json", **self.header)
        self.assertHttpStatus(response, 204)

    # --- creation routes through the service ---

    def test_create_routes_through_the_service(self):
        """An API-created ticket gets its created update and starts in new."""
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        url = reverse("plugins-api:nautobot_event_tracker-api:eventticket-list")
        response = self.client.post(
            url,
            {
                "title": "Created through the API",
                "event_type": str(self.event_type.pk),
                "severity": SeverityChoices.MAJOR,
            },
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, 201)
        ticket = EventTicket.objects.get(pk=response.data["id"])
        self.assertEqual(ticket.status, TicketStatusChoices.NEW)
        self.assertTrue(ticket.updates.filter(update_type=UpdateTypeChoices.CREATED).exists())

    def test_bulk_create(self):
        """A list payload creates one ticket per entry, each through the service."""
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        url = reverse("plugins-api:nautobot_event_tracker-api:eventticket-list")
        response = self.client.post(
            url,
            [
                {"title": "Bulk one", "event_type": str(self.event_type.pk), "severity": SeverityChoices.MAJOR},
                {"title": "Bulk two", "event_type": str(self.event_type.pk), "severity": SeverityChoices.MINOR},
            ],
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, 201)
        for title in ("Bulk one", "Bulk two"):
            ticket = EventTicket.objects.get(title=title)
            self.assertTrue(ticket.updates.filter(update_type=UpdateTypeChoices.CREATED).exists())

    def test_create_keeps_custom_field_values(self):
        """The serializer's own fields survive the trip through the service layer."""
        custom_field = CustomField.objects.create(type=CustomFieldTypeChoices.TYPE_TEXT, label="Runbook")
        custom_field.content_types.set([ContentType.objects.get_for_model(EventTicket)])
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        url = reverse("plugins-api:nautobot_event_tracker-api:eventticket-list")
        response = self.client.post(
            url,
            {
                "title": "With a custom field",
                "event_type": str(self.event_type.pk),
                "severity": SeverityChoices.MAJOR,
                "custom_fields": {custom_field.key: "runbook-42"},
            },
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, 201)
        ticket = EventTicket.objects.get(title="With a custom field")
        self.assertEqual(ticket.cf[custom_field.key], "runbook-42")

    def test_create_honours_a_supplied_id(self):
        """A client may choose the ticket's ID, as it may for any other Nautobot object."""
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        chosen = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        response = self.client.post(
            reverse("plugins-api:nautobot_event_tracker-api:eventticket-list"),
            {
                "id": chosen,
                "title": "Chosen ID",
                "event_type": str(self.event_type.pk),
                "severity": SeverityChoices.MAJOR,
            },
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, 201)
        self.assertEqual(str(EventTicket.objects.get(title="Chosen ID").pk), chosen)

    # --- updates route through the service too ---

    def test_patch_severity_writes_an_update(self):
        """A severity change carries its own update type, so it cannot be a silent write."""
        self.add_permissions("nautobot_event_tracker.change_eventticket")
        response = self.client.patch(
            self.detail_url, {"severity": SeverityChoices.CRITICAL}, format="json", **self.header
        )
        self.assertHttpStatus(response, 200)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.severity, SeverityChoices.CRITICAL)
        update = self.ticket.updates.get(update_type=UpdateTypeChoices.SEVERITY_CHANGE)
        self.assertEqual(update.user, self.user)

    def test_patch_assignment_writes_an_update(self):
        """So does an assignment."""
        self.add_permissions("nautobot_event_tracker.change_eventticket")
        response = self.client.patch(self.detail_url, {"assigned_to": str(self.user.pk)}, format="json", **self.header)
        self.assertHttpStatus(response, 200)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to, self.user)
        self.assertTrue(self.ticket.updates.filter(update_type=UpdateTypeChoices.ASSIGNMENT).exists())

    def test_create_applies_dedup(self):
        """The API create path inherits rule S5."""
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        url = reverse("plugins-api:nautobot_event_tracker-api:eventticket-list")
        payload = {
            "title": "Deduped",
            "event_type": str(self.event_type.pk),
            "severity": SeverityChoices.MAJOR,
            "dedup_key": "api-dedup",
        }
        first = self.client.post(url, payload, format="json", **self.header)
        second = self.client.post(url, payload, format="json", **self.header)
        self.assertHttpStatus(first, 201)
        self.assertEqual(EventTicket.objects.filter(dedup_key="api-dedup").count(), 1)
        self.assertEqual(second.data["id"], first.data["id"])


class TicketUpdateAPITest(APITestCase):
    """The ticket-updates endpoint must be read-only."""

    def setUp(self):
        """Create a ticket with a trail."""
        super().setUp()
        fixtures.create_event_types()
        self.ticket = fixtures.create_ticket(user=self.user)
        self.update = self.ticket.updates.first()
        self.list_url = reverse("plugins-api:nautobot_event_tracker-api:ticketupdate-list")
        self.detail_url = reverse("plugins-api:nautobot_event_tracker-api:ticketupdate-detail", args=[self.update.pk])

    def test_list_is_readable(self):
        """Reading the trail works."""
        self.add_permissions("nautobot_event_tracker.view_ticketupdate")
        response = self.client.get(self.list_url, **self.header)
        self.assertHttpStatus(response, 200)

    def test_post_is_rejected(self):
        """There is no create route: updates are written by the service layer alone."""
        self.add_permissions("nautobot_event_tracker.add_ticketupdate")
        response = self.client.post(self.list_url, {"message": "nope"}, format="json", **self.header)
        self.assertHttpStatus(response, 405)

    def test_patch_is_rejected(self):
        """Append-only means no edit route."""
        self.add_permissions("nautobot_event_tracker.change_ticketupdate")
        response = self.client.patch(self.detail_url, {"message": "nope"}, format="json", **self.header)
        self.assertHttpStatus(response, 405)

    def test_put_is_rejected(self):
        """Nor a replace route."""
        self.add_permissions("nautobot_event_tracker.change_ticketupdate")
        response = self.client.put(self.detail_url, {"message": "nope"}, format="json", **self.header)
        self.assertHttpStatus(response, 405)

    def test_delete_is_rejected(self):
        """Nor a delete route."""
        self.add_permissions("nautobot_event_tracker.delete_ticketupdate")
        response = self.client.delete(self.detail_url, **self.header)
        self.assertHttpStatus(response, 405)


class TestCreateRejectsADisabledEventType(APITestCase):
    """A service refusal on create is reported, not raised through as a 500."""

    def setUp(self):
        """Create test data."""
        super().setUp()
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        self.disabled = EventType.objects.create(name="Retired Type", enabled=False)

    def _post(self):
        """Try to open a ticket against the disabled type."""
        return self.client.post(
            reverse("plugins-api:nautobot_event_tracker-api:eventticket-list"),
            {
                "title": "Should not open",
                "event_type": str(self.disabled.pk),
                "severity": SeverityChoices.MAJOR,
            },
            format="json",
            **self.header,
        )

    def test_a_disabled_event_type_is_a_bad_request(self):
        """The service refuses it, and a refusal the client caused is a 400, not a 500."""
        self.assertEqual(self._post().status_code, 400)

    def test_the_message_says_which_type(self):
        """An operator should not have to read a traceback to find out which."""
        self.assertIn("Retired Type", str(self._post().data))

    def test_no_ticket_is_left_behind(self):
        """The refusal happens inside the create transaction."""
        self._post()
        self.assertFalse(EventTicket.objects.filter(title="Should not open").exists())
