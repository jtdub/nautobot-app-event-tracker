"""Test the Event Tracker UI."""

# pylint: disable=too-many-ancestors,duplicate-code

from decimal import Decimal
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured as DjangoImproperlyConfigured
from django.urls import NoReverseMatch, reverse
from nautobot.apps.choices import CustomFieldTypeChoices
from nautobot.apps.testing import TestCase, ViewTestCases
from nautobot.dcim.models import Location
from nautobot.extras.models import CustomField

from nautobot_event_tracker import forms
from nautobot_event_tracker.api.serializers import SERVICE_OWNED_FIELDS
from nautobot_event_tracker.choices import (
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
    LLMModelKindChoices,
    LLMProviderTypeChoices,
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.models import (
    AgentRun,
    AgentToolCall,
    EventTicket,
    EventType,
    LLMModel,
    LLMProvider,
    LLMUsageRecord,
    MCPServer,
    MCPTool,
    TicketEmbedding,
)
from nautobot_event_tracker.services import agent as agent_service
from nautobot_event_tracker.services import mcp as mcp_service
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import MCPCallError
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


class TestDetachingSomethingNotAttached(TestCase):
    """The detach view mirrors the attach view when nothing happened."""

    def setUp(self):
        """Create test data."""
        super().setUp()
        self.user = fixtures.create_user()
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        self.ticket = fixtures.create_ticket(user=self.user)
        self.location = fixtures.create_location()

    def test_a_no_op_detach_does_not_claim_to_have_detached(self):
        """The service returns None, and the page must not report a success that did not happen."""
        response = self.client.post(
            reverse("plugins:nautobot_event_tracker:eventticket_detach", kwargs={"pk": self.ticket.pk}),
            {
                "object_type": ContentType.objects.get_for_model(self.location).pk,
                "object_id": str(self.location.pk),
            },
            follow=True,
        )
        text = response.content.decode()
        self.assertIn("is not attached to this ticket", text)
        self.assertNotIn(f"Detached {self.location}", text)

    def test_a_no_op_detach_writes_no_update(self):
        """Append-only means nothing, if a no-op appends."""
        before = self.ticket.updates.count()
        self.client.post(
            reverse("plugins:nautobot_event_tracker:eventticket_detach", kwargs={"pk": self.ticket.pk}),
            {
                "object_type": ContentType.objects.get_for_model(self.location).pk,
                "object_id": str(self.location.pk),
            },
        )
        self.assertEqual(self.ticket.updates.count(), before)


class LLMProviderViewTest(ViewTestCases.PrimaryObjectViewTestCase):
    """Standard view test cases for LLMProvider."""

    model = LLMProvider

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_llmprovider(name="Provider One")
        fixtures.create_llmprovider(name="Provider Two")
        fixtures.create_llmprovider(name="Provider Three")
        integration = fixtures.create_external_integration()
        cls.form_data = {
            "name": "View Test Provider",
            "description": "created through the form",
            "provider_type": LLMProviderTypeChoices.OPENAI_COMPATIBLE,
            "external_integration": integration.pk,
            "enabled": True,
        }
        cls.bulk_edit_data = {"description": "bulk edited"}


class LLMModelViewTest(ViewTestCases.PrimaryObjectViewTestCase):
    """Standard view test cases for LLMModel."""

    model = LLMModel

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        provider = fixtures.create_llmprovider()
        fixtures.create_llmmodel(name="model-one", provider=provider)
        fixtures.create_llmmodel(name="model-two", provider=provider)
        fixtures.create_llmmodel(name="model-three", provider=provider)
        cls.form_data = {
            "provider": provider.pk,
            "name": "view-test-model",
            "description": "created through the form",
            "enabled": True,
            # Required since Kind joined the form. It has a model default, but a ModelForm field
            # without `blank=True` is required regardless, so the generic create/edit cases have to
            # send it.
            "kind": LLMModelKindChoices.CHAT,
            # Decimal, not string: the generic edit test compares this dict against the saved
            # instance, which holds Decimals.
            "input_cost_per_million": Decimal("1.0000"),
            "output_cost_per_million": Decimal("2.0000"),
            "default_parameters": "{}",
        }
        cls.bulk_edit_data = {"description": "bulk edited"}


class LLMUsageRecordViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """List and detail only: the usage pages exist to be read, not written."""

    model = LLMUsageRecord

    @classmethod
    def setUpTestData(cls):
        """Create three records the sole-writer way."""
        model = fixtures.create_llmmodel()
        for _ in range(3):
            fixtures.create_llmusagerecord(model=model)

    def test_there_is_no_add_route(self):
        """The router must not register an add view for a service-written model."""
        with self.assertRaises(NoReverseMatch):
            reverse("plugins:nautobot_event_tracker:llmusagerecord_add")


class TicketDetailShowsLLMUsageTest(TestCase):
    """The ticket page carries its LLM spend."""

    def setUp(self):
        """A ticket with one usage record linked to it."""
        super().setUp()
        fixtures.create_event_types()
        self.ticket = fixtures.create_ticket(user=self.user)
        fixtures.create_llmusagerecord(ticket=self.ticket)

    def test_the_panel_renders(self):
        """The detail page shows the LLM Usage panel with the record in it."""
        self.add_permissions(
            "nautobot_event_tracker.view_eventticket",
            "nautobot_event_tracker.view_llmusagerecord",
        )
        response = self.client.get(self.ticket.get_absolute_url())
        text = response.content.decode()
        self.assertIn("LLM Usage", text)
        self.assertIn("test-model", text)


class MCPServerViewTest(ViewTestCases.PrimaryObjectViewTestCase):
    """Standard view test cases for MCPServer."""

    model = MCPServer

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_mcpserver(name="Server One")
        fixtures.create_mcpserver(name="Server Two")
        fixtures.create_mcpserver(name="Server Three")
        integration = fixtures.create_external_integration(
            name="View MCP Endpoint", remote_url="https://view.example.test/mcp"
        )
        cls.form_data = {
            "name": "View Test Server",
            "description": "created through the form",
            "external_integration": integration.pk,
            "enabled": True,
        }
        cls.bulk_edit_data = {"description": "bulk edited"}


class MCPToolViewTest(ViewTestCases.PrimaryObjectViewTestCase):
    """Standard view test cases for MCPTool."""

    model = MCPTool

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        server = fixtures.create_mcpserver()
        fixtures.create_mcptool(server=server, name="tool_one")
        fixtures.create_mcptool(server=server, name="tool_two")
        fixtures.create_mcptool(server=server, name="tool_three")
        cls.form_data = {
            "server": server.pk,
            "name": "view_test_tool",
            "description": "created through the form",
            "mutating": True,
            "enabled": False,
            "input_schema": "{}",
        }
        cls.bulk_edit_data = {"enabled": True}


class MCPServerDiscoverViewTest(TestCase):
    """The Discover Tools action: what it writes, what it refuses, and who may press it."""

    user_permissions = ["nautobot_event_tracker.view_mcpserver"]

    #: What the action actually needs: the server row it stamps, and the tool rows it writes.
    DISCOVER_PERMISSIONS = ("nautobot_event_tracker.change_mcpserver", "nautobot_event_tracker.add_mcptool")

    def setUp(self):
        """A registered server to discover against."""
        super().setUp()
        self.server = fixtures.create_mcpserver()
        self.url = reverse("plugins:nautobot_event_tracker:mcpserver_discover", kwargs={"pk": self.server.pk})

    def test_the_button_on_the_page_posts(self):
        """The regression that 1384 green tests missed: a plain Button renders a link.

        A link issues a GET, this view accepts POST only, and an operator holding both permissions
        got a 405 from the only documented way to run discovery. Asserting on the rendered page is
        the only thing that catches it - posting to the URL directly, which every other test here
        does, works perfectly well against a button nobody can use.
        """
        self.add_permissions(*self.DISCOVER_PERMISSIONS)
        response = self.client.get(self.server.get_absolute_url())

        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(f'<form method="post" action="{self.url}"', content)

    def test_the_button_is_hidden_for_a_disabled_server(self):
        """Offering an action that would be refused is how an operator learns to ignore buttons."""
        self.add_permissions(*self.DISCOVER_PERMISSIONS)
        self.server.enabled = False
        self.server.validated_save()

        response = self.client.get(self.server.get_absolute_url())

        self.assertNotIn(self.url, response.content.decode())

    def test_it_needs_more_than_permission_to_look(self):
        """Discovery writes rows; viewing the server is not enough to make it."""
        self.assertHttpStatus(self.client.post(self.url), 403)

    def test_editing_a_server_does_not_carry_the_right_to_add_tools(self):
        """The two are separate decisions: one edits a record, one widens what may be called."""
        self.add_permissions("nautobot_event_tracker.change_mcpserver")
        self.assertHttpStatus(self.client.post(self.url), 403)

    def test_it_writes_the_tools_it_finds(self):
        """The happy path, with the client seam standing in for a server."""
        self.add_permissions(*self.DISCOVER_PERMISSIONS)
        client = fixtures.FakeMCPClient([fixtures.tool_definition(name="get_device")])
        # The real function, captured before the patch: inside the side effect,
        # `mcp_service.discover` is the mock, and calling it would recurse.
        real_discover = mcp_service.discover

        with mock.patch(
            "nautobot_event_tracker.services.mcp.discover",
            side_effect=lambda server: real_discover(server, client=client),
        ):
            response = self.client.post(self.url)

        self.assertHttpStatus(response, 302)
        self.assertTrue(MCPTool.objects.filter(name="get_device", enabled=False).exists())

    def test_a_failure_is_a_message_rather_than_a_traceback(self):
        """An unreachable server is an ordinary state of the world, not a server error."""
        self.add_permissions(*self.DISCOVER_PERMISSIONS)

        with mock.patch(
            "nautobot_event_tracker.services.mcp.discover",
            side_effect=MCPCallError("connection refused"),
        ):
            response = self.client.post(self.url)

        self.assertHttpStatus(response, 302)
        self.assertFalse(MCPTool.objects.exists())

    def test_a_missing_extra_is_a_message_too(self):
        """`ImproperlyConfigured` is outside the MCPError family and would otherwise be a 500."""
        self.add_permissions(*self.DISCOVER_PERMISSIONS)

        with mock.patch(
            "nautobot_event_tracker.services.mcp.discover",
            side_effect=DjangoImproperlyConfigured("The MCP client is not installed."),
        ):
            response = self.client.post(self.url)

        self.assertHttpStatus(response, 302)


class AgentRunViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """List and detail only: a run is started by the Job and edited by nobody."""

    model = AgentRun

    @classmethod
    def setUpTestData(cls):
        """Three runs on one ticket."""
        ticket = fixtures.create_ticket()
        for _ in range(3):
            fixtures.create_agentrun(ticket=ticket)

    def test_there_is_no_add_route(self):
        """The router must not register an add view for a service-written model."""
        with self.assertRaises(NoReverseMatch):
            reverse("plugins:nautobot_event_tracker:agentrun_add")

    def test_the_detail_page_shows_the_transcript(self):
        """A person asking "why did it want to do that" reads this page (A7)."""
        self.add_permissions("nautobot_event_tracker.view_agentrun")
        run = fixtures.create_agentrun(transcript=[{"role": "assistant", "content": "the answer"}])

        response = self.client.get(run.get_absolute_url())

        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        # The framework upper-cases a panel label when it renders the header.
        self.assertIn("TRANSCRIPT", content)
        self.assertIn("the answer", content)


class AgentToolCallViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """List and detail only, plus the two decision controls."""

    model = AgentToolCall

    @classmethod
    def setUpTestData(cls):
        """Three calls on one run."""
        run = fixtures.create_agentrun()
        server = fixtures.create_mcpserver()
        for name in ("one", "two", "three"):
            fixtures.create_agenttoolcall(run=run, tool=fixtures.create_mcptool(server=server, name=name))

    def test_there_is_no_add_route(self):
        """A call is asked for by a model, never by a form."""
        with self.assertRaises(NoReverseMatch):
            reverse("plugins:nautobot_event_tracker:agenttoolcall_add")


class AgentDecisionViewTest(TestCase):
    """The approve and deny routes: who may press them, what they write, and what they refuse."""

    user_permissions = ["nautobot_event_tracker.view_agenttoolcall", "nautobot_event_tracker.view_eventticket"]

    def setUp(self):
        """A ticket with a run waiting on one proposal."""
        super().setUp()
        fixtures.create_event_types()
        self.ticket = fixtures.create_ticket(user=self.user)
        self.run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.WAITING_APPROVAL)
        self.tool = fixtures.create_mcptool(name="push_config", enabled=True, mutating=True)
        self.call = fixtures.create_agenttoolcall(run=self.run, tool=self.tool, arguments={"device": "leaf-01"})
        self.approve_url = reverse("plugins:nautobot_event_tracker:agenttoolcall_approve", kwargs={"pk": self.call.pk})
        self.deny_url = reverse("plugins:nautobot_event_tracker:agenttoolcall_deny", kwargs={"pk": self.call.pk})

    def test_deciding_needs_its_own_permission(self):
        """`change_agenttoolcall` does not imply it, and neither does `view` (7.3)."""
        self.assertHttpStatus(self.client.post(self.approve_url), 403)

        self.add_permissions("nautobot_event_tracker.change_agenttoolcall")

        self.assertHttpStatus(self.client.post(self.approve_url), 403)

    def test_a_get_does_not_decide(self):
        """POST only: approving a call against the network is not a thing a link may do."""
        self.add_permissions("nautobot_event_tracker.approve_agenttoolcall")

        self.assertHttpStatus(self.client.get(self.approve_url), 405)

    def test_denying_records_the_decision_and_ends_the_run(self):
        """13.8 - the chain ends, and the trail says who ended it."""
        self.add_permissions("nautobot_event_tracker.approve_agenttoolcall")

        response = self.client.post(self.deny_url)

        self.assertHttpStatus(response, 302)
        self.call.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.DENIED)
        self.assertEqual(self.call.decided_by, self.user)
        self.assertEqual(self.run.status, AgentRunStatusChoices.DENIED)

    def test_approving_records_the_decision(self):
        """The resumption may or may not start; the approval is recorded either way."""
        self.add_permissions("nautobot_event_tracker.approve_agenttoolcall")

        response = self.client.post(self.approve_url)

        self.assertHttpStatus(response, 302)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.APPROVED)
        self.assertEqual(self.call.decided_by, self.user)

    def test_deciding_twice_is_a_message_rather_than_a_second_decision(self):
        """A call is decided once, and the second press says so."""
        self.add_permissions("nautobot_event_tracker.approve_agenttoolcall")
        self.client.post(self.approve_url)

        response = self.client.post(self.deny_url, follow=True)

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.APPROVED)
        self.assertIn("already", response.content.decode())


class TicketAgentPanelTest(TestCase):
    """The ticket page: the runs, the proposal, and the two buttons that decide it."""

    user_permissions = ["nautobot_event_tracker.view_eventticket", "nautobot_event_tracker.view_agentrun"]

    def setUp(self):
        """A ticket with a run waiting on a proposal."""
        super().setUp()
        fixtures.create_event_types()
        self.ticket = fixtures.create_ticket(user=self.user)
        self.run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.WAITING_APPROVAL)
        self.tool = fixtures.create_mcptool(name="push_config", enabled=True, mutating=True)
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, arguments={"device": "leaf-01", "config": "shutdown"}
        )

    def page(self):
        """The ticket detail page, rendered."""
        return self.client.get(self.ticket.get_absolute_url()).content.decode()

    def test_the_runs_panel_renders(self):
        """A person reading a ticket can see that an agent has been at it."""
        self.assertIn("Agent Runs", self.page())

    def test_the_proposal_is_spelled_out(self):
        """Section 11 - the gate is only as good as what the approver is shown."""
        self.add_permissions("nautobot_event_tracker.approve_agenttoolcall")

        content = self.page()

        self.assertIn("WAITING FOR YOUR DECISION", content)
        self.assertIn("push_config", content)
        self.assertIn("leaf-01", content)
        self.assertIn("shutdown", content)

    def test_the_decision_buttons_post(self):
        """A control that issues a GET is a control a link preview can press."""
        self.add_permissions("nautobot_event_tracker.approve_agenttoolcall")
        approve_url = reverse("plugins:nautobot_event_tracker:agenttoolcall_approve", kwargs={"pk": self.call.pk})

        self.assertIn(f'<form method="post" action="{approve_url}"', self.page())

    def test_the_buttons_are_hidden_without_the_permission(self):
        """Offering an action that would be refused teaches people to ignore buttons."""
        approve_url = reverse("plugins:nautobot_event_tracker:agenttoolcall_approve", kwargs={"pk": self.call.pk})

        self.assertNotIn(approve_url, self.page())

    def test_the_buttons_are_hidden_when_nothing_is_waiting(self):
        """The panel and the buttons both exist only while there is a decision to make."""
        self.add_permissions("nautobot_event_tracker.approve_agenttoolcall")
        agent_service.deny_tool_call(tool_call=self.call, user=self.user)

        content = self.page()

        self.assertNotIn("WAITING FOR YOUR DECISION", content)
        self.assertNotIn("Approve Tool Call", content)

    def test_the_investigate_button_appears_when_agents_are_on(self):
        """It leads to the Job's own run form, which is where the permissions are written."""
        self.add_permissions("extras.run_job")

        with fixtures.agent_settings():
            content = self.page()

        self.assertIn("Investigate with Agent", content)
        self.assertIn("nautobot_event_tracker.jobs.EventTicketAgentJob", content)

    def test_the_investigate_button_is_hidden_when_agents_are_off(self):
        """A button that leads to a refusal is worse than no button.

        Under `app_settings()` rather than the ambient configuration, so this asserts what a stock
        install does rather than what the machine running the tests is configured for.
        """
        self.add_permissions("extras.run_job")

        with fixtures.app_settings():
            content = self.page()

        self.assertNotIn("Investigate with Agent", content)

    def test_the_investigate_button_is_hidden_on_a_finished_ticket(self):
        """S3 - an agent may not work a resolved ticket, so the page does not offer it."""
        self.add_permissions("extras.run_job")
        ticket = fixtures.create_ticket_in_status(TicketStatusChoices.RESOLVED, user=self.user)

        with fixtures.agent_settings():
            content = self.client.get(ticket.get_absolute_url()).content.decode()

        self.assertNotIn("Investigate with Agent", content)


class TicketEmbeddingViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """List and detail only: the corpus is written by services/rag.py.

    `view_eventticket` is granted alongside the model's own permission because the viewset narrows
    the corpus to embeddings of tickets the user may read (rule R6) - a `document` is a verbatim
    copy of its ticket, so reading one has to be gated on the ticket. Without it the generic
    mixins see an empty queryset and every case here fails, which is the restriction working.
    """

    model = TicketEmbedding
    user_permissions = ["nautobot_event_tracker.view_eventticket"]

    def test_get_object_anonymous(self):
        """Skipped: this model deliberately does not honour its own view exemption.

        `EXEMPT_VIEW_PERMISSIONS` on `ticketembedding` would make the corpus anonymously readable,
        and a corpus document is a verbatim copy of its ticket - so the exemption would publish
        ticket text to unauthenticated users through a model whose name gives no hint of that.
        Visibility follows the *ticket*, which has its own exemption setting if an operator really
        wants this public.
        """
        self.skipTest("Corpus visibility follows the ticket's permissions, not this model's exemption.")

    def test_list_objects_anonymous_with_exempt_permission_for_one_view_only(self):
        """Skipped for the reason above."""
        self.skipTest("Corpus visibility follows the ticket's permissions, not this model's exemption.")

    @classmethod
    def setUpTestData(cls):
        """Three embeddings on three closed tickets."""
        embedding_model = fixtures.create_embedding_model()
        for index in range(3):
            ticket = fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, title=f"Corpus {index}")
            fixtures.create_ticketembedding(ticket=ticket, model=embedding_model)

    def test_there_is_no_add_route(self):
        """Nothing outside the service writes a corpus row."""
        with self.assertRaises(NoReverseMatch):
            reverse("plugins:nautobot_event_tracker:ticketembedding_add")


class SimilarTicketsPanelTest(TestCase):
    """The panel: the only consumer of retrieval, and the one a person reads."""

    user_permissions = ["nautobot_event_tracker.view_eventticket"]

    def setUp(self):
        """A corpus with one close neighbour, and an open ticket to view."""
        super().setUp()
        fixtures.create_event_types()
        self.embedding_model = fixtures.create_embedding_model()
        self.neighbour = fixtures.create_ticket_in_status(
            TicketStatusChoices.CLOSED, user=self.user, title="leaf-01 optic replaced"
        )
        fixtures.create_ticketembedding(ticket=self.neighbour, model=self.embedding_model, vector=[1.0, 0.0, 0.0])
        self.ticket = fixtures.create_ticket(user=self.user, title="leaf-01 down again")

    def page(self, **overrides):
        """The ticket page, rendered with retrieval on and a vector pointing at the neighbour.

        The panel takes no seam - it is the production path - so the model call is patched at the
        service. The real function is captured before the patch: inside the side effect
        `llm_service.embed` is the mock, and calling it would recurse.
        """
        from nautobot_event_tracker.services import llm as llm_service  # pylint: disable=C0415

        real_embed = llm_service.embed

        def _embed(**kwargs):
            return real_embed(**kwargs, client=fixtures.FakeEmbeddingClient([1.0, 0.0, 0.0]))

        with fixtures.rag_settings(**overrides):
            with mock.patch("nautobot_event_tracker.services.rag.llm_service.embed", side_effect=_embed):
                return self.client.get(self.ticket.get_absolute_url()).content.decode()

    def test_the_panel_shows_a_neighbour_and_its_resolution(self):
        """ "We have seen this before", which is the entire point of the phase."""
        content = self.page()

        self.assertIn("SIMILAR TICKETS", content)
        self.assertIn("leaf-01 optic replaced", content)

    def test_the_panel_is_absent_when_retrieval_is_off(self):
        """A stock install renders exactly as it did before this phase."""
        with fixtures.app_settings():
            content = self.client.get(self.ticket.get_absolute_url()).content.decode()

        self.assertNotIn("SIMILAR TICKETS", content)

    def test_the_panel_is_absent_on_a_closed_ticket(self):
        """12.2 - a closed ticket's neighbours are of historical interest at best."""
        closed = fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, user=self.user, title="already done")

        with fixtures.rag_settings():
            content = self.client.get(closed.get_absolute_url()).content.decode()

        self.assertNotIn("SIMILAR TICKETS", content)
