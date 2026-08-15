"""Test the Event Tracker UI."""

# pylint: disable=too-many-ancestors,duplicate-code

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from nautobot.apps.choices import CustomFieldTypeChoices
from nautobot.apps.testing import TestCase, ViewTestCases
from nautobot.dcim.models import Location
from nautobot.extras.models import CustomField

from nautobot_event_tracker import forms
from nautobot_event_tracker.api.serializers import SERVICE_OWNED_FIELDS
from nautobot_event_tracker.choices import (
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.models import EventTicket, EventType
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.tests import fixtures


class EventTypeViewTest(ViewTestCases.OrganizationalObjectViewTestCase):
    """Standard view test cases for EventType."""

    model = EventType

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()
        cls.form_data = {
            "name": "View Test Type",
            "description": "created through the form",
            "default_severity": SeverityChoices.MAJOR,
            "enabled": True,
        }
        cls.bulk_edit_data = {"description": "bulk edited"}


class EventTicketViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """Standard view test cases for EventTicket."""

    model = EventTicket

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_eventticket()
        event_type = EventType.objects.get(name="Test Interface Down")
        cls.form_data = {
            "title": "Created through the form",
            "event_type": event_type.pk,
            "severity": SeverityChoices.MAJOR,
            "description": "form created",
        }


class TicketFormFieldTest(TestCase):
    """The edit forms must not expose service-owned fields."""

    def test_form_omits_service_owned_fields(self):
        """status, resolved_at, closed_at, resolution and event_count are unreachable by form."""
        form = forms.EventTicketForm()
        for field in SERVICE_OWNED_FIELDS:
            self.assertNotIn(field, form.fields, f"'{field}' must not be editable through the form")

    def test_bulk_edit_form_omits_service_owned_and_tracked_fields(self):
        """The bulk edit form is not a back door either.

        Nautobot's bulk edit assigns and saves each object itself, so a field there cannot be
        routed through the service - which rules out severity and assignee as well as status.
        """
        form = forms.EventTicketBulkEditForm(model=EventTicket)
        for field in ("status", "severity", "assigned_to"):
            self.assertNotIn(field, form.fields)


class TicketCreationRoutesThroughServiceTest(TestCase):
    """A ticket created in the UI must get its trail, like one created anywhere else."""

    def setUp(self):
        """Create the event type the form needs."""
        super().setUp()
        fixtures.create_event_types()
        self.event_type = EventType.objects.get(name="Test Interface Down")

    def test_ui_creation_writes_a_created_update(self):
        """The UI create path goes through the service layer, not straight to the ORM."""
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            # The event type picker will not offer a type the user cannot see.
            "nautobot_event_tracker.view_eventtype",
        )
        response = self.client.post(
            reverse("plugins:nautobot_event_tracker:eventticket_add"),
            {
                "title": "Made in the UI",
                "event_type": str(self.event_type.pk),
                "severity": SeverityChoices.MAJOR,
                "description": "",
                "dedup_key": "",
            },
        )
        self.assertIn(response.status_code, (200, 302), response.content[:500])

        ticket = EventTicket.objects.filter(title="Made in the UI").first()
        self.assertIsNotNone(ticket, "the ticket was not created")
        self.assertEqual(ticket.status, TicketStatusChoices.NEW)
        self.assertTrue(
            ticket.updates.filter(update_type="created").exists(),
            "a UI-created ticket must still get its 'created' trail entry",
        )

    def test_ui_creation_keeps_custom_field_values(self):
        """Routing through the service must not cost the form its own fields."""
        custom_field = CustomField.objects.create(type=CustomFieldTypeChoices.TYPE_TEXT, label="Runbook")
        custom_field.content_types.set([ContentType.objects.get_for_model(EventTicket)])
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        self.client.post(
            reverse("plugins:nautobot_event_tracker:eventticket_add"),
            {
                "title": "With a custom field",
                "event_type": str(self.event_type.pk),
                "severity": SeverityChoices.MAJOR,
                "description": "",
                "dedup_key": "",
                f"cf_{custom_field.key}": "runbook-42",
            },
        )
        ticket = EventTicket.objects.get(title="With a custom field")
        self.assertEqual(ticket.cf[custom_field.key], "runbook-42")

    def test_ui_creation_does_not_rewrite_a_deduped_ticket(self):
        """A recurrence joins the open ticket; the second submission must not overwrite it."""
        self.add_permissions(
            "nautobot_event_tracker.add_eventticket",
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        url = reverse("plugins:nautobot_event_tracker:eventticket_add")
        data = {
            "title": "First wording",
            "event_type": str(self.event_type.pk),
            "severity": SeverityChoices.MAJOR,
            "description": "",
            "dedup_key": "ui-dedup",
        }
        self.client.post(url, data)
        self.client.post(url, {**data, "title": "Second wording"})

        tickets = EventTicket.objects.filter(dedup_key="ui-dedup")
        self.assertEqual(tickets.count(), 1)
        self.assertEqual(tickets.first().title, "First wording")


class TransitionViewTest(TestCase):
    """The transition view and its buttons."""

    def setUp(self):
        """Create a ticket to transition."""
        super().setUp()
        fixtures.create_event_types()
        self.ticket = fixtures.create_ticket(user=self.user, title="Transition target")
        self.url = reverse("plugins:nautobot_event_tracker:eventticket_transition", args=[self.ticket.pk])

    def test_transition_requires_permission(self):
        """Without transition_eventticket the view refuses."""
        self.add_permissions("nautobot_event_tracker.view_eventticket")
        response = self.client.post(self.url, {"to_status": TicketStatusChoices.TRIAGED})
        self.assertHttpStatus(response, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.NEW)

    def test_transition_succeeds_with_permission(self):
        """With the permission the ticket moves, through the service layer."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.transition_eventticket",
        )
        response = self.client.post(self.url, {"to_status": TicketStatusChoices.TRIAGED})
        self.assertHttpStatus(response, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.TRIAGED)
        self.assertTrue(self.ticket.updates.filter(update_type="status_change").exists())

    def test_illegal_transition_leaves_status_untouched(self):
        """An illegal move reports an error rather than applying."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.transition_eventticket",
        )
        response = self.client.post(self.url, {"to_status": TicketStatusChoices.RESOLVED})
        self.assertHttpStatus(response, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.NEW)

    def test_detail_page_shows_only_legal_transitions(self):
        """The detail page offers exactly the legal next states and no others."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.transition_eventticket",
        )
        response = self.client.get(self.ticket.get_absolute_url())
        self.assertHttpStatus(response, 200)
        content = response.content.decode()

        legal = ticket_service.get_allowed_transitions(self.ticket)
        self.assertTrue(legal)
        for status in legal:
            self.assertIn(f"to_status={status}", content, f"'{status}' should be offered from 'new'")

        illegal = set(TicketStatusChoices.values()) - set(legal)
        for status in illegal:
            self.assertNotIn(f"to_status={status}", content, f"'{status}' must not be offered from 'new'")

    def test_transition_buttons_hidden_without_permission(self):
        """A user lacking the permission sees no transition controls at all."""
        self.add_permissions("nautobot_event_tracker.view_eventticket")
        response = self.client.get(self.ticket.get_absolute_url())
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        for status in TicketStatusChoices.values():
            self.assertNotIn(f"to_status={status}", content)

    def test_transition_menu_is_hidden_when_nothing_is_legal(self):
        """A closed ticket has no legal move, so it must not offer an empty menu."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.transition_eventticket",
        )
        closed = fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, user=self.user)
        response = self.client.get(closed.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertNotIn("Transition", response.content.decode())


class AttachViewTest(TestCase):
    """The attach and detach views and the related-objects panel."""

    def setUp(self):
        """Create a ticket and an attachable object."""
        super().setUp()
        fixtures.create_event_types()
        self.ticket = fixtures.create_ticket(user=self.user, title="Attach target")
        self.location = fixtures.create_location()
        self.attach_url = reverse("plugins:nautobot_event_tracker:eventticket_attach", args=[self.ticket.pk])
        self.detach_url = reverse("plugins:nautobot_event_tracker:eventticket_detach", args=[self.ticket.pk])
        self.location_type = ContentType.objects.get_for_model(Location)

    def test_attach_requires_permission(self):
        """Attaching needs change_eventticket."""
        self.add_permissions("nautobot_event_tracker.view_eventticket")
        response = self.client.post(
            self.attach_url, {"object_type": self.location_type.pk, "object_id": str(self.location.pk)}
        )
        self.assertHttpStatus(response, 403)

    def test_attach_and_detach(self):
        """With permission, attach and detach both route through the service layer."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        payload = {"object_type": self.location_type.pk, "object_id": str(self.location.pk)}

        response = self.client.post(self.attach_url, payload)
        self.assertHttpStatus(response, 302)
        self.assertEqual(list(ticket_service.get_related_objects(self.ticket).values()), [[self.location]])

        response = self.client.post(self.detach_url, payload)
        self.assertHttpStatus(response, 302)
        self.assertEqual(ticket_service.get_related_objects(self.ticket), {})

    def test_panel_groups_attached_objects_by_type(self):
        """The detail page groups attachments under their object type."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        ticket_service.attach_object(
            ticket=self.ticket,
            obj=self.location,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        response = self.client.get(self.ticket.get_absolute_url())
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("Locations", content)
        self.assertIn(str(self.location), content)

    def test_attach_asks_for_the_type_then_the_object(self):
        """The two-step flow: type first, then a picker over that type's objects."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        first = self.client.get(self.attach_url)
        self.assertHttpStatus(first, 200)
        self.assertIsInstance(first.context["form"], forms.AttachObjectTypeForm)

        second = self.client.post(self.attach_url, {"object_type": self.location_type.pk})
        self.assertHttpStatus(second, 200)
        form = second.context["form"]
        self.assertIsInstance(form, forms.AttachObjectForm)
        self.assertEqual(form.fields["object_id"].queryset.model, Location)

    def test_attach_rejects_a_type_off_the_allowlist(self):
        """A type the service would refuse never reaches the object picker."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        response = self.client.post(
            self.attach_url,
            {"object_type": ContentType.objects.get_for_model(EventType).pk, "object_id": str(self.location.pk)},
        )
        self.assertHttpStatus(response, 200)
        self.assertIsInstance(response.context["form"], forms.AttachObjectTypeForm)
        self.assertEqual(ticket_service.get_related_objects(self.ticket), {})

    def test_panel_offers_a_detach_control(self):
        """Every attached object carries a link to detach it (spec section 6)."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        ticket_service.attach_object(
            ticket=self.ticket,
            obj=self.location,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        response = self.client.get(self.ticket.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertIn(
            f"{self.detach_url}?object_type={self.location_type.pk}&amp;object_id={self.location.pk}",
            response.content.decode(),
        )

    def test_detach_control_is_hidden_without_permission(self):
        """A read-only user sees the attachment but no way to remove it."""
        self.add_permissions("nautobot_event_tracker.view_eventticket")
        ticket_service.attach_object(
            ticket=self.ticket,
            obj=self.location,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        response = self.client.get(self.ticket.get_absolute_url())
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(str(self.location), content)
        self.assertNotIn(self.detach_url, content)

    def test_detach_confirmation_page_renders(self):
        """The detach link leads to a confirmation, not a bare POST target."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        response = self.client.get(
            f"{self.detach_url}?object_type={self.location_type.pk}&object_id={self.location.pk}"
        )
        self.assertHttpStatus(response, 200)
        self.assertIsInstance(response.context["form"], forms.DetachObjectForm)

    def test_object_names_are_shown_verbatim(self):
        """An object's own name is not re-title-cased as if it were a field label."""
        self.add_permissions("nautobot_event_tracker.view_eventticket")
        location = fixtures.create_location(name="edge_rtr_01")
        ticket_service.attach_object(
            ticket=self.ticket,
            obj=location,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        response = self.client.get(self.ticket.get_absolute_url())
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("edge_rtr_01", content)
        self.assertNotIn("Edge Rtr 01", content)

    def test_attach_button_hidden_on_closed_tickets(self):
        """A closed ticket offers no attach control."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
        )
        closed = fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, user=self.user)
        response = self.client.get(closed.get_absolute_url())
        self.assertHttpStatus(response, 200)
        attach_url = reverse("plugins:nautobot_event_tracker:eventticket_attach", args=[closed.pk])
        self.assertNotIn(attach_url, response.content.decode())


class TicketEditRoutesThroughServiceTest(TestCase):
    """Editing a ticket in the UI must leave the same trail as any other mutation."""

    def setUp(self):
        """A ticket to edit, and the permissions to edit it."""
        super().setUp()
        fixtures.create_event_types()
        self.event_type = EventType.objects.get(name="Test Interface Down")
        self.ticket = fixtures.create_ticket(
            user=self.user,
            event_type=self.event_type,
            title="Editable",
            severity=SeverityChoices.MINOR,
        )
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.change_eventticket",
            "nautobot_event_tracker.view_eventtype",
        )
        self.edit_url = reverse("plugins:nautobot_event_tracker:eventticket_edit", args=[self.ticket.pk])

    def _post_edit(self, **overrides):
        data = {
            "title": self.ticket.title,
            "event_type": str(self.event_type.pk),
            "severity": self.ticket.severity,
            "description": "",
            "dedup_key": "",
            **overrides,
        }
        return self.client.post(self.edit_url, data)

    def test_severity_change_writes_an_update(self):
        """A severity edit records who changed it and from what."""
        self._post_edit(severity=SeverityChoices.CRITICAL)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.severity, SeverityChoices.CRITICAL)
        update = self.ticket.updates.filter(update_type=UpdateTypeChoices.SEVERITY_CHANGE).last()
        self.assertIsNotNone(update)
        self.assertEqual(update.user, self.user)
        self.assertIn(SeverityChoices.MINOR, update.message)

    def test_assignment_change_writes_an_update(self):
        """So does an assignment."""
        self._post_edit(assigned_to=str(self.user.pk))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to, self.user)
        self.assertTrue(self.ticket.updates.filter(update_type=UpdateTypeChoices.ASSIGNMENT).exists())

    def test_editing_something_else_writes_no_spurious_update(self):
        """An unchanged severity is a no-op, as it is everywhere else in the service."""
        self._post_edit(description="just a description")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.description, "just a description")
        self.assertFalse(self.ticket.updates.filter(update_type=UpdateTypeChoices.SEVERITY_CHANGE).exists())
        self.assertFalse(self.ticket.updates.filter(update_type=UpdateTypeChoices.ASSIGNMENT).exists())


class UpdateTrailViewTest(TestCase):
    """The update trail panel."""

    def test_trail_renders_and_offers_no_edit_controls(self):
        """Updates appear on the page, with no edit or delete affordance."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_ticketupdate",
        )
        fixtures.create_event_types()
        ticket = fixtures.create_ticket(user=self.user, title="Trail target")
        ticket_service.add_comment(
            ticket=ticket,
            message="a distinctive comment",
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        response = self.client.get(ticket.get_absolute_url())
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("a distinctive comment", content)
        # There is no edit or delete route for an update, so no URL for one can appear.
        self.assertNotIn("ticketupdate_edit", content)
        self.assertNotIn("ticketupdate_delete", content)

    def test_trail_is_hidden_without_permission_to_view_updates(self):
        """A user who may see a ticket but not its updates gets the ticket without the trail."""
        self.add_permissions("nautobot_event_tracker.view_eventticket")
        fixtures.create_event_types()
        ticket = fixtures.create_ticket(user=self.user, title="Hidden trail")
        ticket_service.add_comment(
            ticket=ticket,
            message="a distinctive comment",
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        response = self.client.get(ticket.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertNotIn("a distinctive comment", response.content.decode())
