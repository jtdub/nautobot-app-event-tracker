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
from nautobot_ai_models.models import AIModel, MCPTool

from nautobot_event_tracker.choices import (
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
    LLMPurposeChoices,
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
)
from nautobot_event_tracker.models import (
    AgentRun,
    AgentToolCall,
    EventTicket,
    EventType,
    IngestionStats,
    LLMUsageRecord,
    TicketEmbedding,
)
from nautobot_event_tracker.services import analytics
from nautobot_event_tracker.services import tickets as ticket_service

#: Nautobot has BOOLEAN_WITH_BLANK_CHOICES, but only under nautobot.core.forms.constants, which is
#: outside the public nautobot.apps surface this app otherwise stays within.
YES_NO_CHOICES = (("", "---------"), ("True", "Yes"), ("False", "No"))


class EventTypeForm(NautobotModelForm):  # pylint: disable=too-many-ancestors
    """EventType creation/edit form."""

    class Meta:
        """Meta attributes."""

        model = EventType
        fields = ["name", "description", "default_severity", "enabled"]  # pylint: disable=nb-use-fields-all


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
        # Deliberately not "__all__": omitting the service-owned fields is the point of this form.
        fields = [  # pylint: disable=nb-use-fields-all
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

    Offers no `status` field for the same reason the create/edit form does not, and no `severity`
    or `assigned_to` either: Nautobot applies a bulk edit by assigning attributes and saving each
    object itself, with no hook the service layer could run in, so a bulk change to either would be
    a ticket mutation with nothing in the trail to record it. Both remain changeable one ticket at
    a time in the UI, and in bulk through the REST API's bulk PATCH, which does route through the
    service.
    """

    pk = forms.ModelMultipleChoiceField(queryset=EventTicket.objects.all(), widget=forms.MultipleHiddenInput)
    event_type = DynamicModelChoiceField(queryset=EventType.objects.all(), required=False)
    description = forms.CharField(required=False)

    class Meta:
        """Meta attributes."""

        nullable_fields = ["description"]


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


class DashboardWindowForm(forms.Form):
    """The analytics dashboard's time window, submitted by GET.

    The choices come from `services.analytics.window_choices()` rather than being written out
    here, so what the page offers and what `max_window_days` allows cannot disagree (rule D4).
    """

    days = forms.TypedChoiceField(
        coerce=int,
        required=False,
        label="Window",
        widget=StaticSelect2(),
    )

    def __init__(self, *args, **kwargs):
        """Build the choices from the configured bound, and remember the default.

        `get_settings()` is strict and this is one of the two places it runs, the view being the
        other. A malformed `dashboard` block therefore fails on the dashboard page, where somebody
        can read the message - not at import time, where it would stop Nautobot serving anything.
        """
        super().__init__(*args, **kwargs)
        settings = analytics.get_settings()
        self.default_days = settings.default_window_days
        self.fields["days"].choices = [
            (days, "Last 24 hours" if days == 1 else f"Last {days} days") for days in analytics.window_choices(settings)
        ]
        self.fields["days"].initial = self.default_days

        if self.is_bound and not self.is_valid():
            # Unbind, so the control shows the window the page actually drew. Left bound, the
            # `<select>` carries a value matching no option and the browser falls back to the first
            # one - a bookmarked `?days=90` opened after `max_window_days` was tightened would
            # render a seven-day page labelled "Last 24 hours". The realistic way to send an
            # unreadable window is a stale link, not a hand-edited URL.
            self.is_bound = False
            self.data = {}
            self._errors = None

    def window_days(self):
        """The window the page asked for, or the configured default when it did not ask.

        A window this form cannot read is the default rather than an error page: the only way to
        send one is to edit the query string by hand, and the answer to that is a chart.
        """
        if self.is_bound and self.is_valid() and self.cleaned_data.get("days"):
            return self.cleaned_data["days"]
        return self.default_days


class AttachObjectTypeForm(forms.Form):
    """Step one of attaching: which kind of object?

    The type choices come from the configured allowlist, so the picker cannot offer something
    `services.tickets.attach_object()` would reject.
    """

    object_type = forms.ModelChoiceField(
        queryset=ContentType.objects.none(),
        label="Object type",
        widget=StaticSelect2,
        help_text="Choose the kind of object, then pick the object itself.",
    )

    def __init__(self, *args, **kwargs):
        """Limit the type choices to the configured allowlist."""
        super().__init__(*args, **kwargs)
        self.fields["object_type"].queryset = ticket_service.get_attachable_content_types()


class AttachObjectForm(AttachObjectTypeForm):
    """Step two of attaching: which object of the chosen type?

    Splitting the two steps is what makes the second field a real type-ahead picker: a
    `DynamicModelChoiceField` needs a concrete queryset, which only exists once the type is known.
    The type comes back as a hidden field so the submission carries both halves.
    """

    def __init__(self, content_type, *args, **kwargs):
        """Build the object picker for this content type."""
        super().__init__(*args, **kwargs)
        model = content_type.model_class()
        self.fields["object_type"].widget = forms.HiddenInput()
        self.fields["object_type"].initial = content_type.pk
        self.fields["object_id"] = DynamicModelChoiceField(
            queryset=model.objects.all(),
            label=model._meta.verbose_name.title(),
            help_text="Select the object to attach.",
        )


class DetachObjectForm(forms.Form):
    """Confirm detaching an object from a ticket.

    Both fields are hidden: the object is identified by the link the user followed, and the page is
    a confirmation, not a picker. The type is deliberately not limited to the allowlist, so that an
    object attached under an older configuration stays removable.
    """

    object_type = forms.ModelChoiceField(queryset=ContentType.objects.all(), widget=forms.HiddenInput)
    object_id = forms.UUIDField(widget=forms.HiddenInput)


class IngestionStatsFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for IngestionStats.

    Two questions the page exists to answer: what is one consumer doing, and what is happening on
    one topic.
    """

    model = IngestionStats
    field_order = ["q", "consumer_name", "topic"]

    q = forms.CharField(required=False, label="Search", help_text="Search within consumer name and topic.")
    consumer_name = forms.CharField(required=False, label="Consumer name")
    topic = forms.CharField(required=False, label="Topic")


class LLMUsageRecordFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for LLMUsageRecord.

    Filter form only: usage records are written by the service layer alone, so there is no create
    or edit form to offer.
    """

    model = LLMUsageRecord
    field_order = ["q", "model", "purpose", "success"]

    q = forms.CharField(required=False, label="Search", help_text="Search within model name, purpose and error.")
    purpose = forms.MultipleChoiceField(choices=LLMPurposeChoices, required=False, widget=StaticSelect2Multiple)
    success = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))

    def __init__(self, *args, **kwargs):
        """Add the model picker after construction.

        Declared here rather than as a class attribute because `model` already names the Django
        model on every NautobotFilterForm. The collision is class-level only, so the field goes
        into `self.fields` - which is what the page exists to filter by, and worth more than
        leaving operators to hand-edit `?model=` into the URL.
        """
        super().__init__(*args, **kwargs)
        self.fields["model"] = DynamicModelChoiceField(
            queryset=AIModel.objects.all(),
            required=False,
            label="Model",
        )
        # `field_order` is applied by `super().__init__()`, which ran before the field existed, so
        # ordering it takes a second pass. Without this the picker renders last, below the fields
        # a reader is meant to reach after it.
        self.order_fields(self.field_order)

        # `advertised_read_only` is deliberately absent: it records what the server claimed, and a
        # field an operator can type into is no longer a record of that.


class AgentRunFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for AgentRun.

    Filter form only: a run is written by `services.agent` and by nothing else, so there is no
    create or edit form to offer.
    """

    model = AgentRun
    field_order = ["q", "status", "ticket"]

    q = forms.CharField(required=False, label="Search", help_text="Search within ticket title, status and error.")
    status = forms.MultipleChoiceField(choices=AgentRunStatusChoices, required=False, widget=StaticSelect2Multiple)
    ticket = DynamicModelChoiceField(queryset=EventTicket.objects.all(), required=False, label="Ticket")


class AgentToolCallFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for AgentToolCall.

    Filter form only, for the same reason. The status filter is the useful one: `proposed` is the
    queue of decisions waiting on a person. `outcome_unknown` is the list of changes to check on a device.
    """

    model = AgentToolCall
    field_order = ["q", "status", "tool", "ticket"]

    q = forms.CharField(required=False, label="Search", help_text="Search within tool name, status and error.")
    status = forms.MultipleChoiceField(choices=AgentToolCallStatusChoices, required=False, widget=StaticSelect2Multiple)
    tool = DynamicModelChoiceField(queryset=MCPTool.objects.all(), required=False, label="Tool")
    ticket = DynamicModelChoiceField(queryset=EventTicket.objects.all(), required=False, label="Ticket")


class TicketEmbeddingFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for TicketEmbedding.

    Filter form only: the corpus is written by `services.rag` and by nothing else, so there is no
    create or edit form to offer.
    """

    model = TicketEmbedding
    field_order = ["q", "ticket"]

    q = forms.CharField(required=False, label="Search", help_text="Search within ticket title and document.")
    ticket = DynamicModelChoiceField(queryset=EventTicket.objects.all(), required=False, label="Ticket")
