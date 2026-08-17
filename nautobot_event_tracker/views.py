"""Views for nautobot_event_tracker.

Built on NautobotUIViewSet and the UI Component Framework only. The app ships no page templates.
See ADR 0008.
"""

from contextlib import contextmanager

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import format_html
from nautobot.apps.models import count_related
from nautobot.apps.templatetags import hyperlinked_object
from nautobot.apps.ui import (
    Button,
    ButtonColorChoices,
    DropdownButton,
    GroupedKeyValueTablePanel,
    KeyValueTablePanel,
    ObjectDetailContent,
    ObjectFieldsPanel,
    ObjectsTablePanel,
    ObjectTextPanel,
    SectionChoices,
)
from nautobot.apps.views import (
    GenericView,
    NautobotUIViewSet,
    ObjectDetailViewMixin,
    ObjectListViewMixin,
    ObjectPermissionRequiredMixin,
)

from nautobot_event_tracker import filters, forms, models, tables
from nautobot_event_tracker.api import serializers
from nautobot_event_tracker.choices import TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import TicketServiceError

TRANSITION_PERMISSION = "nautobot_event_tracker.transition_eventticket"
CHANGE_PERMISSION = "nautobot_event_tracker.change_eventticket"


def _render_object_form(request, ticket, form):
    """Render a form against a ticket using Nautobot's generic object-edit page."""
    return render(
        request,
        "generic/object_edit.html",
        {
            "obj": ticket,
            "obj_type": ticket._meta.verbose_name,  # pylint: disable=protected-access
            "form": form,
            "return_url": ticket.get_absolute_url(),
            "editing": True,
        },
    )


@contextmanager
def _reporting_service_errors(request):
    """Turn any ticket service error into a user-facing message.

    One mapping for the whole UI transport, so a new action cannot forget a branch - which is how
    the detach view came to be missing the ValidationError case.
    """
    try:
        yield
    except TicketServiceError as error:
        messages.error(request, str(error))
    except DjangoValidationError as error:
        messages.error(request, "; ".join(error.messages))


#: Button colour per target status, so the destructive-looking moves read as such.
TRANSITION_COLORS = {
    TicketStatusChoices.TRIAGED: ButtonColorChoices.BLUE,
    TicketStatusChoices.IN_PROGRESS: ButtonColorChoices.BLUE,
    TicketStatusChoices.SUPPRESSED: ButtonColorChoices.GREY,
    TicketStatusChoices.RESOLVED: ButtonColorChoices.GREEN,
    TicketStatusChoices.CLOSED: ButtonColorChoices.GREY,
}


class RelatedObjectsPanel(GroupedKeyValueTablePanel):
    """Attached Nautobot objects, grouped by object type, each with a detach control.

    Attachment is derived from the ticket's update trail, so the data comes from the service layer
    rather than from a relation on the ticket.
    """

    def get_data(self, context):
        """Return `{type label: {object label: rendered link}}` for the framework to render.

        The framework calls this twice per render - once from `should_render()` and again from
        `render_body_content()` - so the result is cached for the life of the ticket instance.
        The cache lives on the ticket, not on `self`: panels are module-level singletons shared
        across requests and threads, so caching on the panel would leak one request's data into
        another's.
        """
        ticket = context.get("object")
        if ticket is None:
            return {}

        cached = getattr(ticket, "_related_objects_panel_cache", None)
        if cached is not None:
            return cached

        request = context.get("request")
        grouped = {}
        for content_type, objects in ticket_service.get_related_objects(ticket).items():
            label = content_type.model_class()._meta.verbose_name_plural.title()  # pylint: disable=protected-access
            grouped[label] = {str(obj): self._render_object(ticket, content_type, obj, request) for obj in objects}
        ticket._related_objects_panel_cache = grouped  # pylint: disable=protected-access
        return grouped

    def render_key(self, key, value, context):
        """Show the object's own name, not a title-cased guess at a field name.

        The inherited implementation renders keys as field labels - underscores to spaces, then
        title case - which would turn a device named `edge_rtr_01` into "Edge Rtr 01".
        """
        return key

    @staticmethod
    def _render_object(ticket, content_type, obj, request):
        """The object as a link, followed by a detach control when this user may detach it."""
        link = hyperlinked_object(obj)
        may_detach = request is not None and request.user.has_perm(CHANGE_PERMISSION)
        if not ticket.is_open or not may_detach:
            return link

        url = reverse("plugins:nautobot_event_tracker:eventticket_detach", kwargs={"pk": ticket.pk})
        return format_html(
            '{} <a href="{}?object_type={}&amp;object_id={}" class="float-end" title="Detach">'
            '<span class="mdi mdi-link-variant-off" aria-hidden="true"></span>'
            '<span class="visually-hidden">Detach</span></a>',
            link,
            url,
            content_type.pk,
            obj.pk,
        )


class TransitionButton(Button):
    """A transition button that renders only when the workflow graph permits the move.

    The graph is consulted through the service layer; it is never restated here.
    """

    required_permissions = (TRANSITION_PERMISSION,)

    def __init__(self, to_status, **kwargs):
        """Record the target status this button moves the ticket to."""
        self.to_status = to_status
        kwargs.setdefault("label", TicketStatusChoices.as_dict()[to_status])
        kwargs.setdefault("color", TRANSITION_COLORS.get(to_status, ButtonColorChoices.DEFAULT))
        super().__init__(**kwargs)

    def get_link(self, context):
        """Link to the transition view, carrying the target status."""
        ticket = context.get("object")
        if ticket is None:
            return None
        base = reverse("plugins:nautobot_event_tracker:eventticket_transition", kwargs={"pk": ticket.pk})
        return f"{base}?to_status={self.to_status}"

    def should_render(self, context):
        """Render only for a legal next state, on top of the framework's permission check."""
        ticket = context.get("object")
        if ticket is None or not super().should_render(context):
            return False
        return self.to_status in ticket_service.get_allowed_transitions(ticket)


class TransitionDropdownButton(DropdownButton):
    """The transition menu, which renders only when it would have something in it.

    `DropdownButton` renders itself regardless of its children, so without this a closed ticket -
    or a user without the transition permission - would see a button that opens an empty menu.
    """

    def should_render(self, context):
        """Render only when at least one transition is available to this user on this ticket."""
        if not super().should_render(context):
            return False
        return any(child.should_render(context) for child in self.children)


class AttachObjectButton(Button):
    """Opens the attach-object form. Hidden once the ticket is resolved or closed."""

    link_name = "plugins:nautobot_event_tracker:eventticket_attach"
    required_permissions = (CHANGE_PERMISSION,)

    def should_render(self, context):
        """Only for open tickets, on top of the framework's permission check."""
        ticket = context.get("object")
        if ticket is None or not super().should_render(context):
            return False
        return ticket.is_open


class EventTypeUIViewSet(NautobotUIViewSet):
    """ViewSet for EventType views."""

    bulk_update_form_class = forms.EventTypeBulkEditForm
    filterset_class = filters.EventTypeFilterSet
    filterset_form_class = forms.EventTypeFilterForm
    form_class = forms.EventTypeForm
    lookup_field = "pk"
    # Annotated so the table's ticket count is one query rather than one per row.
    queryset = models.EventType.objects.annotate(ticket_count=count_related(models.EventTicket, "event_type"))
    serializer_class = serializers.EventTypeSerializer
    table_class = tables.EventTypeTable

    object_detail_content = ObjectDetailContent(
        panels=[
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=["name", "description", "default_severity", "enabled"],
            ),
            ObjectsTablePanel(
                weight=200,
                section=SectionChoices.FULL_WIDTH,
                table_class=tables.EventTicketTable,
                table_filter="event_type",
                related_field_name="event_type",
                label="Tickets",
                select_related_fields=["event_type", "assigned_to"],
            ),
        ],
    )


class EventTicketUIViewSet(NautobotUIViewSet):
    """ViewSet for EventTicket views."""

    bulk_update_form_class = forms.EventTicketBulkEditForm
    filterset_class = filters.EventTicketFilterSet
    filterset_form_class = forms.EventTicketFilterForm
    form_class = forms.EventTicketForm
    lookup_field = "pk"
    queryset = models.EventTicket.objects.select_related("event_type", "assigned_to")
    serializer_class = serializers.EventTicketSerializer
    table_class = tables.EventTicketTable

    def form_save(self, form, **kwargs):
        """Route both create and edit through the service layer.

        Without this, a ticket created in the UI would be written straight to the database with no
        `created` entry in its trail and no dedup handling, and an edit would change severity or
        assignee with nothing in the trail to say who did - the two paths that would quietly
        violate ADR 0001.
        """
        if self.action == "create":
            return self._create_from_form(form, **kwargs)
        if self.action == "update":
            self._apply_tracked_fields(form)
        return super().form_save(form, **kwargs)

    def _create_from_form(self, form, **kwargs):
        """Create through the service, then let the form finish its own work.

        The form's mixins own custom fields and relationships, which the service knows nothing
        about, so the form is re-pointed at the row the service wrote and saved as usual. A dedup
        join is the exception: that ticket belongs to an earlier event, and this form must not
        rewrite it.
        """
        data = form.cleaned_data
        ticket = ticket_service.create_ticket_for_user(
            user=self.request.user,
            title=data["title"],
            event_type=data["event_type"],
            severity=data.get("severity"),
            description=data.get("description", ""),
            dedup_key=data.get("dedup_key", ""),
            assignee=data.get("assigned_to"),
            tags=data.get("tags"),
        )
        if not ticket.was_created:
            return ticket

        # `clean()` has already stashed the submitted custom field values on the unsaved instance.
        ticket._custom_field_data = form.instance._custom_field_data  # pylint: disable=protected-access
        form.instance = ticket
        return super().form_save(form, **kwargs)

    def _apply_tracked_fields(self, form):
        """Send the edited fields that carry their own update type through the service.

        The form then saves an instance whose severity and assignee already hold the new values, so
        its own save is a no-op for them.
        """
        ticket = self.get_queryset().get(pk=form.instance.pk)
        data = form.cleaned_data
        ticket_service.set_severity(
            ticket=ticket,
            severity=data["severity"],
            source=TicketSourceChoices.HUMAN,
            user=self.request.user,
        )
        ticket_service.assign(
            ticket=ticket,
            assignee=data.get("assigned_to"),
            source=TicketSourceChoices.HUMAN,
            user=self.request.user,
        )

    object_detail_content = ObjectDetailContent(
        panels=[
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=list(tables.TICKET_CORE_FIELDS),
            ),
            ObjectTextPanel(
                weight=200,
                section=SectionChoices.LEFT_HALF,
                label="Description",
                object_field="description",
            ),
            ObjectTextPanel(
                weight=300,
                section=SectionChoices.LEFT_HALF,
                label="Resolution",
                object_field="resolution",
            ),
            RelatedObjectsPanel(
                weight=400,
                section=SectionChoices.RIGHT_HALF,
                label="Attached Objects",
                body_id="related-objects",
            ),
            ObjectsTablePanel(
                weight=500,
                section=SectionChoices.FULL_WIDTH,
                table_class=tables.TicketUpdateTable,
                table_filter="ticket",
                label="Update Trail",
                # Without these the table lazy-loads the user FK and the generic target once per
                # row; a busy ticket would cost ~2 queries per update rendered.
                select_related_fields=["user", "related_object_type"],
                prefetch_related_fields=["related_object"],
                enable_bulk_actions=False,
                # TicketUpdate deliberately has no list or add view, so the panel must not try to
                # link to one. Append-only means there is nothing to add from here either.
                add_button_route=None,
                enable_related_link=False,
                include_columns=["created", "update_type", "source", "user", "message", "related_object"],
            ),
            ObjectsTablePanel(
                weight=600,
                section=SectionChoices.FULL_WIDTH,
                table_class=tables.LLMUsageRecordTable,
                table_filter="ticket",
                label="LLM Usage",
                select_related_fields=["model__provider"],
                enable_bulk_actions=False,
                # Usage records are written by the service layer alone, so there is nothing to add
                # from here.
                add_button_route=None,
                include_columns=[
                    "called_at",
                    "model",
                    "purpose",
                    "prompt_tokens",
                    "completion_tokens",
                    "cost",
                    "success",
                ],
            ),
        ],
        extra_buttons=[
            AttachObjectButton(weight=100, label="Attach Object", icon="mdi-link-variant"),
            TransitionDropdownButton(
                weight=200,
                label="Transition",
                color=ButtonColorChoices.BLUE,
                icon="mdi-arrow-right-bold",
                children=[
                    TransitionButton(to_status, weight=index)
                    for index, to_status in enumerate(TicketStatusChoices.values())
                ],
            ),
        ],
    )


class EventTicketTransitionView(ObjectPermissionRequiredMixin, GenericView):
    """Perform a ticket status transition through the service layer.

    The same service call the REST API makes, so there is one implementation of a transition.
    """

    queryset = models.EventTicket.objects.all()

    def get_required_permission(self):
        """Transitioning needs its own permission, separate from change."""
        return TRANSITION_PERMISSION

    def get(self, request, pk):
        """Render the confirmation form for the requested target status."""
        ticket = get_object_or_404(self.queryset, pk=pk)
        form = forms.TicketTransitionForm(initial={"to_status": request.GET.get("to_status", "")})
        return _render_object_form(request, ticket, form)

    def post(self, request, pk):
        """Apply the transition, mapping service errors onto user-facing messages."""
        ticket = get_object_or_404(self.queryset, pk=pk)
        form = forms.TicketTransitionForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Invalid transition request.")
            return redirect(ticket.get_absolute_url())

        with _reporting_service_errors(request):
            ticket_service.transition(
                ticket=ticket,
                to_status=form.cleaned_data["to_status"],
                source=TicketSourceChoices.HUMAN,
                user=request.user,
                message=form.cleaned_data.get("message", ""),
                resolution=form.cleaned_data.get("resolution", ""),
            )
            messages.success(request, f"Ticket moved to '{form.cleaned_data['to_status']}'.")

        return redirect(ticket.get_absolute_url())


class EventTicketAttachView(ObjectPermissionRequiredMixin, GenericView):
    """Attach a Nautobot object to a ticket through the service layer."""

    queryset = models.EventTicket.objects.all()

    def get_required_permission(self):
        """Attaching is a change to the ticket."""
        return CHANGE_PERMISSION

    def get(self, request, pk):
        """Ask which kind of object is being attached, or which object once the kind is known.

        Two steps rather than one because the object picker is a `DynamicModelChoiceField`, which
        needs a concrete queryset - and so a chosen content type - to exist at all.
        """
        ticket = get_object_or_404(self.queryset, pk=pk)
        content_type = self._attachable_type(request.GET.get("object_type"))
        if content_type is None:
            return _render_object_form(request, ticket, forms.AttachObjectTypeForm())
        return _render_object_form(request, ticket, forms.AttachObjectForm(content_type))

    def post(self, request, pk):
        """Attach the selected object, or move the user on to the second step."""
        ticket = get_object_or_404(self.queryset, pk=pk)
        content_type = self._attachable_type(request.POST.get("object_type"))
        if content_type is None:
            messages.error(request, "Select an object type that may be attached to a ticket.")
            return _render_object_form(request, ticket, forms.AttachObjectTypeForm())

        if "object_id" not in request.POST:
            # The type picker was submitted; ask for the object itself.
            return _render_object_form(request, ticket, forms.AttachObjectForm(content_type))

        form = forms.AttachObjectForm(content_type, request.POST)
        if not form.is_valid():
            return _render_object_form(request, ticket, form)

        obj = form.cleaned_data["object_id"]
        with _reporting_service_errors(request):
            update = ticket_service.attach_object(
                ticket=ticket,
                obj=obj,
                source=TicketSourceChoices.HUMAN,
                user=request.user,
            )
            if update is None:
                messages.info(request, f"{obj} is already attached to this ticket.")
            else:
                messages.success(request, f"Attached {obj}.")

        return redirect(ticket.get_absolute_url())

    @staticmethod
    def _attachable_type(value):
        """Resolve a submitted content type ID against the allowlist, or None if it is not on it."""
        try:
            return ticket_service.get_attachable_content_types().filter(pk=value).first()
        except (TypeError, ValueError):
            return None


class EventTicketDetachView(ObjectPermissionRequiredMixin, GenericView):
    """Detach a Nautobot object from a ticket through the service layer."""

    queryset = models.EventTicket.objects.all()

    def get_required_permission(self):
        """Detaching is a change to the ticket."""
        return CHANGE_PERMISSION

    def get(self, request, pk):
        """Confirm the detach the panel's control asked for."""
        ticket = get_object_or_404(self.queryset, pk=pk)
        form = forms.DetachObjectForm(
            initial={
                "object_type": request.GET.get("object_type"),
                "object_id": request.GET.get("object_id"),
            }
        )
        return _render_object_form(request, ticket, form)

    def post(self, request, pk):
        """Detach the identified object.

        Deliberately does not consult the attachable-type allowlist: the service lets an object
        attached under an older configuration be removed, and the UI must not take that back.
        Detaching something that is not attached is a service-level no-op, so accepting any type
        here is safe.
        """
        ticket = get_object_or_404(self.queryset, pk=pk)
        form = forms.DetachObjectForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Could not identify the object to detach.")
            return redirect(ticket.get_absolute_url())

        with _reporting_service_errors(request):
            obj = ticket_service.resolve_object(form.cleaned_data["object_type"], form.cleaned_data["object_id"])
            update = ticket_service.detach_object(
                ticket=ticket,
                obj=obj,
                source=TicketSourceChoices.HUMAN,
                user=request.user,
            )
            if update is None:
                messages.info(request, f"{obj} is not attached to this ticket.")
            else:
                messages.success(request, f"Detached {obj}.")

        return redirect(ticket.get_absolute_url())


class DropsByReasonPanel(KeyValueTablePanel):
    """The drop breakdown from a counter row.

    The stock panel reads its data from a render context key; this one reads it off the object,
    where it lives. Keys are rule names and filter constants, so they are rendered verbatim rather
    than through `bettertitle()`, which would turn `lab-estate` into `Lab-Estate`.
    """

    def get_data(self, context):
        """The object's own breakdown."""
        return getattr(context.get("object"), "drops_by_reason", None) or {}

    def render_key(self, key, value, context):
        """A rule name is a name, not a label."""
        return key


class RecordUIViewSet(  # pylint: disable=too-many-ancestors,abstract-method
    ObjectListViewMixin,
    ObjectDetailViewMixin,
):
    """List and detail only, for rows that are records of what happened.

    No add, edit or delete route exists, because nothing outside the process that writes these
    rows has any business changing them. The posture is declared once here, as its API twin
    `api.views.RecordViewSet` does for REST.

    `abstract-method` is disabled deliberately: `NautobotViewSetMixin` declares the form-processing
    hooks for creating, updating and destroying objects, and a viewset offering none of those
    routes has no form to process.
    """

    action_buttons = ()


class IngestionStatsUIViewSet(RecordUIViewSet):  # pylint: disable=too-many-ancestors,abstract-method
    """Read-only views for the ingestion counters: the consumer is the only writer."""

    queryset = models.IngestionStats.objects.all()
    table_class = tables.IngestionStatsTable
    filterset_class = filters.IngestionStatsFilterSet
    filterset_form_class = forms.IngestionStatsFilterForm
    serializer_class = serializers.IngestionStatsSerializer

    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=("consumer_name", "topic", "bucket_start", *tables.INGESTION_STATS_COUNTER_FIELDS),
            ),
            # "Which rule is eating my events" is the question this page exists to answer, so the
            # breakdown gets a panel of its own rather than a cell in the table above.
            DropsByReasonPanel(
                weight=200,
                section=SectionChoices.RIGHT_HALF,
                label="Drops by reason",
            ),
        ),
    )


class LLMProviderUIViewSet(NautobotUIViewSet):
    """ViewSet for LLMProvider views."""

    bulk_update_form_class = forms.LLMProviderBulkEditForm
    filterset_class = filters.LLMProviderFilterSet
    filterset_form_class = forms.LLMProviderFilterForm
    form_class = forms.LLMProviderForm
    lookup_field = "pk"
    # Annotated so the table's model count is one query rather than one per row.
    queryset = models.LLMProvider.objects.select_related("external_integration").annotate(
        model_count=count_related(models.LLMModel, "provider")
    )
    serializer_class = serializers.LLMProviderSerializer
    table_class = tables.LLMProviderTable

    object_detail_content = ObjectDetailContent(
        panels=[
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=["name", "description", "provider_type", "external_integration", "enabled"],
            ),
            ObjectsTablePanel(
                weight=200,
                section=SectionChoices.FULL_WIDTH,
                table_class=tables.LLMModelTable,
                table_filter="provider",
                related_field_name="provider",
                label="Models",
                select_related_fields=["provider"],
            ),
        ],
    )


class LLMModelUIViewSet(NautobotUIViewSet):
    """ViewSet for LLMModel views."""

    bulk_update_form_class = forms.LLMModelBulkEditForm
    filterset_class = filters.LLMModelFilterSet
    filterset_form_class = forms.LLMModelFilterForm
    form_class = forms.LLMModelForm
    lookup_field = "pk"
    queryset = models.LLMModel.objects.select_related("provider")
    serializer_class = serializers.LLMModelSerializer
    table_class = tables.LLMModelTable

    object_detail_content = ObjectDetailContent(
        panels=[
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=list(forms.LLM_MODEL_FIELDS),
            ),
            ObjectsTablePanel(
                weight=200,
                section=SectionChoices.FULL_WIDTH,
                table_class=tables.LLMUsageRecordTable,
                table_filter="model",
                label="Recent Usage",
                select_related_fields=["model__provider", "ticket"],
                enable_bulk_actions=False,
                add_button_route=None,
            ),
        ],
    )


class LLMUsageRecordUIViewSet(RecordUIViewSet):  # pylint: disable=too-many-ancestors,abstract-method
    """Read-only views for the LLM usage records: the service layer is the only writer (rule L1)."""

    queryset = models.LLMUsageRecord.objects.select_related("model__provider", "ticket")
    table_class = tables.LLMUsageRecordTable
    filterset_class = filters.LLMUsageRecordFilterSet
    filterset_form_class = forms.LLMUsageRecordFilterForm
    serializer_class = serializers.LLMUsageRecordSerializer

    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=(*tables.LLM_USAGE_FIELDS, "request_id", "error"),
            ),
        ),
    )
