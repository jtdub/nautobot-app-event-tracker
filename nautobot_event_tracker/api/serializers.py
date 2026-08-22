"""API serializers for nautobot_event_tracker."""

from nautobot.apps.api import BaseModelSerializer, ContentTypeField, NautobotModelSerializer, TaggedModelSerializerMixin
from rest_framework import serializers

from nautobot_event_tracker.models import (
    EventTicket,
    EventType,
    IngestionStats,
    LLMModel,
    LLMProvider,
    LLMUsageRecord,
    MCPServer,
    MCPTool,
    TicketUpdate,
)

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


class IngestionStatsSerializer(BaseModelSerializer):
    """IngestionStats Serializer.

    Read-only in every field. These counters are a record of what a consumer saw; nothing outside
    the consumer has any business writing them.
    """

    class Meta:
        """Meta attributes."""

        model = IngestionStats
        fields = [
            "id",
            "url",
            # Nautobot's generic API tests expect every object to expose its natural slug, and a
            # client following one detail representation to another expects the same shape.
            "natural_slug",
            "consumer_name",
            "topic",
            "bucket_start",
            "received",
            "errored",
            "dropped",
            "tickets_opened",
            "tickets_joined",
            "suppressed",
            "triaged",
            "triage_attached",
            "triage_errors",
            "enriched",
            "enrichment_misses",
            "drops_by_reason",
            "last_message_at",
        ]
        read_only_fields = fields


class LLMProviderSerializer(NautobotModelSerializer):  # pylint: disable=too-many-ancestors
    """LLMProvider Serializer."""

    class Meta:
        """Meta attributes."""

        model = LLMProvider
        fields = "__all__"


class LLMModelSerializer(NautobotModelSerializer):  # pylint: disable=too-many-ancestors
    """LLMModel Serializer."""

    class Meta:
        """Meta attributes."""

        model = LLMModel
        fields = "__all__"


class LLMUsageRecordSerializer(BaseModelSerializer):
    """LLMUsageRecord Serializer.

    Read-only in every field. A usage record is the service layer's accounting of a call it made;
    nothing outside the service has any business writing one (rule L1).
    """

    class Meta:
        """Meta attributes."""

        model = LLMUsageRecord
        fields = [
            "id",
            "url",
            "natural_slug",
            "model",
            "ticket",
            "purpose",
            "request_id",
            "prompt_tokens",
            "completion_tokens",
            "cost",
            "latency_ms",
            "success",
            "error",
            "called_at",
        ]
        read_only_fields = fields


class MCPServerSerializer(NautobotModelSerializer):  # pylint: disable=too-many-ancestors
    """MCPServer Serializer."""

    class Meta:
        """Meta attributes."""

        model = MCPServer
        fields = "__all__"


class MCPToolSerializer(NautobotModelSerializer):  # pylint: disable=too-many-ancestors
    """MCPTool Serializer."""

    class Meta:
        """Meta attributes."""

        model = MCPTool
        fields = "__all__"
