"""API views for nautobot_event_tracker."""

from collections import namedtuple
from contextlib import contextmanager

from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from nautobot.apps.api import NautobotModelViewSet, ReadOnlyModelViewSet
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
    EventTicket,
    EventType,
    IngestionStats,
    LLMModel,
    LLMProvider,
    LLMUsageRecord,
    TicketUpdate,
)
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import (
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
    except (InvalidTransitionError, TicketImmutableError) as error:
        raise Conflict(str(error)) from error
    except (InvalidActorError, DjangoValidationError) as error:
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


class TicketUpdateViewSet(ReadOnlyModelViewSet):  # pylint: disable=too-many-ancestors
    """TicketUpdate viewset.

    Read-only by construction: updates are append-only, so there is no create, update or delete
    route to offer. New updates appear as a side effect of ticket actions.
    """

    queryset = TicketUpdate.objects.select_related("ticket", "user", "related_object_type")
    serializer_class = serializers.TicketUpdateSerializer
    filterset_class = filters.TicketUpdateFilterSet
    http_method_names = ["get", "head", "options"]


class IngestionStatsViewSet(ReadOnlyModelViewSet):  # pylint: disable=too-many-ancestors
    """IngestionStats viewset.

    Read-only by construction, like TicketUpdate and for the same reason: the rows are written by
    one process, as a record of what it saw, and there is nothing for a client to change.
    """

    queryset = IngestionStats.objects.all()
    serializer_class = serializers.IngestionStatsSerializer
    filterset_class = filters.IngestionStatsFilterSet
    http_method_names = ["get", "head", "options"]


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


class LLMUsageRecordViewSet(ReadOnlyModelViewSet):  # pylint: disable=too-many-ancestors
    """LLMUsageRecord viewset.

    Read-only by construction, like TicketUpdate and for the same reason: the rows are the service
    layer's accounting of calls it made (rule L1), and there is nothing for a client to change.
    """

    queryset = LLMUsageRecord.objects.select_related("model__provider", "ticket")
    serializer_class = serializers.LLMUsageRecordSerializer
    filterset_class = filters.LLMUsageRecordFilterSet
    http_method_names = ["get", "head", "options"]


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
