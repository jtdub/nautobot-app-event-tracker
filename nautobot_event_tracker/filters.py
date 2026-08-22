"""Filtering for nautobot_event_tracker."""

import django_filters
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from nautobot.apps.filters import (
    ContentTypeMultipleChoiceFilter,
    MultiValueCharFilter,
    MultiValueDateTimeFilter,
    MultiValueNumberFilter,
    NaturalKeyOrPKMultipleChoiceFilter,
    NautobotFilterSet,
    RelatedMembershipBooleanFilter,
    SearchFilter,
)

from nautobot_event_tracker.choices import TERMINAL_STATUSES
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
from nautobot_event_tracker.services import tickets as ticket_service


class EventTypeFilterSet(NautobotFilterSet):
    """Filter for EventType."""

    q = SearchFilter(filter_predicates={"name": "icontains", "description": "icontains"})
    default_severity = MultiValueCharFilter(label="Default severity")

    class Meta:
        """Meta attributes for filter."""

        # Explicit rather than "__all__": the filter surface is specified deliberately, and
        # "__all__" would expose fields the spec does not.
        model = EventType
        fields = ["name", "description", "default_severity", "enabled"]  # pylint: disable=nb-use-fields-all


class EventTicketFilterSet(NautobotFilterSet):
    """Filter for EventTicket."""

    q = SearchFilter(
        filter_predicates={
            "title": "icontains",
            "description": "icontains",
            "resolution": "icontains",
            "event_type__name": "icontains",
        }
    )
    status = MultiValueCharFilter(label="Status")
    severity = MultiValueCharFilter(label="Severity")
    source = MultiValueCharFilter(label="Source")
    event_type = NaturalKeyOrPKMultipleChoiceFilter(
        queryset=EventType.objects.all(),
        to_field_name="name",
        label="Event type (name or ID)",
    )
    assigned_to = NaturalKeyOrPKMultipleChoiceFilter(
        queryset=get_user_model().objects.all(),
        to_field_name="username",
        label="Assigned user (username or ID)",
    )
    has_assignee = RelatedMembershipBooleanFilter(
        field_name="assigned_to",
        label="Has an assignee",
    )
    dedup_key = MultiValueCharFilter(label="Dedup key")
    event_count = MultiValueNumberFilter(label="Event count")
    first_seen = MultiValueDateTimeFilter(label="First seen")
    last_seen = MultiValueDateTimeFilter(label="Last seen")
    resolved_at = MultiValueDateTimeFilter(label="Resolved at")
    closed_at = MultiValueDateTimeFilter(label="Closed at")
    is_open = django_filters.BooleanFilter(
        method="filter_is_open",
        label="Is open (neither resolved nor closed)",
    )
    related_object_type = MultiValueCharFilter(
        method="filter_related_object_type",
        label="Attached object type (app_label.model)",
    )

    class Meta:
        """Meta attributes for filter."""

        model = EventTicket
        # Explicit rather than "__all__": "__all__" would auto-filter the raw JSON payload and the
        # service-owned timestamps, which the spec does not expose.
        fields = [  # pylint: disable=nb-use-fields-all
            "title",
            "status",
            "severity",
            "source",
            "dedup_key",
            "event_count",
            "tags",
        ]

    def filter_is_open(self, queryset, name, value):  # pylint: disable=unused-argument
        """Open means the status is not one of the terminal statuses.

        Takes its definition from the same constant the service layer uses, so the filter cannot
        drift from the workflow graph.
        """
        if value is None:
            return queryset
        if value:
            return queryset.exclude(status__in=TERMINAL_STATUSES)
        return queryset.filter(status__in=TERMINAL_STATUSES)

    def filter_related_object_type(self, queryset, name, value):  # pylint: disable=unused-argument
        """Match tickets that *currently* have an object of one of these types attached.

        Attachment is derived from the update trail (spec section 3.4), so this cannot be a plain
        join: an object attached and later detached must not match. The derivation itself belongs
        to the service layer, so this filter only supplies the scope and consumes the result.
        """
        if not value:
            return queryset

        content_types = ticket_service.content_types_from_labels(value)
        if not content_types:
            return queryset.none()

        ticket_ids = ticket_service.ticket_ids_with_attached_types(queryset, content_types)
        return queryset.filter(pk__in=ticket_ids)


class TicketUpdateFilterSet(NautobotFilterSet):
    """Filter for TicketUpdate."""

    q = SearchFilter(filter_predicates={"message": "icontains"})
    ticket = django_filters.ModelMultipleChoiceFilter(
        queryset=EventTicket.objects.all(),
        label="Ticket",
    )
    update_type = MultiValueCharFilter(label="Update type")
    source = MultiValueCharFilter(label="Source")
    user = NaturalKeyOrPKMultipleChoiceFilter(
        queryset=get_user_model().objects.all(),
        to_field_name="username",
        label="User (username or ID)",
    )
    created = MultiValueDateTimeFilter(label="Created")
    # A plain FK to ContentType, so core's filter handles `app_label.model` without a method.
    related_object_type = ContentTypeMultipleChoiceFilter(
        field_name="related_object_type",
        choices=lambda: [
            (ticket_service.content_type_label(content_type), ticket_service.content_type_label(content_type))
            for content_type in ContentType.objects.order_by("app_label", "model")
        ],
        conjoined=False,
    )

    class Meta:
        """Meta attributes for filter."""

        model = TicketUpdate
        fields = ["ticket", "update_type", "source", "message"]  # pylint: disable=nb-use-fields-all


class IngestionStatsFilterSet(NautobotFilterSet):
    """Filter for IngestionStats.

    Two questions the page exists to answer: what is one consumer doing, and what is happening on
    one topic. Everything else is a matter of reading the newest rows, which is the default order.
    """

    q = SearchFilter(filter_predicates={"consumer_name": "icontains", "topic": "icontains"})
    consumer_name = MultiValueCharFilter(label="Consumer name")
    topic = MultiValueCharFilter(label="Topic")
    bucket_start = MultiValueDateTimeFilter(label="Bucket start")

    class Meta:
        """Meta attributes for filter."""

        model = IngestionStats
        fields = ["consumer_name", "topic", "bucket_start"]  # pylint: disable=nb-use-fields-all


class LLMProviderFilterSet(NautobotFilterSet):
    """Filter for LLMProvider."""

    q = SearchFilter(filter_predicates={"name": "icontains", "description": "icontains"})
    provider_type = MultiValueCharFilter(label="Provider type")

    class Meta:
        """Meta attributes for filter."""

        # Explicit rather than "__all__": the filter surface is specified deliberately.
        model = LLMProvider
        # `tags` because a PrimaryModel is taggable and Nautobot's generic filter suite expects
        # the filter to exist, as EventTicket's does.
        fields = ["name", "description", "provider_type", "enabled", "tags"]  # pylint: disable=nb-use-fields-all


class LLMModelFilterSet(NautobotFilterSet):
    """Filter for LLMModel."""

    q = SearchFilter(filter_predicates={"name": "icontains", "description": "icontains", "provider__name": "icontains"})
    provider = NaturalKeyOrPKMultipleChoiceFilter(
        queryset=LLMProvider.objects.all(),
        to_field_name="name",
        label="Provider (name or ID)",
    )

    class Meta:
        """Meta attributes for filter."""

        model = LLMModel
        fields = ["provider", "name", "description", "enabled", "tags"]  # pylint: disable=nb-use-fields-all


class LLMUsageRecordFilterSet(NautobotFilterSet):
    """Filter for LLMUsageRecord.

    The questions the page exists to answer: what did one model or ticket cost, and what failed.
    """

    q = SearchFilter(filter_predicates={"model__name": "icontains", "purpose": "icontains", "error": "icontains"})
    model = django_filters.ModelMultipleChoiceFilter(
        queryset=LLMModel.objects.all(),
        label="Model",
    )
    ticket = django_filters.ModelMultipleChoiceFilter(
        queryset=EventTicket.objects.all(),
        label="Ticket",
    )
    purpose = MultiValueCharFilter(label="Purpose")
    called_at = MultiValueDateTimeFilter(label="Called at")

    class Meta:
        """Meta attributes for filter."""

        model = LLMUsageRecord
        fields = ["model", "ticket", "purpose", "success"]  # pylint: disable=nb-use-fields-all


class MCPServerFilterSet(NautobotFilterSet):
    """Filter for MCPServer."""

    q = SearchFilter(filter_predicates={"name": "icontains", "description": "icontains"})

    class Meta:
        """Meta attributes for filter."""

        model = MCPServer
        fields = ["name", "description", "enabled", "tags"]  # pylint: disable=nb-use-fields-all


class MCPToolFilterSet(NautobotFilterSet):
    """Filter for MCPTool.

    The questions the page exists to answer: what is enabled, what is mutating, and what a
    particular server offers. `enabled` and `mutating` are the two an operator reviews by.
    """

    q = SearchFilter(filter_predicates={"name": "icontains", "description": "icontains", "server__name": "icontains"})
    server = NaturalKeyOrPKMultipleChoiceFilter(
        queryset=MCPServer.objects.all(),
        to_field_name="name",
        label="Server (name or ID)",
    )

    class Meta:
        """Meta attributes for filter."""

        model = MCPTool
        fields = ["server", "name", "description", "enabled", "mutating", "tags"]  # pylint: disable=nb-use-fields-all
