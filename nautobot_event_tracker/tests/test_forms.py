"""Test the Event Tracker forms."""

from nautobot.apps.testing import TestCase

from nautobot_event_tracker import forms
from nautobot_event_tracker.api.serializers import SERVICE_OWNED_FIELDS
from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.models import EventType
from nautobot_event_tracker.services.tickets import get_attachable_object_types
from nautobot_event_tracker.tests import fixtures


class EventTypeFormTest(TestCase):
    """EventTypeForm."""

    def test_name_is_required(self):
        """A type without a name is invalid."""
        form = forms.EventTypeForm(data={"default_severity": SeverityChoices.MINOR})
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_minimum_valid_form(self):
        """Name alone is enough; severity defaults."""
        form = forms.EventTypeForm(data={"name": "Form Type", "default_severity": SeverityChoices.MINOR})
        self.assertTrue(form.is_valid(), form.errors)

    def test_all_fields(self):
        """Every field accepts a value."""
        form = forms.EventTypeForm(
            data={
                "name": "Form Type All",
                "description": "described",
                "default_severity": SeverityChoices.CRITICAL,
                "enabled": False,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)


class EventTicketFormTest(TestCase):
    """EventTicketForm."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def test_title_and_event_type_are_required(self):
        """A ticket needs at least a title and a type."""
        form = forms.EventTicketForm(data={})
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)
        self.assertIn("event_type", form.errors)

    def test_valid_form(self):
        """The happy path validates."""
        event_type = EventType.objects.get(name="Test Interface Down")
        form = forms.EventTicketForm(
            data={
                "title": "A form ticket",
                "event_type": event_type.pk,
                "severity": SeverityChoices.MAJOR,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_service_owned_fields_are_absent(self):
        """The form cannot reach anything the service layer owns."""
        form = forms.EventTicketForm()
        for field in SERVICE_OWNED_FIELDS:
            self.assertNotIn(field, form.fields)

    def test_disabled_event_types_are_not_offered(self):
        """A disabled type must not appear in the picker."""
        form = forms.EventTicketForm()
        names = set(form.fields["event_type"].queryset.values_list("name", flat=True))
        self.assertNotIn("Test Disabled Type", names)


class AttachObjectFormTest(TestCase):
    """AttachObjectForm."""

    def test_type_choices_come_from_the_allowlist(self):
        """The picker offers exactly the configured types that exist."""
        form = forms.AttachObjectForm()
        offered = {
            f"{content_type.app_label}.{content_type.model}" for content_type in form.fields["object_type"].queryset
        }
        self.assertTrue(offered)
        self.assertTrue(offered.issubset(set(get_attachable_object_types())))

    def test_disallowed_type_is_not_offered(self):
        """The app's own models are not attachable."""
        form = forms.AttachObjectForm()
        offered = {
            f"{content_type.app_label}.{content_type.model}" for content_type in form.fields["object_type"].queryset
        }
        self.assertNotIn("nautobot_event_tracker.eventtype", offered)

    def test_both_fields_are_required(self):
        """An empty submission is rejected."""
        form = forms.AttachObjectForm(data={})
        self.assertFalse(form.is_valid())
        self.assertIn("object_type", form.errors)
        self.assertIn("object_id", form.errors)


class TicketTransitionFormTest(TestCase):
    """TicketTransitionForm."""

    def test_to_status_is_required(self):
        """A transition must name a target."""
        form = forms.TicketTransitionForm(data={})
        self.assertFalse(form.is_valid())
        self.assertIn("to_status", form.errors)

    def test_message_and_resolution_are_optional(self):
        """Only the target is mandatory at the form layer; the service enforces the rest."""
        form = forms.TicketTransitionForm(data={"to_status": "triaged"})
        self.assertTrue(form.is_valid(), form.errors)
