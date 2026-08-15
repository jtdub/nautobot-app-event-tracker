"""Tables for nautobot_event_tracker."""

import django_tables2 as tables
from nautobot.apps.tables import BaseTable, ButtonsColumn, LinkedCountColumn, ToggleColumn

from nautobot_event_tracker.models import EventTicket, EventType, IngestionStats, TicketUpdate

#: The counters, in the order they read best: what arrived, what became of it, and when the last
#: message was. Shared by the list table and the detail panel so the two cannot drift apart.
INGESTION_STATS_COUNTER_FIELDS = (
    "received",
    "tickets_opened",
    "tickets_joined",
    "suppressed",
    "dropped",
    "errored",
    "last_message_at",
)

#: The ticket's core fields, in the order they read best. Shared by the list table and the detail
#: panel so the two cannot drift into showing different things.
TICKET_CORE_FIELDS = (
    "title",
    "event_type",
    "status",
    "severity",
    "source",
    "assigned_to",
    "event_count",
    "first_seen",
    "last_seen",
    "resolved_at",
    "closed_at",
)


class EventTypeTable(BaseTable):
    # pylint: disable=R0903
    """Table for the EventType list view."""

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    # Counts the annotation the view applies, and links through to the filtered ticket list.
    ticket_count = LinkedCountColumn(
        viewname="plugins:nautobot_event_tracker:eventticket_list",
        url_params={"event_type": "pk"},
        verbose_name="Tickets",
    )
    actions = ButtonsColumn(EventType, pk_field="pk")

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = EventType
        fields = ("pk", "name", "description", "default_severity", "enabled", "ticket_count", "actions")
        default_columns = ("pk", "name", "description", "default_severity", "enabled", "actions")


class EventTicketTable(BaseTable):
    # pylint: disable=R0903
    """Table for the EventTicket list view."""

    pk = ToggleColumn()
    title = tables.Column(linkify=True)
    event_type = tables.Column(linkify=True)
    # Not linkified: Nautobot's User model has no absolute URL.
    assigned_to = tables.Column()
    actions = ButtonsColumn(EventTicket, pk_field="pk")

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = EventTicket
        fields = ("pk", *TICKET_CORE_FIELDS, "actions")
        default_columns = (
            "pk",
            "title",
            "event_type",
            "status",
            "severity",
            "assigned_to",
            "event_count",
            "last_seen",
            "actions",
        )


class TicketUpdateTable(BaseTable):
    # pylint: disable=R0903
    """Table for the ticket update trail.

    Deliberately has no `actions` column: updates are append-only, so there is no edit or delete
    control to offer.
    """

    created = tables.DateTimeColumn(verbose_name="When")
    update_type = tables.Column(verbose_name="Type")
    # Not linkified: Nautobot's User model has no absolute URL.
    user = tables.Column(verbose_name="User", default="—")
    related_object = tables.Column(linkify=True, verbose_name="Object", default="—", orderable=False)

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = TicketUpdate
        fields = ("created", "update_type", "source", "user", "message", "related_object")
        default_columns = ("created", "update_type", "source", "user", "message", "related_object")
        order_by = ("created",)


class IngestionStatsTable(BaseTable):
    # pylint: disable=R0903
    """Table for the IngestionStats list view.

    No ToggleColumn and no ButtonsColumn: there is no bulk action and no per-row action, because
    nothing outside the consumer writes these rows.
    """

    bucket_start = tables.DateTimeColumn(linkify=True, verbose_name="Window")

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = IngestionStats
        fields = ("bucket_start", "consumer_name", "topic", *INGESTION_STATS_COUNTER_FIELDS)
        default_columns = fields
