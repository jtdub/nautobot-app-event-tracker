"""Views for nautobot_event_tracker.

Built on NautobotUIViewSet and the UI Component Framework only. The app ships no page templates.
See ADR 0008.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from nautobot.apps.ui import (
    Button,
    ButtonColorChoices,
    DropdownButton,
    GroupedKeyValueTablePanel,
    ObjectDetailContent,
    ObjectFieldsPanel,
    ObjectsTablePanel,
    ObjectTextPanel,
    SectionChoices,
)
from nautobot.apps.views import GenericView, NautobotUIViewSet, ObjectPermissionRequiredMixin
from nautobot.core.templatetags import helpers

from nautobot_event_tracker import filters, forms, models, tables
from nautobot_event_tracker.api import serializers
from nautobot_event_tracker.choices import TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import (
    InvalidActorError,
    InvalidTransitionError,
    TicketImmutableError,
)

TRANSITION_PERMISSION = "nautobot_event_tracker.transition_eventticket"
CHANGE_PERMISSION = "nautobot_event_tracker.change_eventticket"

#: Button colour per target status, so the destructive-looking moves read as such.
TRANSITION_COLORS = {
    TicketStatusChoices.TRIAGED: ButtonColorChoices.BLUE,
    TicketStatusChoices.IN_PROGRESS: ButtonColorChoices.BLUE,
    TicketStatusChoices.SUPPRESSED: ButtonColorChoices.GREY,
    TicketStatusChoices.RESOLVED: ButtonColorChoices.GREEN,
    TicketStatusChoices.CLOSED: ButtonColorChoices.GREY,
}


class RelatedObjectsPanel(GroupedKeyValueTablePanel):
    """Attached Nautobot objects, grouped by object type.

    Attachment is derived from the ticket's update trail, so the data comes from the service layer
    rather than from a relation on the ticket.
    """

    def get_data(self, context):
        """Return `{type label: {object label: rendered link}}` for the framework to render."""
        ticket = context.get("object")
        if ticket is None:
            return {}

        grouped = {}
        for content_type, objects in ticket_service.get_related_objects(ticket).items():
            label = content_type.model_class()._meta.verbose_name_plural.title()  # pylint: disable=protected-access
            grouped[label] = {str(obj): helpers.hyperlinked_object(obj) for obj in objects}
        return grouped


class TransitionButton(Button):
    """A transition button that renders only when the workflow graph permits the move.

    The graph is consulted through the service layer; it is never restated here.
    """

    def __init__(self, to_status, **kwargs):
        """Record the target status this button moves the ticket to."""
        self.to_status = to_status
        kwargs.setdefault("label", f"{dict(TicketStatusChoices.CHOICES)[to_status]}")
        kwargs.setdefault("color", TRANSITION_COLORS.get(to_status, ButtonColorChoices.DEFAULT))
        kwargs.setdefault("link_name", "plugins:nautobot_event_tracker:eventticket_transition")
        super().__init__(**kwargs)

    def get_link(self, context):
        """Link to the transition view, carrying the target status."""
        ticket = context.get("object")
        if ticket is None:
            return None
        base = reverse(
            "plugins:nautobot_event_tracker:eventticket_transition",
            kwargs={"pk": ticket.pk},
        )
        return f"{base}?to_status={self.to_status}"

    def should_render(self, context):
        """Render only for a legal next state, and only with the transition permission."""
        ticket = context.get("object")
        user = context.get("user")
        if ticket is None or user is None:
            return False
        if not user.has_perm(TRANSITION_PERMISSION):
            return False
        return self.to_status in ticket_service.get_allowed_transitions(ticket)


class AttachObjectButton(Button):
    """Opens the attach-object form. Hidden once the ticket is resolved or closed."""

    def get_link(self, context):
        """Link to the attach view for this ticket."""
        ticket = context.get("object")
        if ticket is None:
            return None
        return reverse("plugins:nautobot_event_tracker:eventticket_attach", kwargs={"pk": ticket.pk})

    def should_render(self, context):
        """Only for open tickets, and only with change permission."""
        ticket = context.get("object")
        user = context.get("user")
        if ticket is None or user is None:
            return False
        return ticket.is_open and user.has_perm(CHANGE_PERMISSION)


class EventTypeUIViewSet(NautobotUIViewSet):
    """ViewSet for EventType views."""

    bulk_update_form_class = forms.EventTypeBulkEditForm
    filterset_class = filters.EventTypeFilterSet
    filterset_form_class = forms.EventTypeFilterForm
    form_class = forms.EventTypeForm
    lookup_field = "pk"
    queryset = models.EventType.objects.all()
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
        """Route creation through the service layer.

        Without this, a ticket created in the UI would be written straight to the database with no
        `created` entry in its trail and no dedup handling - the one path that would quietly
        violate ADR 0001.
        """
        if self.action != "create":
            return super().form_save(form, **kwargs)

        data = form.cleaned_data
        return ticket_service.create_ticket_for_user(
            user=self.request.user,
            title=data["title"],
            event_type=data["event_type"],
            severity=data.get("severity"),
            description=data.get("description", ""),
            dedup_key=data.get("dedup_key", ""),
            assignee=data.get("assigned_to"),
            tags=data.get("tags"),
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
                enable_bulk_actions=False,
                # TicketUpdate deliberately has no list or add view, so the panel must not try to
                # link to one. Append-only means there is nothing to add from here either.
                add_button_route=None,
                enable_related_link=False,
                include_columns=["created", "update_type", "source", "user", "message", "related_object"],
            ),
        ],
        extra_buttons=[
            AttachObjectButton(weight=100, label="Attach Object", icon="mdi-link-variant"),
            DropdownButton(
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
        to_status = request.GET.get("to_status", "")
        form = forms.TicketTransitionForm(initial={"to_status": to_status})
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

    def post(self, request, pk):
        """Apply the transition, mapping service errors onto user-facing messages."""
        ticket = get_object_or_404(self.queryset, pk=pk)
        form = forms.TicketTransitionForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Invalid transition request.")
            return redirect(ticket.get_absolute_url())

        try:
            ticket_service.transition(
                ticket=ticket,
                to_status=form.cleaned_data["to_status"],
                source=TicketSourceChoices.HUMAN,
                user=request.user,
                message=form.cleaned_data.get("message", ""),
                resolution=form.cleaned_data.get("resolution", ""),
            )
        except (InvalidTransitionError, TicketImmutableError, InvalidActorError) as error:
            messages.error(request, str(error))
        except DjangoValidationError as error:
            messages.error(request, "; ".join(error.messages))
        else:
            messages.success(request, f"Ticket moved to '{form.cleaned_data['to_status']}'.")

        return redirect(ticket.get_absolute_url())


class EventTicketAttachView(ObjectPermissionRequiredMixin, GenericView):
    """Attach a Nautobot object to a ticket through the service layer."""

    queryset = models.EventTicket.objects.all()

    def get_required_permission(self):
        """Attaching is a change to the ticket."""
        return CHANGE_PERMISSION

    def get(self, request, pk):
        """Render the attach form."""
        ticket = get_object_or_404(self.queryset, pk=pk)
        return render(
            request,
            "generic/object_edit.html",
            {
                "obj": ticket,
                "obj_type": ticket._meta.verbose_name,  # pylint: disable=protected-access
                "form": forms.AttachObjectForm(),
                "return_url": ticket.get_absolute_url(),
                "editing": True,
            },
        )

    def post(self, request, pk):
        """Attach the selected object."""
        ticket = get_object_or_404(self.queryset, pk=pk)
        form = forms.AttachObjectForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Select an object type and an object to attach.")
            return redirect(ticket.get_absolute_url())

        content_type = form.cleaned_data["object_type"]
        model_class = content_type.model_class()
        obj = model_class.objects.filter(pk=form.cleaned_data["object_id"]).first()
        if obj is None:
            messages.error(request, "That object no longer exists.")
            return redirect(ticket.get_absolute_url())

        try:
            update = ticket_service.attach_object(
                ticket=ticket,
                obj=obj,
                source=TicketSourceChoices.HUMAN,
                user=request.user,
            )
        except (TicketImmutableError, InvalidActorError) as error:
            messages.error(request, str(error))
        except DjangoValidationError as error:
            messages.error(request, "; ".join(error.messages))
        else:
            if update is None:
                messages.info(request, f"{obj} is already attached to this ticket.")
            else:
                messages.success(request, f"Attached {obj}.")

        return redirect(ticket.get_absolute_url())


class EventTicketDetachView(ObjectPermissionRequiredMixin, GenericView):
    """Detach a Nautobot object from a ticket through the service layer."""

    queryset = models.EventTicket.objects.all()

    def get_required_permission(self):
        """Detaching is a change to the ticket."""
        return CHANGE_PERMISSION

    def post(self, request, pk):
        """Detach the identified object."""
        ticket = get_object_or_404(self.queryset, pk=pk)
        content_types = forms.attachable_content_types().filter(pk=request.POST.get("object_type"))
        content_type = content_types.first()
        obj = None
        if content_type is not None:
            model_class = content_type.model_class()
            obj = model_class.objects.filter(pk=request.POST.get("object_id")).first()

        if obj is None:
            messages.error(request, "Could not identify the object to detach.")
            return redirect(ticket.get_absolute_url())

        try:
            ticket_service.detach_object(
                ticket=ticket,
                obj=obj,
                source=TicketSourceChoices.HUMAN,
                user=request.user,
            )
        except (TicketImmutableError, InvalidActorError) as error:
            messages.error(request, str(error))
        else:
            messages.success(request, f"Detached {obj}.")

        return redirect(ticket.get_absolute_url())
