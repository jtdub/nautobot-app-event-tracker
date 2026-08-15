"""Filtering for nautobot_event_tracker."""

import django_filters
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from nautobot.apps.filters import (
    MultiValueCharFilter,
    MultiValueDateTimeFilter,
    MultiValueNumberFilter,
    NaturalKeyOrPKMultipleChoiceFilter,
    NautobotFilterSet,
    RelatedMembershipBooleanFilter,
    SearchFilter,
)

from nautobot_event_tracker.choices import TERMINAL_STATUSES, UpdateTypeChoices
from nautobot_event_tracker.models import EventTicket, EventType, TicketUpdate

ATTACHMENT_UPDATE_TYPES = (UpdateTypeChoices.OBJECT_ATTACHED, UpdateTypeChoices.OBJECT_DETACHED)


def _content_type_ids(labels):
    """Turn `app_label.model` strings into ContentType primary keys, skipping unknown ones."""
    ids = []
    for label in labels:
        app_label, _, model = str(label).lower().partition(".")
        ids.extend(ContentType.objects.filter(app_label=app_label, model=model).values_list("pk", flat=True))
    return ids


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
        join: an object that was attached and later detached must not match. Replay the attach and
        detach rows in order and keep the tickets left holding at least one attachment.
        """
        if not value:
            return queryset

        content_type_ids = _content_type_ids(value)
        if not content_type_ids:
            return queryset.none()

        rows = (
            TicketUpdate.objects.filter(
                update_type__in=ATTACHMENT_UPDATE_TYPES,
                related_object_type__in=content_type_ids,
            )
            .order_by("created")
            .values_list("ticket_id", "related_object_type_id", "related_object_id", "update_type")
        )

        attached = set()
        for ticket_id, content_type_id, object_id, update_type in rows:
            key = (ticket_id, content_type_id, object_id)
            if update_type == UpdateTypeChoices.OBJECT_ATTACHED:
                attached.add(key)
            else:
                attached.discard(key)

        return queryset.filter(pk__in={key[0] for key in attached})


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
    related_object_type = MultiValueCharFilter(
        method="filter_related_object_type",
        label="Related object type (app_label.model)",
    )

    class Meta:
        """Meta attributes for filter."""

        model = TicketUpdate
        fields = ["ticket", "update_type", "source", "message"]  # pylint: disable=nb-use-fields-all

    def filter_related_object_type(self, queryset, name, value):  # pylint: disable=unused-argument
        """Filter by `app_label.model`, matching the convention core filtersets use."""
        if not value:
            return queryset
        content_type_ids = _content_type_ids(value)
        if not content_type_ids:
            return queryset.none()
        return queryset.filter(related_object_type__in=content_type_ids)
