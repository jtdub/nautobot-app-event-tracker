"""API serializers for nautobot_event_tracker."""

from nautobot.apps.api import BaseModelSerializer, ContentTypeField, NautobotModelSerializer, TaggedModelSerializerMixin
from rest_framework import serializers

from nautobot_event_tracker.models import EventTicket, EventType, TicketUpdate

#: Fields the service layer owns. A write to any of them through the generic endpoints is rejected
#: rather than silently dropped, so a client never gets a 200 for a change that did not happen.
SERVICE_OWNED_FIELDS = ("status", "resolved_at", "closed_at", "resolution", "event_count")

#: The create-payload fields `services.tickets.create_ticket_for_user()` consumes. Anything else a
#: create payload carries - custom fields, relationships - belongs to the serializer, which applies
#: it to the row the service wrote.
SERVICE_CREATE_FIELDS = (
    "id",
    "title",
    "event_type",
    "severity",
    "description",
    "dedup_key",
    "payload",
    "assigned_to",
    "tags",
)

#: Fields the service layer fills in at creation. Read-only so that the API does not demand them,
#: but not in SERVICE_OWNED_FIELDS: offering them is a mistake, not an attempt to bypass anything,
#: so they are quietly ignored rather than rejected.
SERVICE_ASSIGNED_FIELDS = ("source", "first_seen", "last_seen")


class EventTypeSerializer(NautobotModelSerializer):  # pylint: disable=too-many-ancestors
    """EventType Serializer."""

    class Meta:
        """Meta attributes."""

        model = EventType
        fields = "__all__"


class TicketUpdateSerializer(BaseModelSerializer):
    """TicketUpdate Serializer.

    Read-only in every field: updates are append-only and are written by the service layer alone.
    """

    related_object_type = ContentTypeField(read_only=True)

    class Meta:
        """Meta attributes."""

        model = TicketUpdate
        fields = [
            "id",
            "url",
            "ticket",
            "update_type",
            "source",
            "user",
            "message",
            "from_status",
            "to_status",
            "related_object_type",
            "related_object_id",
            "created",
        ]
        read_only_fields = fields


class EventTicketSerializer(NautobotModelSerializer, TaggedModelSerializerMixin):  # pylint: disable=too-many-ancestors
    """EventTicket Serializer."""

    class Meta:
        """Meta attributes."""

        model = EventTicket
        fields = "__all__"
        read_only_fields = SERVICE_OWNED_FIELDS + SERVICE_ASSIGNED_FIELDS

    def validate(self, attrs):
        """Reject writes to service-owned fields instead of dropping them silently.

        DRF ignores read-only fields, which would hand the client a 200 for a status change that
        never happened. Callers are pointed at the endpoint that does the job.
        """
        offered = [field for field in SERVICE_OWNED_FIELDS if field in (self.initial_data or {})]
        if offered:
            raise serializers.ValidationError(
                {
                    field: (
                        "This field is managed by the ticket service layer and cannot be set directly. "
                        "Use POST /tickets/{id}/transition/ to change ticket state."
                    )
                    for field in offered
                }
            )

        return super().validate(attrs)


class TicketTransitionSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Input for the transition action."""

    to_status = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True, default="")
    resolution = serializers.CharField(required=False, allow_blank=True, default="")


class TicketCommentSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Input for the comment action."""

    message = serializers.CharField()


class TicketObjectSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Input for the attach and detach actions."""

    object_type = serializers.CharField(help_text="An 'app_label.model' string, for example 'dcim.device'.")
    object_id = serializers.UUIDField()
