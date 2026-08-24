"""API views for nautobot_event_tracker."""

from collections import namedtuple
from contextlib import contextmanager

from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from nautobot.apps.api import NautobotModelViewSet, ReadOnlyModelViewSet

# The one deliberate internal import in the app. `TokenPermissions` is not re-exported from
# `nautobot.apps.api`, so there is no public path to it and no substitute that enforces token
# write-permissions the same way. Anything outside `nautobot.apps.*` may move in a patch release:
# re-check this import on every Nautobot upgrade, minor or otherwise.
from nautobot.core.api.authentication import TokenPermissions
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.serializers import ListSerializer

from nautobot_event_tracker import filters
from nautobot_event_tracker.api import serializers
from nautobot_event_tracker.choices import TicketSourceChoices
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
    TicketEmbedding,
    TicketUpdate,
)
from nautobot_event_tracker.services import agent as agent_service
from nautobot_event_tracker.services import rag as rag_service
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import (
    AgentBusyError,
    AgentDecisionError,
    AgentError,
    InvalidActorError,
    InvalidTransitionError,
    TicketImmutableError,
)


class Conflict(APIException):
    """A well-formed request that the ticket's current state does not permit."""

    status_code = http_status.HTTP_409_CONFLICT


@contextmanager
def _reporting_service_errors():
    """Map the service layer's refusals onto their HTTP meanings.

    Every action goes through this, so the same refusal reads the same way whichever endpoint
    provoked it: a state conflict is a 409, anything else the service rejects is a 400.
    """
    try:
        yield
    except (InvalidTransitionError, TicketImmutableError, AgentBusyError, AgentDecisionError) as error:
        raise Conflict(str(error)) from error
    except (InvalidActorError, AgentError, DjangoValidationError) as error:
        raise ValidationError(getattr(error, "messages", [str(error)])) from error


class TicketChangeActionPermissions(TokenPermissions):
    """Permissions for ticket actions that modify an existing ticket.

    The stock map demands `add_<model>` for any POST, because a POST normally creates an object.
    These actions post to an existing ticket to change it, so `change_<model>` is the honest
    requirement.
    """

    perms_map = {
        **TokenPermissions.perms_map,
        "POST": ["%(app_label)s.change_%(model_name)s"],
    }


class TicketTransitionPermissions(TokenPermissions):
    """Permissions for the transition action.

    Transitioning deliberately does not imply `change`, so that an operator can move tickets
    through the workflow without being able to rewrite their content. GET keeps the inherited
    `view` requirement: reading the legal next states is reading the ticket.
    """

    perms_map = {
        **TokenPermissions.perms_map,
        "POST": ["%(app_label)s.transition_%(model_name)s"],
    }


#: What each custom ticket action requires: the object-level permission the queryset is restricted
#: by, and the model-level permission class DRF checks first. Keeping both halves on one line means
#: neither can be updated without the other coming into view.
ActionPolicy = namedtuple("ActionPolicy", ["object_action", "permission_class"])

ACTION_POLICIES = {
    "transition": ActionPolicy("view", TicketTransitionPermissions),
    "comment": ActionPolicy("change", TicketChangeActionPermissions),
    "attach": ActionPolicy("change", TicketChangeActionPermissions),
    "detach": ActionPolicy("change", TicketChangeActionPermissions),
}


class EventTypeViewSet(NautobotModelViewSet):  # pylint: disable=too-many-ancestors
    """EventType viewset."""

    queryset = EventType.objects.all()
    serializer_class = serializers.EventTypeSerializer
    filterset_class = filters.EventTypeFilterSet


class RecordViewSet(ReadOnlyModelViewSet):  # pylint: disable=too-many-ancestors
    """A viewset for rows that are records of what happened, which no client may rewrite.

    Read-only by construction - GET, HEAD and OPTIONS, everything else a 405 regardless of
    permissions. The posture is declared once here so a fourth record-like model cannot forget
    half of it: the trail (append-only, service-written), the ingestion counters (consumer-
    written), and the LLM usage accounting (rule L1, service-written) all mean the same thing
    by "read-only".
    """

    http_method_names = ["get", "head", "options"]


class TicketUpdateViewSet(RecordViewSet):  # pylint: disable=too-many-ancestors
    """TicketUpdate viewset. New updates appear as a side effect of ticket actions."""

    queryset = TicketUpdate.objects.select_related("ticket", "user", "related_object_type")
    serializer_class = serializers.TicketUpdateSerializer
    filterset_class = filters.TicketUpdateFilterSet


class IngestionStatsViewSet(RecordViewSet):  # pylint: disable=too-many-ancestors
    """IngestionStats viewset. The rows are one process's record of what it saw."""

    queryset = IngestionStats.objects.all()
    serializer_class = serializers.IngestionStatsSerializer
    filterset_class = filters.IngestionStatsFilterSet


class LLMProviderViewSet(NautobotModelViewSet):  # pylint: disable=too-many-ancestors
    """LLMProvider viewset."""

    queryset = LLMProvider.objects.select_related("external_integration")
    serializer_class = serializers.LLMProviderSerializer
    filterset_class = filters.LLMProviderFilterSet


class LLMModelViewSet(NautobotModelViewSet):  # pylint: disable=too-many-ancestors
    """LLMModel viewset."""

    queryset = LLMModel.objects.select_related("provider")
    serializer_class = serializers.LLMModelSerializer
    filterset_class = filters.LLMModelFilterSet


class LLMUsageRecordViewSet(RecordViewSet):  # pylint: disable=too-many-ancestors
    """LLMUsageRecord viewset. The rows are the service layer's accounting of calls it made."""

    queryset = LLMUsageRecord.objects.select_related("model__provider", "ticket")
    serializer_class = serializers.LLMUsageRecordSerializer
    filterset_class = filters.LLMUsageRecordFilterSet


class MCPServerViewSet(NautobotModelViewSet):  # pylint: disable=too-many-ancestors
    """MCPServer viewset."""

    queryset = MCPServer.objects.select_related("external_integration")
    serializer_class = serializers.MCPServerSerializer
    filterset_class = filters.MCPServerFilterSet


class MCPToolViewSet(NautobotModelViewSet):  # pylint: disable=too-many-ancestors
    """MCPTool viewset.

    Writable, because enabling a tool is an operator decision and an operator may reasonably make
    it from a script. It is the same decision the UI offers and it needs the same permission; what
    no route offers is a way to call one.
    """

    queryset = MCPTool.objects.select_related("server")
    serializer_class = serializers.MCPToolSerializer
    filterset_class = filters.MCPToolFilterSet


class EventTicketViewSet(NautobotModelViewSet):  # pylint: disable=too-many-ancestors
    """EventTicket viewset.

    Every mutation routes through `services.tickets`. The generic create/update routes exist for
    ticket content; state changes go through the action endpoints below.
    """

    queryset = EventTicket.objects.select_related("event_type", "assigned_to").prefetch_related("tags")
    serializer_class = serializers.EventTicketSerializer
    filterset_class = filters.EventTicketFilterSet

    def restrict_queryset(self, request, *args, **kwargs):
        """Map the custom actions onto the object permission they really need.

        The stock implementation derives the permission from the HTTP method, so a POST restricts
        the queryset to objects the user may *add* - which matches nothing, and the action 404s on
        a ticket that plainly exists. Same pattern as Nautobot's own Job `/cancel/` endpoint.
        """
        policy = ACTION_POLICIES.get(self.action)
        if policy is not None and request.user.is_authenticated:
            self.queryset = self.queryset.restrict(request.user, policy.object_action)
        else:
            super().restrict_queryset(request, *args, **kwargs)

    def get_permissions(self):
        """Custom actions post to an existing ticket, so the stock add/change map does not fit."""
        policy = ACTION_POLICIES.get(self.action)
        if policy is not None:
            return [policy.permission_class()]
        return super().get_permissions()

    def perform_create(self, serializer):
        """Create through the service layer so the ticket gets its trail and dedup behaviour.

        A list payload is a bulk create in Nautobot, so this handles one ticket or many. Object
        permissions are enforced the way the base class does it, by checking the created rows
        against the restricted queryset inside the transaction that made them.

        Service refusals go through the same mapping as every other action: creating a ticket with
        a disabled event type is a 400 with the service's own message, not a 500.
        """
        try:
            with transaction.atomic(), _reporting_service_errors():
                if isinstance(serializer, ListSerializer):
                    instance = [self._create_ticket(serializer.child, data) for data in serializer.validated_data]
                else:
                    instance = self._create_ticket(serializer, serializer.validated_data)
                serializer.instance = instance
                self._validate_objects(instance)
        except ObjectDoesNotExist as error:
            raise PermissionDenied() from error

    def _create_ticket(self, serializer, data):
        """Create one ticket, then let the serializer apply the fields the service does not own.

        The service owns the ticket's own fields, its trail and its dedup behaviour. Custom fields
        and relationships belong to every Nautobot model and mean nothing to the service, so the
        serializer applies those afterwards - except on a dedup join, where the ticket is somebody
        else's and this payload has no business rewriting it.
        """
        ticket = ticket_service.create_ticket_for_user(
            user=self.request.user,
            pk=data.get("id"),
            title=data.get("title"),
            event_type=data.get("event_type"),
            severity=data.get("severity"),
            description=data.get("description", ""),
            dedup_key=data.get("dedup_key", ""),
            payload=data.get("payload"),
            assignee=data.get("assigned_to"),
            tags=data.get("tags"),
        )
        # Only what the service did not already write, and only where it differs: applying the rest
        # would cost a second save and a spurious "updated" entry in the change log.
        extra = {
            key: value
            for key, value in data.items()
            if key not in serializers.SERVICE_CREATE_FIELDS and getattr(ticket, key, None) != value
        }
        if extra and ticket.was_created:
            ticket = serializer.update(ticket, extra)
        return ticket

    def perform_update(self, serializer):
        """Apply the fields that carry a trail through the service, and the rest as usual.

        `severity` and `assigned_to` each have their own update type, so a PATCH that wrote them
        directly would change the ticket without recording who changed it or from what. The bulk
        PATCH route calls this once per object, so it is covered too.

        The stored ticket is re-read rather than trusting `serializer.instance`: Nautobot's
        `ValidatedModelSerializer.validate()` has already applied the incoming values to it, so the
        service would be comparing the new value against itself and would record nothing.
        """
        stored = EventTicket.objects.get(pk=serializer.instance.pk)
        data = serializer.validated_data
        with transaction.atomic():
            with _reporting_service_errors():
                if "severity" in data:
                    ticket_service.set_severity(
                        ticket=stored,
                        severity=data.pop("severity"),
                        source=TicketSourceChoices.HUMAN,
                        user=self.request.user,
                    )
                if "assigned_to" in data:
                    ticket_service.assign(
                        ticket=stored,
                        assignee=data.pop("assigned_to"),
                        source=TicketSourceChoices.HUMAN,
                        user=self.request.user,
                    )
            super().perform_update(serializer)

    @action(detail=True, methods=["get", "post"], url_path="transition")
    def transition(self, request, pk=None):  # pylint: disable=unused-argument
        """GET returns the currently allowed transitions; POST performs one.

        Exposing the allowed set means a client can drive its own UI without keeping a copy of the
        workflow graph.
        """
        ticket = self.get_object()

        if request.method == "GET":
            return Response(
                {
                    "status": ticket.status,
                    "allowed_transitions": sorted(ticket_service.get_allowed_transitions(ticket)),
                }
            )

        payload = serializers.TicketTransitionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        with _reporting_service_errors():
            ticket_service.transition(
                ticket=ticket,
                to_status=payload.validated_data["to_status"],
                source=TicketSourceChoices.HUMAN,
                user=request.user,
                message=payload.validated_data.get("message", ""),
                resolution=payload.validated_data.get("resolution", ""),
            )

        ticket.refresh_from_db()
        return Response(self.get_serializer(ticket).data)

    @action(detail=True, methods=["post"], url_path="comment")
    def comment(self, request, pk=None):  # pylint: disable=unused-argument
        """Append a comment to the ticket."""
        ticket = self.get_object()
        payload = serializers.TicketCommentSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        with _reporting_service_errors():
            update = ticket_service.add_comment(
                ticket=ticket,
                message=payload.validated_data["message"],
                source=TicketSourceChoices.HUMAN,
                user=request.user,
            )

        return self._update_response(update)

    @action(detail=True, methods=["post"], url_path="attach")
    def attach(self, request, pk=None):  # pylint: disable=unused-argument
        """Attach a Nautobot object to the ticket."""
        return self._attachment_action(request, ticket_service.attach_object)

    @action(detail=True, methods=["post"], url_path="detach")
    def detach(self, request, pk=None):  # pylint: disable=unused-argument
        """Detach a Nautobot object from the ticket."""
        return self._attachment_action(request, ticket_service.detach_object)

    def _attachment_action(self, request, service_function):
        ticket = self.get_object()
        payload = serializers.TicketObjectSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        with _reporting_service_errors():
            update = service_function(
                ticket=ticket,
                obj=ticket_service.resolve_object(
                    payload.validated_data["object_type"],
                    payload.validated_data["object_id"],
                ),
                source=TicketSourceChoices.HUMAN,
                user=request.user,
            )

        return self._update_response(update)

    def _update_response(self, update):
        """Render the update an action wrote, or report that it was a no-op."""
        if update is None:
            return Response(status=http_status.HTTP_204_NO_CONTENT)
        return Response(
            serializers.TicketUpdateSerializer(update, context=self.get_serializer_context()).data,
            status=http_status.HTTP_201_CREATED,
        )


class AgentRunViewSet(RecordViewSet):  # pylint: disable=too-many-ancestors
    """AgentRun viewset. The rows are the agent service's record of what one run did."""

    queryset = AgentRun.objects.select_related("ticket", "started_by", "job_result")
    serializer_class = serializers.AgentRunSerializer
    filterset_class = filters.AgentRunFilterSet


class AgentToolCallPermissions(TokenPermissions):
    """Permissions for the approve and deny actions.

    `approve_agenttoolcall`, which `change_agenttoolcall` does not imply (7.3). The stock map wants
    `add_` for any POST, because a POST normally creates something; these actions post to an
    existing proposal to decide it.
    """

    perms_map = {
        **TokenPermissions.perms_map,
        "POST": ["%(app_label)s.approve_%(model_name)s"],
    }


class AgentToolCallViewSet(RecordViewSet):  # pylint: disable=too-many-ancestors
    """AgentToolCall viewset: read-only, plus the two decisions.

    POST is allowed only for the approve and deny routes. There is no create route on a read-only
    viewset, so a POST to the list URL is still a 405, and no route anywhere writes `status`
    directly - which is what makes the gate a gate rather than a field.
    """

    queryset = AgentToolCall.objects.select_related("run__ticket", "tool__server", "decided_by")
    serializer_class = serializers.AgentToolCallSerializer
    filterset_class = filters.AgentToolCallFilterSet
    http_method_names = ["get", "head", "options", "post"]

    def restrict_queryset(self, request, *args, **kwargs):
        """Restrict by `view` for the decision actions rather than by the POST-derived `add`.

        The stock implementation derives the object permission from the HTTP method, so a POST
        would restrict the queryset to calls this user may *add* - which matches nothing, and the
        action 404s on a proposal that plainly exists. The model-level check that matters is
        `AgentToolCallPermissions` above, and DRF has already made it.
        """
        if self.action in ("approve", "deny") and request.user.is_authenticated:
            self.queryset = self.queryset.restrict(request.user, "view")
        else:
            super().restrict_queryset(request, *args, **kwargs)

    def get_permissions(self):
        """The decision actions need the decision permission."""
        if self.action in ("approve", "deny"):
            return [AgentToolCallPermissions()]
        return super().get_permissions()

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):  # pylint: disable=unused-argument
        """Approve this proposal, exactly as it stands (7.3).

        The resumption is not enqueued here. A REST client that approves a call is not necessarily
        sitting in front of a page waiting for it, and running the agent is a Job with its own
        endpoint and its own permission - so approving records the decision, and running the agent
        runs the agent.
        """
        return self._decide(request, agent_service.approve_tool_call)

    @action(detail=True, methods=["post"], url_path="deny")
    def deny(self, request, pk=None):  # pylint: disable=unused-argument
        """Deny this proposal, which ends the run (13.8)."""
        return self._decide(request, agent_service.deny_tool_call)

    def _decide(self, request, service_function):
        """One decision, through the service, mapped onto its HTTP meaning."""
        tool_call = self.get_object()
        with _reporting_service_errors():
            tool_call = service_function(tool_call=tool_call, user=request.user)
        return Response(self.get_serializer(tool_call).data)


class TicketEmbeddingViewSet(RecordViewSet):  # pylint: disable=too-many-ancestors
    """TicketEmbedding viewset. The rows are the retrieval corpus, written by services/rag.py."""

    queryset = TicketEmbedding.objects.select_related("ticket", "model__provider")
    serializer_class = serializers.TicketEmbeddingSerializer
    filterset_class = filters.TicketEmbeddingFilterSet

    def get_queryset(self):
        """Only embeddings of tickets this user may read.

        `document` is a verbatim copy of its ticket - title, description, resolution and every
        human comment - so without this the corpus is a way around ticket permissions: hold
        `view_ticketembedding` and an ObjectPermission constraint on `EventTicket` stops applying,
        because nothing carries it across the relation. Rule R6 is enforced in the panel and has to
        be enforced here too, or it is not enforced.
        """
        return rag_service.visible_embeddings(super().get_queryset(), self.request.user)
