"""Forms for nautobot_event_tracker."""

from django import forms
from django.contrib.contenttypes.models import ContentType
from nautobot.apps.constants import CHARFIELD_MAX_LENGTH
from nautobot.apps.forms import (
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    NautobotBulkEditForm,
    NautobotFilterForm,
    NautobotModelForm,
    StaticSelect2,
    StaticSelect2Multiple,
    TagsBulkEditFormMixin,
)
from nautobot.extras.models import ExternalIntegration

from nautobot_event_tracker.choices import (
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
    LLMProviderTypeChoices,
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
    LLMModel,
    LLMProvider,
    LLMUsageRecord,
    MCPServer,
    MCPTool,
)
from nautobot_event_tracker.services import tickets as ticket_service

#: The model registry entry's editable fields, in the order they read best. Shared with the detail
#: panel in views.py so the form and the page cannot drift apart. Homed here rather than in
#: tables.py: no table uses it, and forms must not depend on presentation modules.
LLM_MODEL_FIELDS = (
    "provider",
    "name",
    "description",
    "enabled",
    "input_cost_per_million",
    "output_cost_per_million",
    "max_output_tokens",
    "default_parameters",
)

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


class LLMProviderForm(NautobotModelForm):  # pylint: disable=too-many-ancestors
    """LLMProvider creation/edit form."""

    external_integration = DynamicModelChoiceField(queryset=ExternalIntegration.objects.all())

    class Meta:
        """Meta attributes."""

        model = LLMProvider
        fields = [  # pylint: disable=nb-use-fields-all
            "name",
            "description",
            "provider_type",
            "external_integration",
            "enabled",
            "tags",
        ]


class LLMProviderBulkEditForm(TagsBulkEditFormMixin, NautobotBulkEditForm):  # pylint: disable=too-many-ancestors
    """LLMProvider bulk edit form.

    `TagsBulkEditFormMixin` for the same reason `EventTicketBulkEditForm` has it: both models are
    PrimaryModels, both filtersets offer a `tags` filter, and a tag nobody can apply in bulk is a
    filter for something the UI cannot produce.
    """

    pk = forms.ModelMultipleChoiceField(queryset=LLMProvider.objects.all(), widget=forms.MultipleHiddenInput)
    description = forms.CharField(required=False, max_length=CHARFIELD_MAX_LENGTH)
    enabled = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))

    class Meta:
        """Meta attributes."""

        nullable_fields = ["description"]


class LLMProviderFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for LLMProvider."""

    model = LLMProvider
    field_order = ["q", "name", "provider_type", "enabled"]

    q = forms.CharField(required=False, label="Search", help_text="Search within name and description.")
    name = forms.CharField(required=False, label="Name")
    provider_type = forms.MultipleChoiceField(
        choices=LLMProviderTypeChoices, required=False, widget=StaticSelect2Multiple
    )
    enabled = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))


class LLMModelForm(NautobotModelForm):  # pylint: disable=too-many-ancestors
    """LLMModel creation/edit form."""

    provider = DynamicModelChoiceField(queryset=LLMProvider.objects.all())

    class Meta:
        """Meta attributes."""

        model = LLMModel
        # One definition of the field list, shared with the detail panel, plus tags - which the
        # form offers and the panel does not, because Nautobot renders tags on a detail page
        # itself.
        fields = [*LLM_MODEL_FIELDS, "tags"]  # pylint: disable=nb-use-fields-all


class LLMModelBulkEditForm(TagsBulkEditFormMixin, NautobotBulkEditForm):  # pylint: disable=too-many-ancestors
    """LLMModel bulk edit form."""

    pk = forms.ModelMultipleChoiceField(queryset=LLMModel.objects.all(), widget=forms.MultipleHiddenInput)
    description = forms.CharField(required=False, max_length=CHARFIELD_MAX_LENGTH)
    enabled = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))

    class Meta:
        """Meta attributes."""

        nullable_fields = ["description"]


class LLMModelFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for LLMModel."""

    model = LLMModel
    field_order = ["q", "provider", "name", "enabled"]

    q = forms.CharField(required=False, label="Search", help_text="Search within name, description and provider.")
    provider = DynamicModelChoiceField(queryset=LLMProvider.objects.all(), required=False, to_field_name="name")
    name = forms.CharField(required=False, label="Name")
    enabled = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))


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
            queryset=LLMModel.objects.all(),
            required=False,
            label="Model",
        )
        # `field_order` is applied by `super().__init__()`, which ran before the field existed, so
        # ordering it takes a second pass. Without this the picker renders last, below the fields
        # a reader is meant to reach after it.
        self.order_fields(self.field_order)


class MCPServerForm(NautobotModelForm):  # pylint: disable=too-many-ancestors
    """MCPServer creation/edit form."""

    external_integration = DynamicModelChoiceField(queryset=ExternalIntegration.objects.all())

    class Meta:
        """Meta attributes."""

        model = MCPServer
        fields = [  # pylint: disable=nb-use-fields-all
            "name",
            "description",
            "external_integration",
            "enabled",
            "tags",
        ]


class MCPServerBulkEditForm(TagsBulkEditFormMixin, NautobotBulkEditForm):  # pylint: disable=too-many-ancestors
    """MCPServer bulk edit form."""

    pk = forms.ModelMultipleChoiceField(queryset=MCPServer.objects.all(), widget=forms.MultipleHiddenInput)
    description = forms.CharField(required=False, max_length=CHARFIELD_MAX_LENGTH)
    enabled = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))

    class Meta:
        """Meta attributes."""

        nullable_fields = ["description"]


class MCPServerFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for MCPServer."""

    model = MCPServer
    field_order = ["q", "name", "enabled"]

    q = forms.CharField(required=False, label="Search", help_text="Search within name and description.")
    name = forms.CharField(required=False, label="Name")
    enabled = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))


class MCPToolForm(NautobotModelForm):  # pylint: disable=too-many-ancestors
    """MCPTool creation/edit form.

    `name`, `description` and `input_schema` are what a server advertised, and discovery rewrites
    them. What an operator owns is the two decisions: whether this tool may be called at all, and
    whether calling it changes something.
    """

    server = DynamicModelChoiceField(queryset=MCPServer.objects.all())

    class Meta:
        """Meta attributes."""

        model = MCPTool
        fields = [  # pylint: disable=nb-use-fields-all
            "server",
            "name",
            "description",
            "mutating",
            "enabled",
            "input_schema",
            "tags",
        ]
        # `advertised_read_only` is deliberately absent: it records what the server claimed, and a
        # field an operator can type into is no longer a record of that.


class MCPToolBulkEditForm(TagsBulkEditFormMixin, NautobotBulkEditForm):  # pylint: disable=too-many-ancestors
    """MCPTool bulk edit form.

    The one that carries the weight of ADR 0007's admitted friction: reviewing a forty-tool server
    one row at a time is how an operator ends up enabling all of them to be done with it.
    """

    pk = forms.ModelMultipleChoiceField(queryset=MCPTool.objects.all(), widget=forms.MultipleHiddenInput)
    enabled = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))
    mutating = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))

    class Meta:
        """Meta attributes."""

        nullable_fields = []


class MCPToolFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form for MCPTool."""

    model = MCPTool
    field_order = ["q", "server", "name", "enabled", "mutating", "advertised_read_only"]

    q = forms.CharField(required=False, label="Search", help_text="Search within name, description and server.")
    # Multiple, because the filterset takes multiple and reviewing two servers at once is an
    # ordinary thing to want on the page whose whole purpose is review.
    server = DynamicModelMultipleChoiceField(queryset=MCPServer.objects.all(), to_field_name="name", required=False)
    name = forms.CharField(required=False, label="Name")
    enabled = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))
    mutating = forms.NullBooleanField(required=False, widget=StaticSelect2(choices=YES_NO_CHOICES))
    advertised_read_only = forms.NullBooleanField(
        required=False,
        label="Server claims read-only",
        widget=StaticSelect2(choices=YES_NO_CHOICES),
    )


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
    queue of decisions waiting on a person.
    """

    model = AgentToolCall
    field_order = ["q", "status", "tool", "ticket"]

    q = forms.CharField(required=False, label="Search", help_text="Search within tool name, status and error.")
    status = forms.MultipleChoiceField(choices=AgentToolCallStatusChoices, required=False, widget=StaticSelect2Multiple)
    tool = DynamicModelChoiceField(queryset=MCPTool.objects.all(), required=False, label="Tool")
    ticket = DynamicModelChoiceField(queryset=EventTicket.objects.all(), required=False, label="Ticket")
