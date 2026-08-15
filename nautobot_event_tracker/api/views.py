"""API views for nautobot_event_tracker."""

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from nautobot.apps.api import NautobotModelViewSet, ReadOnlyModelViewSet
from nautobot.core.api.authentication import TokenPermissions
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from nautobot_event_tracker import filters
from nautobot_event_tracker.api import serializers
from nautobot_event_tracker.choices import TicketSourceChoices
from nautobot_event_tracker.models import EventTicket, EventType, TicketUpdate
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import (
    InvalidActorError,
    InvalidTransitionError,
    TicketImmutableError,
)

TRANSITION_PERMISSION = "nautobot_event_tracker.transition_eventticket"


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

    Requires only `view` at the DRF layer; the action itself then requires
    `transition_eventticket`. Transitioning deliberately does not imply `change`, so that an
    operator can move tickets through the workflow without being able to rewrite their content.
    """

    perms_map = {
        **TokenPermissions.perms_map,
        "POST": ["%(app_label)s.view_%(model_name)s"],
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
        action_to_method = {
            "transition": "view",
            "comment": "change",
            "attach": "change",
            "detach": "change",
        }
        if request.user.is_authenticated and self.action in action_to_method:
            self.queryset = self.queryset.restrict(request.user, action_to_method[self.action])
        else:
            super().restrict_queryset(request, *args, **kwargs)

    def get_permissions(self):
        """Custom actions post to an existing ticket, so the stock add/change map does not fit."""
        if self.action == "transition":
            return [TicketTransitionPermissions()]
        if self.action in ("comment", "attach", "detach"):
            return [TicketChangeActionPermissions()]
        return super().get_permissions()

    def perform_create(self, serializer):
        """Create through the service layer so the ticket gets its trail and dedup behaviour."""
        data = dict(serializer.validated_data)
        serializer.instance = ticket_service.create_ticket_for_user(
            user=self.request.user,
            title=data.get("title"),
            event_type=data.get("event_type"),
            severity=data.get("severity"),
            description=data.get("description", ""),
            dedup_key=data.get("dedup_key", ""),
            payload=data.get("payload"),
            assignee=data.get("assigned_to"),
            tags=data.get("tags"),
        )

    def _require_transition_permission(self):
        if not self.request.user.has_perm(TRANSITION_PERMISSION):
            raise PermissionDenied("You do not have permission to transition event tickets.")

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

        self._require_transition_permission()
        payload = serializers.TicketTransitionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            ticket_service.transition(
                ticket=ticket,
                to_status=payload.validated_data["to_status"],
                source=TicketSourceChoices.HUMAN,
                user=request.user,
                message=payload.validated_data.get("message", ""),
                resolution=payload.validated_data.get("resolution", ""),
            )
        except (InvalidTransitionError, TicketImmutableError) as error:
            return Response({"detail": str(error)}, status=http_status.HTTP_409_CONFLICT)
        except (InvalidActorError, DjangoValidationError) as error:
            raise ValidationError(getattr(error, "messages", [str(error)])) from error

        ticket.refresh_from_db()
        return Response(self.get_serializer(ticket).data)

    @action(detail=True, methods=["post"], url_path="comment")
    def comment(self, request, pk=None):  # pylint: disable=unused-argument
        """Append a comment to the ticket."""
        ticket = self.get_object()
        payload = serializers.TicketCommentSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            update = ticket_service.add_comment(
                ticket=ticket,
                message=payload.validated_data["message"],
                source=TicketSourceChoices.HUMAN,
                user=request.user,
            )
        except TicketImmutableError as error:
            return Response({"detail": str(error)}, status=http_status.HTTP_409_CONFLICT)
        except (InvalidActorError, DjangoValidationError) as error:
            raise ValidationError(getattr(error, "messages", [str(error)])) from error

        return Response(
            serializers.TicketUpdateSerializer(update, context=self.get_serializer_context()).data,
            status=http_status.HTTP_201_CREATED,
        )

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

        obj = self._resolve_object(
            payload.validated_data["object_type"],
            payload.validated_data["object_id"],
        )

        try:
            update = service_function(
                ticket=ticket,
                obj=obj,
                source=TicketSourceChoices.HUMAN,
                user=request.user,
            )
        except TicketImmutableError as error:
            return Response({"detail": str(error)}, status=http_status.HTTP_409_CONFLICT)
        except (InvalidActorError, DjangoValidationError) as error:
            raise ValidationError(getattr(error, "messages", [str(error)])) from error

        if update is None:
            return Response(status=http_status.HTTP_204_NO_CONTENT)
        return Response(
            serializers.TicketUpdateSerializer(update, context=self.get_serializer_context()).data,
            status=http_status.HTTP_201_CREATED,
        )

    @staticmethod
    def _resolve_object(object_type, object_id):
        """Turn an 'app_label.model' string and a UUID into a model instance."""
        app_label, _, model = str(object_type).partition(".")
        try:
            content_type = ContentType.objects.get(app_label=app_label, model=model)
        except ContentType.DoesNotExist as error:
            raise ValidationError({"object_type": f"Unknown object type '{object_type}'."}) from error

        model_class = content_type.model_class()
        if model_class is None:
            raise ValidationError({"object_type": f"Object type '{object_type}' has no model."})

        try:
            return model_class.objects.get(pk=object_id)
        except (ObjectDoesNotExist, ValueError, TypeError) as error:
            raise ValidationError({"object_id": f"No {object_type} with ID {object_id}."}) from error
