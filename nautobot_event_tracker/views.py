"""Views for nautobot_event_tracker.

Built on NautobotUIViewSet and the UI Component Framework only. The app ships no page templates.
See ADR 0008.
"""

import json
from contextlib import contextmanager

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured as DjangoImproperlyConfigured
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
    PostButton,
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
from nautobot_event_tracker.choices import AgentToolCallStatusChoices, TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.jobs import EventTicketAgentJob
from nautobot_event_tracker.services import agent as agent_service
from nautobot_event_tracker.services import mcp as mcp_service
from nautobot_event_tracker.services import rag as rag_service
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import AgentError, MCPError, TicketServiceError

TRANSITION_PERMISSION = "nautobot_event_tracker.transition_eventticket"
CHANGE_PERMISSION = "nautobot_event_tracker.change_eventticket"
#: Discovery writes both: the server's `last_discovered_at`, and a row per tool it found. The
#: first is what the view's queryset is restricted by, so it is the one the mixin is given; the
#: second goes in `additional_permissions`, which the mixin checks without touching the
#: restriction. Editing a server does not carry the right to widen what may be called.
DISCOVER_PERMISSION = "nautobot_event_tracker.change_mcpserver"
DISCOVER_TOOL_PERMISSION = "nautobot_event_tracker.add_mcptool"
#: Deciding a proposed tool call is its own permission, and `change_agenttoolcall` does not imply
#: it (7.3). Approving a call against the network is not the same right as editing a row.
APPROVE_PERMISSION = "nautobot_event_tracker.approve_agenttoolcall"
#: Running a Job is Nautobot's own permission, and the agent is a Job (ADR 0009). There is nothing
#: for this app to invent.
RUN_JOB_PERMISSION = "extras.run_job"


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


def pending_proposal(ticket):
    """The tool call on this ticket that somebody still has to decide, or None.

    Read through the agent service rather than queried here, so "which run owns this ticket" has
    one definition. Cached on the ticket for the life of the instance: the framework calls a
    panel's `get_data()` twice per render, and the cache belongs on the object rather than on the
    panel, which is a module-level singleton shared across requests and threads.
    """
    cached = getattr(ticket, "_pending_proposal_cache", None)
    if cached is None:
        cached = (agent_service.pending_call(agent_service.live_run(ticket)),)
        ticket._pending_proposal_cache = cached  # pylint: disable=protected-access
    return cached[0]


class SimilarTicketsPanel(KeyValueTablePanel):
    """Closed tickets that resemble this one, with what was done about them (Phase 5A).

    The only consumer of retrieval, and deliberately so: what is shown here is read by a person and
    goes nowhere near a prompt (rule R9). The corpus is built from payloads written by whoever
    emits the events, and a person in the loop is what keeps one poisoned closed ticket from
    steering every future investigation that resembles it.

    Cached on the ticket for the life of the instance, like `RelatedObjectsPanel`: the framework
    calls `get_data()` twice per render and this one can make a model call.
    """

    def get_data(self, context):
        """`{ticket title: rendered link and resolution}` for the framework to render."""
        ticket = context.get("object")
        request = context.get("request")
        if ticket is None or request is None:
            return {}

        cached = getattr(ticket, "_similar_tickets_panel_cache", None)
        if cached is not None:
            return cached

        matches = rag_service.similar_tickets(ticket, user=request.user)
        # Keyed with the distance as well as the title, because a dict silently collapses
        # duplicates and keeps the *last* one written - the farthest. Three closed tickets called
        # "leaf-01 ethernet-1/1 down" in one closeness band is not a corner case, it is precisely
        # the recurring fault this panel exists to surface, and it would have shown one of them.
        data = {
            f"{match.ticket.title} ({match.closeness}, {match.distance:.2f})": self._render(match) for match in matches
        }
        ticket._similar_tickets_panel_cache = data  # pylint: disable=protected-access
        return data

    def should_render(self, context):
        """Only for an open ticket, and only when there is something to show.

        A closed ticket's neighbours are of historical interest at best (12.2), and an empty panel
        headed "Similar Tickets" reads as a broken feature rather than as an honest "no".
        """
        ticket = context.get("object")
        if ticket is None or not ticket.is_open:
            return False
        return bool(self.get_data(context))

    def render_key(self, key, value, context):
        """A ticket's own title, not a title-cased guess at a field name."""
        return key

    @staticmethod
    def _render(match):
        """The neighbour as a link, followed by what was done about it."""
        link = hyperlinked_object(match.ticket)
        resolution = (match.ticket.resolution or "").strip()
        if not resolution:
            return link
        if len(resolution) > 300:
            resolution = resolution[:300] + "…"
        return format_html('{} <div class="text-secondary small">{}</div>', link, resolution)


class AgentProposalPanel(KeyValueTablePanel):
    """The tool call waiting on a decision, spelled out: which server, which tool, which arguments.

    The arguments are rendered in full rather than summarized, and that is the whole point of the
    panel. A gate is only as good as the person reading it, and what they have to read is what
    will actually be sent - not the model's account of it (section 11).
    """

    def get_data(self, context):
        """The proposal's own fields, or nothing when this ticket has no decision waiting."""
        ticket = context.get("object")
        if ticket is None:
            return {}
        proposal = pending_proposal(ticket)
        if proposal is None:
            return {}
        return {
            "MCP server": proposal.tool.server.name,
            "Tool": proposal.tool.name,
            "Arguments": format_html('<pre class="mb-0">{}</pre>', json.dumps(proposal.arguments, indent=2)),
            "Proposed": proposal.proposed_at,
        }


class AgentDecisionButton(PostButton):
    """Approve or deny the proposal waiting on this ticket. Hidden when there is none.

    A `PostButton`, like Discover Tools and for the same reason: this writes rows and calls a
    network tool, and a control that issues a GET is a control a link preview can press.
    """

    required_permissions = (APPROVE_PERMISSION,)

    def __init__(self, approve, **kwargs):
        """Record which decision this button makes."""
        self.approve = approve
        super().__init__(**kwargs)

    def get_link(self, context):
        """Link to the decision view for the proposal, not for the ticket."""
        ticket = context.get("object")
        proposal = pending_proposal(ticket) if ticket is not None else None
        if proposal is None:
            return None
        name = "agenttoolcall_approve" if self.approve else "agenttoolcall_deny"
        return reverse(f"plugins:nautobot_event_tracker:{name}", kwargs={"pk": proposal.pk})

    def should_render(self, context):
        """Only when there is something to decide, on top of the framework's permission check."""
        ticket = context.get("object")
        if ticket is None or not super().should_render(context):
            return False
        return pending_proposal(ticket) is not None


class RunAgentButton(Button):
    """Launch the agent Job on this ticket, from the page a person is standing on when they want it.

    A link to Nautobot's own Job run form with the ticket pre-filled, rather than a control that
    enqueues the Job itself: the run form is where a person sees the Job's name, its description
    and its time limit, and it is the page Nautobot's own permissions are written against.
    """

    required_permissions = (RUN_JOB_PERMISSION,)

    def get_link(self, context):
        """The Job run form, with this ticket filled in."""
        ticket = context.get("object")
        if ticket is None:
            return None
        base = reverse(
            "extras:job_run_by_class_path",
            kwargs={"class_path": f"{EventTicketAgentJob.__module__}.{EventTicketAgentJob.__name__}"},
        )
        return f"{base}?ticket={ticket.pk}"

    def should_render(self, context):
        """Only for a ticket an agent may act on at all (S3), and only when agents are configured."""
        ticket = context.get("object")
        if ticket is None or not super().should_render(context):
            return False
        if not ticket.is_open:
            return False
        try:
            return agent_service.get_settings().enabled
        except DjangoImproperlyConfigured:
            # A settings fault is worth showing rather than hiding: the button leads to the Job,
            # and the Job's own refusal says what is wrong in one sentence.
            return True


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
            SimilarTicketsPanel(
                weight=430,
                section=SectionChoices.RIGHT_HALF,
                label="Similar Tickets",
                body_id="similar-tickets",
            ),
            AgentProposalPanel(
                weight=450,
                section=SectionChoices.RIGHT_HALF,
                label="Waiting for Your Decision",
                body_id="agent-proposal",
            ),
            ObjectsTablePanel(
                weight=550,
                section=SectionChoices.FULL_WIDTH,
                table_class=tables.AgentRunTable,
                table_filter="ticket",
                label="Agent Runs",
                select_related_fields=["started_by"],
                enable_bulk_actions=False,
                # A run is started by the Job, never from a form, so there is nothing to add here.
                add_button_route=None,
                include_columns=["started_at", "status", "started_by", "iterations", "finished_at"],
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
            RunAgentButton(
                weight=150,
                label="Investigate with Agent",
                icon="mdi-robot-outline",
                color=ButtonColorChoices.BLUE,
            ),
            AgentDecisionButton(
                True,
                weight=160,
                label="Approve Tool Call",
                icon="mdi-check-bold",
                color=ButtonColorChoices.GREEN,
            ),
            AgentDecisionButton(
                False,
                weight=170,
                label="Deny Tool Call",
                icon="mdi-close-thick",
                color=ButtonColorChoices.RED,
            ),
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


class DiscoverToolsButton(PostButton):
    """Reads a server's tool list and reconciles the registry with it. Hidden for a disabled server.

    A `PostButton`, not a `Button`: the plain one renders an anchor, which issues a GET, and this
    action writes rows. It also means the request carries a CSRF token, which a link cannot.
    """

    link_name = "plugins:nautobot_event_tracker:mcpserver_discover"
    required_permissions = (DISCOVER_PERMISSION, DISCOVER_TOOL_PERMISSION)

    def should_render(self, context):
        """Only for an enabled server, on top of the framework's permission check."""
        server = context.get("object")
        if server is None or not super().should_render(context):
            return False
        return server.enabled


class MCPServerUIViewSet(NautobotUIViewSet):
    """ViewSet for MCPServer views."""

    bulk_update_form_class = forms.MCPServerBulkEditForm
    filterset_class = filters.MCPServerFilterSet
    filterset_form_class = forms.MCPServerFilterForm
    form_class = forms.MCPServerForm
    lookup_field = "pk"
    # Both counts annotated: the gap between them is the thing worth seeing on the list page. A
    # server offering forty tools of which two are enabled is the default-deny rule working, and
    # one where the two numbers are equal is a review nobody did.
    queryset = models.MCPServer.objects.select_related("external_integration").annotate(
        tool_count=count_related(models.MCPTool, "server"),
        enabled_tool_count=count_related(models.MCPTool, "server", filter_dict={"enabled": True}),
    )
    serializer_class = serializers.MCPServerSerializer
    table_class = tables.MCPServerTable

    object_detail_content = ObjectDetailContent(
        panels=[
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=["name", "description", "external_integration", "enabled", "last_discovered_at"],
            ),
            ObjectsTablePanel(
                weight=200,
                section=SectionChoices.FULL_WIDTH,
                table_class=tables.MCPToolTable,
                table_filter="server",
                related_field_name="server",
                label="Tools",
                select_related_fields=["server"],
            ),
        ],
        extra_buttons=[
            DiscoverToolsButton(
                weight=100,
                label="Discover Tools",
                icon="mdi-magnify-scan",
                color=ButtonColorChoices.BLUE,
            ),
        ],
    )


class MCPToolUIViewSet(NautobotUIViewSet):
    """ViewSet for MCPTool views."""

    bulk_update_form_class = forms.MCPToolBulkEditForm
    filterset_class = filters.MCPToolFilterSet
    filterset_form_class = forms.MCPToolFilterForm
    form_class = forms.MCPToolForm
    lookup_field = "pk"
    queryset = models.MCPTool.objects.select_related("server")
    serializer_class = serializers.MCPToolSerializer
    table_class = tables.MCPToolTable

    object_detail_content = ObjectDetailContent(
        panels=[
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=[
                    "server",
                    "name",
                    "description",
                    "enabled",
                    "mutating",
                    "advertised_read_only",
                    "last_seen_at",
                ],
            ),
            ObjectTextPanel(
                weight=200,
                section=SectionChoices.RIGHT_HALF,
                label="Input Schema",
                object_field="input_schema",
                render_as=ObjectTextPanel.RenderOptions.JSON,
            ),
        ],
    )


class MCPServerDiscoverView(ObjectPermissionRequiredMixin, GenericView):
    """Read one server's tool list and reconcile the registry with it (rule M5).

    POST only: it writes rows, and a discovery reachable by following a link is a discovery a
    crawler can trigger. The button posts.
    """

    queryset = models.MCPServer.objects.all()
    #: Checked alongside the required permission, without being used to restrict this view's
    #: queryset - which is what makes it the right home for a permission on another model.
    additional_permissions = [DISCOVER_TOOL_PERMISSION]

    def get_required_permission(self):
        """The permission the queryset is restricted by: discovery stamps the server it is given."""
        return DISCOVER_PERMISSION

    def post(self, request, pk):
        """Discover, then say what changed in the terms an operator has to act on."""
        server = get_object_or_404(self.queryset, pk=pk)
        try:
            report = mcp_service.discover(server)
        except MCPError as error:
            messages.error(request, str(error))
            return redirect(server.get_absolute_url())
        except DjangoImproperlyConfigured as error:
            # The missing `mcp` extra, deliberately outside the MCPError family: a deployment
            # fault, and one an operator can only fix by installing something.
            messages.error(request, str(error))
            return redirect(server.get_absolute_url())

        messages.success(request, f"Discovered tools on '{server}': {report.summary()}.")
        if report.needs_attention:
            # The whole point of the default-deny rule is that somebody looks. Saying so here is
            # what stops a new tool sitting disabled and unnoticed for a month.
            messages.warning(
                request,
                "These tools are disabled and need review: " + ", ".join(tool.name for tool in report.needs_attention),
            )
        return redirect(server.get_absolute_url())


class CallDecisionButton(PostButton):
    """Approve or deny, on the tool call's own page. Hidden once the call has been decided."""

    required_permissions = (APPROVE_PERMISSION,)

    def __init__(self, approve, **kwargs):
        """Record which decision this button makes."""
        self.approve = approve
        super().__init__(**kwargs)

    def get_link(self, context):
        """Link to this call's decision route."""
        call = context.get("object")
        if call is None:
            return None
        name = "agenttoolcall_approve" if self.approve else "agenttoolcall_deny"
        return reverse(f"plugins:nautobot_event_tracker:{name}", kwargs={"pk": call.pk})

    def should_render(self, context):
        """Only while the call is still waiting: a call is decided once (7.3)."""
        call = context.get("object")
        if call is None or not super().should_render(context):
            return False
        return call.status == AgentToolCallStatusChoices.PROPOSED


class AgentRunUIViewSet(RecordUIViewSet):  # pylint: disable=too-many-ancestors,abstract-method
    """Read-only views for agent runs: `services/agent.py` is the only writer.

    No add form, and no edit form either. A run is started by the Job, and what it did is not a
    thing anybody edits afterwards - which is most of what makes the transcript worth reading.
    """

    queryset = models.AgentRun.objects.select_related("ticket", "started_by", "job_result").annotate(
        tool_call_count=count_related(models.AgentToolCall, "run")
    )
    table_class = tables.AgentRunTable
    filterset_class = filters.AgentRunFilterSet
    filterset_form_class = forms.AgentRunFilterForm
    serializer_class = serializers.AgentRunSerializer

    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=(*tables.AGENT_RUN_FIELDS, "job_result", "parent", "error"),
            ),
            ObjectTextPanel(
                weight=200,
                section=SectionChoices.RIGHT_HALF,
                label="Transcript",
                object_field="transcript",
                render_as=ObjectTextPanel.RenderOptions.JSON,
            ),
            ObjectsTablePanel(
                weight=300,
                section=SectionChoices.FULL_WIDTH,
                table_class=tables.AgentToolCallTable,
                table_filter="run",
                related_field_name="run",
                label="Tool Calls",
                select_related_fields=["tool__server", "decided_by"],
                enable_bulk_actions=False,
                add_button_route=None,
            ),
        ),
    )


class AgentToolCallUIViewSet(RecordUIViewSet):  # pylint: disable=too-many-ancestors,abstract-method
    """Read-only views for agent tool calls, with the two decision controls.

    Read-only in the ordinary sense - nothing here edits a row - and yet this is where a person
    approves a call against the network. The two are not in tension: approving is its own action
    with its own permission and its own trail entry, and it is not an edit.
    """

    queryset = models.AgentToolCall.objects.select_related("run__ticket", "tool__server", "decided_by")
    table_class = tables.AgentToolCallTable
    filterset_class = filters.AgentToolCallFilterSet
    filterset_form_class = forms.AgentToolCallFilterForm
    serializer_class = serializers.AgentToolCallSerializer

    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=(*tables.AGENT_TOOL_CALL_FIELDS, "tool_fingerprint", "error"),
            ),
            ObjectTextPanel(
                weight=200,
                section=SectionChoices.RIGHT_HALF,
                label="Arguments",
                object_field="arguments",
                render_as=ObjectTextPanel.RenderOptions.JSON,
            ),
            ObjectTextPanel(
                weight=300,
                section=SectionChoices.FULL_WIDTH,
                label="Result",
                object_field="result",
                render_as=ObjectTextPanel.RenderOptions.JSON,
            ),
        ),
        extra_buttons=(
            CallDecisionButton(
                True,
                weight=100,
                label="Approve",
                icon="mdi-check-bold",
                color=ButtonColorChoices.GREEN,
            ),
            CallDecisionButton(
                False,
                weight=200,
                label="Deny",
                icon="mdi-close-thick",
                color=ButtonColorChoices.RED,
            ),
        ),
    )


class AgentToolCallDecisionView(ObjectPermissionRequiredMixin, GenericView):
    """Approve or deny one proposed tool call (7.2).

    POST only. Approving a call is the moment this whole phase exists to control, and a control
    reachable by following a link is one a crawler, a link preview or a prefetching browser can
    press. Both buttons post.

    `approve` is set by the URL rather than read from the request, so the two decisions are two
    routes and neither can be reached by editing a form field.
    """

    queryset = models.AgentToolCall.objects.select_related("run__ticket", "tool__server")
    approve = True

    def get_required_permission(self):
        """Deciding needs its own permission; `change` does not imply it."""
        return APPROVE_PERMISSION

    def post(self, request, pk):
        """Record the decision, then enqueue the resumption when the answer was yes."""
        tool_call = get_object_or_404(self.queryset, pk=pk)
        ticket = tool_call.run.ticket

        try:
            if self.approve:
                agent_service.approve_tool_call(tool_call=tool_call, user=request.user)
            else:
                agent_service.deny_tool_call(tool_call=tool_call, user=request.user)
        except (AgentError, TicketServiceError) as error:
            messages.error(request, str(error))
            return redirect(ticket.get_absolute_url())

        if not self.approve:
            messages.success(request, f"Denied '{tool_call.tool}'. The run has ended.")
            return redirect(ticket.get_absolute_url())

        messages.success(request, f"Approved '{tool_call.tool}'.")
        self._enqueue_resumption(request, ticket)
        return redirect(ticket.get_absolute_url())

    @staticmethod
    def _enqueue_resumption(request, ticket):
        """Start the run that makes the approved call, and say so if it cannot be started.

        Enqueued rather than run here: the call is a network call with a timeout, and a web request
        is not the place for it. A deployment with no worker, or with the Job disabled, gets a
        message saying to run it from the ticket - the approval itself has already been recorded
        and is not lost.
        """
        from nautobot.extras.models import Job, JobResult  # pylint: disable=import-outside-toplevel

        class_path = f"{EventTicketAgentJob.__module__}.{EventTicketAgentJob.__name__}"
        try:
            job_model = Job.objects.restrict(request.user, "run").get_for_class_path(class_path)
            JobResult.enqueue_job(job_model, request.user, job_kwargs={"ticket": str(ticket.pk)})
        except Exception as error:  # pylint: disable=broad-except
            messages.warning(
                request,
                f"The approved call was recorded but the agent could not be restarted ({error}). "
                "Run the agent on this ticket again to make the call.",
            )


class AgentToolCallApproveView(AgentToolCallDecisionView):
    """Approve one proposed tool call."""

    approve = True


class AgentToolCallDenyView(AgentToolCallDecisionView):
    """Deny one proposed tool call, which ends the run (13.8)."""

    approve = False


class TicketEmbeddingUIViewSet(RecordUIViewSet):  # pylint: disable=too-many-ancestors,abstract-method
    """Read-only views for the retrieval corpus: `services/rag.py` is the only writer.

    Worth a page at all because "what is in the corpus" is a real operational question - most
    sharply after changing embedding model, when the Similar Tickets panel goes quiet and the
    answer is that every row here belongs to the old one.
    """

    queryset = models.TicketEmbedding.objects.select_related("ticket", "model__provider")
    table_class = tables.TicketEmbeddingTable
    filterset_class = filters.TicketEmbeddingFilterSet
    filterset_form_class = forms.TicketEmbeddingFilterForm
    serializer_class = serializers.TicketEmbeddingSerializer

    def get_queryset(self):
        """Only embeddings of tickets this user may read - rule R6, on this surface too.

        `document` is a verbatim copy of its ticket, so without this the corpus is a way around
        ticket permissions: an ObjectPermission constraint on `EventTicket` simply stops applying,
        because nothing carries it across the relation.
        """
        return rag_service.visible_embeddings(super().get_queryset(), self.request.user)

    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields=("ticket", "model", "dimensions", "indexed_at", "document_fingerprint"),
            ),
            ObjectTextPanel(
                weight=200,
                section=SectionChoices.RIGHT_HALF,
                label="Document",
                object_field="document",
            ),
        ),
    )
