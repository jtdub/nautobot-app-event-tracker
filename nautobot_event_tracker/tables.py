"""Tables for nautobot_event_tracker."""

import django_tables2 as tables
from nautobot.apps.tables import BaseTable, BooleanColumn, ButtonsColumn, LinkedCountColumn, ToggleColumn

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

#: The counters, in the order they read best: what arrived, what became of it, and when the last
#: message was. Shared by the list table and the detail panel so the two cannot drift apart.
INGESTION_STATS_COUNTER_FIELDS = (
    "received",
    "tickets_opened",
    "tickets_joined",
    "suppressed",
    "dropped",
    "errored",
    # The triage counters read after the outcomes they explain: `triaged` is what the model was
    # paid to judge, and the other two are what came of it.
    "triaged",
    "triage_attached",
    "triage_errors",
    # The enrichment counters read last of the three groups: what was attached to those outcomes,
    # and what the rules could not find.
    "enriched",
    "enrichment_misses",
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


#: The usage record's accounting fields, in the order they read best: what was called, what it
#: consumed, and what came of it. Shared by the list table and the ticket detail panel so the two
#: cannot drift apart.
LLM_USAGE_FIELDS = (
    "called_at",
    "model",
    "purpose",
    "ticket",
    "prompt_tokens",
    "completion_tokens",
    "cost",
    "latency_ms",
    "success",
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


class LLMProviderTable(BaseTable):
    # pylint: disable=R0903
    """Table for the LLMProvider list view."""

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    external_integration = tables.Column(linkify=True)
    model_count = LinkedCountColumn(
        viewname="plugins:nautobot_event_tracker:llmmodel_list",
        url_params={"provider": "pk"},
        verbose_name="Models",
    )
    actions = ButtonsColumn(LLMProvider, pk_field="pk")

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = LLMProvider
        fields = (
            "pk",
            "name",
            "description",
            "provider_type",
            "external_integration",
            "enabled",
            "model_count",
            "actions",
        )
        default_columns = fields


class LLMModelTable(BaseTable):
    # pylint: disable=R0903
    """Table for the LLMModel list view."""

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    provider = tables.Column(linkify=True)
    input_cost_per_million = tables.Column(verbose_name="Input $/1M")
    output_cost_per_million = tables.Column(verbose_name="Output $/1M")
    actions = ButtonsColumn(LLMModel, pk_field="pk")

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = LLMModel
        fields = (
            "pk",
            "name",
            "provider",
            "description",
            "enabled",
            "input_cost_per_million",
            "output_cost_per_million",
            "max_output_tokens",
            "actions",
        )
        default_columns = fields


class LLMUsageRecordTable(BaseTable):
    # pylint: disable=R0903
    """Table for the LLM usage list view and the ticket detail panel.

    No ToggleColumn and no ButtonsColumn: there is no bulk action and no per-row action, because
    nothing outside the service layer writes these rows.
    """

    called_at = tables.DateTimeColumn(linkify=True, verbose_name="When")
    model = tables.Column(linkify=True)
    ticket = tables.Column(linkify=True, default="—")
    success = BooleanColumn()

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = LLMUsageRecord
        fields = LLM_USAGE_FIELDS
        default_columns = fields


class MCPServerTable(BaseTable):
    # pylint: disable=R0903
    """Table for the MCPServer list view."""

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    external_integration = tables.Column(linkify=True)
    tool_count = LinkedCountColumn(
        viewname="plugins:nautobot_event_tracker:mcptool_list",
        url_params={"server": "pk"},
        verbose_name="Tools",
    )
    # A plain column rather than a second LinkedCountColumn: two of those on one relation collide
    # in django-tables2's lookup cache ("already seen with a different queryset"). The count comes
    # from an annotation on the viewset's queryset, which is one subquery either way.
    enabled_tool_count = tables.Column(verbose_name="Enabled", orderable=False)
    actions = ButtonsColumn(MCPServer, pk_field="pk")

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = MCPServer
        # Both counts, because the gap between them is the thing worth seeing: a server offering
        # forty tools of which two are enabled is the default-deny rule working, not a fault.
        fields = (
            "pk",
            "name",
            "description",
            "external_integration",
            "enabled",
            "tool_count",
            "enabled_tool_count",
            "last_discovered_at",
            "actions",
        )
        default_columns = fields


class MCPToolTable(BaseTable):
    # pylint: disable=R0903
    """Table for the MCPTool list view and the server detail panel.

    This is the table an operator reviews a newly discovered server in, so bulk selection is the
    point of it: ADR 0007 admitted that onboarding is tedious in proportion to tool count, and
    bulk enable is the only place that is answerable.
    """

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    server = tables.Column(linkify=True)
    enabled = BooleanColumn()
    mutating = BooleanColumn(verbose_name="Mutating")
    # Shown beside the operator's own classification, never instead of it: a reviewer comparing
    # the two columns is exactly the comparison this app refuses to make on their behalf.
    advertised_read_only = BooleanColumn(verbose_name="Server Claims Read-Only")
    actions = ButtonsColumn(MCPTool, pk_field="pk")

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = MCPTool
        fields = (
            "pk",
            "name",
            "server",
            "description",
            "enabled",
            "mutating",
            "advertised_read_only",
            "last_seen_at",
            "actions",
        )
        default_columns = fields
