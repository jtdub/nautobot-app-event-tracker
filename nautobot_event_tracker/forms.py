"""Forms for nautobot_event_tracker."""

from django import forms
from django.contrib.contenttypes.models import ContentType
from nautobot.apps.constants import CHARFIELD_MAX_LENGTH
from nautobot.apps.forms import (
    DynamicModelChoiceField,
    NautobotBulkEditForm,
    NautobotFilterForm,
    NautobotModelForm,
    StaticSelect2,
    StaticSelect2Multiple,
    TagsBulkEditFormMixin,
)

from nautobot_event_tracker.choices import SeverityChoices, TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.models import EventTicket, EventType
from nautobot_event_tracker.services.tickets import get_attachable_object_types


class EventTypeForm(NautobotModelForm):  # pylint: disable=too-many-ancestors
    """EventType creation/edit form."""

    class Meta:
        """Meta attributes."""

        model = EventType
        fields = ["name", "description", "default_severity", "enabled"]


class EventTypeBulkEditForm(NautobotBulkEditForm):  # pylint: disable=too-many-ancestors
    """EventType bulk edit form."""

    pk = forms.ModelMultipleChoiceField(queryset=EventType.objects.all(), widget=forms.MultipleHiddenInput)
    description = forms.CharField(required=False, max_length=CHARFIELD_MAX_LENGTH)
    default_severity = forms.ChoiceField(choices=SeverityChoices, required=False, widget=StaticSelect2)
    enabled = forms.NullBooleanField(
        required=False,
        widget=StaticSelect2(
            choices=[
                ("", "---------"),
                ("True", "Yes"),
                ("False", "No"),
            ]
        ),
    )

    class Meta:
        """Meta attributes."""

        nullable_fields = ["description"]


class EventTypeFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for EventType."""

    model = EventType
    field_order = ["q", "name", "default_severity", "enabled"]

    q = forms.CharField(required=False, label="Search", help_text="Search within name and description.")
    name = forms.CharField(required=False, label="Name")
    default_severity = forms.MultipleChoiceField(choices=SeverityChoices, required=False, widget=StaticSelect2Multiple)
    enabled = forms.NullBooleanField(
        required=False,
        widget=StaticSelect2(
            choices=[
                ("", "---------"),
                ("True", "Yes"),
                ("False", "No"),
            ]
        ),
    )


class EventTicketForm(NautobotModelForm):  # pylint: disable=too-many-ancestors
    """EventTicket creation/edit form.

    Deliberately omits `status`, `resolved_at`, `closed_at`, `resolution` and `event_count`: those
    are service-owned, and the only path to a status change is a transition button.
    """

    event_type = DynamicModelChoiceField(queryset=EventType.objects.filter(enabled=True))

    class Meta:
        """Meta attributes."""

        model = EventTicket
        fields = [
            "title",
            "event_type",
            "severity",
            "description",
            "assigned_to",
            "dedup_key",
            "tags",
        ]


class EventTicketBulkEditForm(TagsBulkEditFormMixin, NautobotBulkEditForm):  # pylint: disable=too-many-ancestors
    """EventTicket bulk edit form.

    Offers no `status` field for the same reason the create/edit form does not.
    """

    pk = forms.ModelMultipleChoiceField(queryset=EventTicket.objects.all(), widget=forms.MultipleHiddenInput)
    severity = forms.ChoiceField(choices=SeverityChoices, required=False, widget=StaticSelect2)
    event_type = DynamicModelChoiceField(queryset=EventType.objects.all(), required=False)
    description = forms.CharField(required=False)

    class Meta:
        """Meta attributes."""

        nullable_fields = ["description", "assigned_to"]


class EventTicketFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for EventTicket."""

    model = EventTicket
    field_order = ["q", "status", "severity", "source", "event_type", "assigned_to", "is_open"]

    q = forms.CharField(
        required=False,
        label="Search",
        help_text="Search within title, description, resolution and event type name.",
    )
    status = forms.MultipleChoiceField(choices=TicketStatusChoices, required=False, widget=StaticSelect2Multiple)
    severity = forms.MultipleChoiceField(choices=SeverityChoices, required=False, widget=StaticSelect2Multiple)
    source = forms.MultipleChoiceField(choices=TicketSourceChoices, required=False, widget=StaticSelect2Multiple)
    event_type = DynamicModelChoiceField(queryset=EventType.objects.all(), required=False, to_field_name="name")
    is_open = forms.NullBooleanField(
        required=False,
        widget=StaticSelect2(
            choices=[
                ("", "---------"),
                ("True", "Yes"),
                ("False", "No"),
            ]
        ),
    )


class TicketCommentForm(forms.Form):
    """Add a comment to a ticket. Routed through the service layer."""

    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), label="Comment")


class TicketTransitionForm(forms.Form):
    """Confirm a status transition. Routed through the service layer."""

    to_status = forms.CharField(widget=forms.HiddenInput)
    message = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Note",
        help_text="Optional note recorded with the status change.",
    )
    resolution = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Resolution",
        help_text="Required when resolving a ticket.",
    )


class AttachObjectForm(forms.Form):
    """Attach a Nautobot object to a ticket.

    The type choices come from the configured allowlist, so the picker cannot offer something
    `services.tickets.attach_object()` would reject.
    """

    object_type = forms.ModelChoiceField(
        queryset=ContentType.objects.none(),
        label="Object type",
        widget=StaticSelect2,
    )
    object_id = forms.UUIDField(
        label="Object",
        help_text="Select the object to attach.",
    )

    def __init__(self, *args, **kwargs):
        """Limit the type choices to the configured allowlist."""
        super().__init__(*args, **kwargs)
        self.fields["object_type"].queryset = attachable_content_types()


def attachable_content_types():
    """Return a ContentType queryset covering the configured allowlist."""
    pks = []
    for label in get_attachable_object_types():
        app_label, _, model = label.partition(".")
        content_type = ContentType.objects.filter(app_label=app_label, model=model).first()
        if content_type is not None:
            pks.append(content_type.pk)
    return ContentType.objects.filter(pk__in=pks).order_by("app_label", "model")
