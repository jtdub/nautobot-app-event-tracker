"""Tables for nautobot_event_tracker."""

import django_tables2 as tables
from nautobot.apps.tables import BaseTable, BooleanColumn, ButtonsColumn, LinkedCountColumn, ToggleColumn

from nautobot_event_tracker.models import (
    AgentRun,
    AgentToolCall,
    EventTicket,
    EventType,
    IngestionStats,
    LLMUsageRecord,
    TicketEmbedding,
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


#: The run's own fields, in the order they read best: whose ticket, how it went, and what it cost.
#: Shared by the list table and the detail panel so the two cannot drift apart.
AGENT_RUN_FIELDS = (
    "started_at",
    "ticket",
    "status",
    "started_by",
    "iterations",
    "finished_at",
)

#: The same for a tool call: what was asked for, what was decided, and what came of it.
AGENT_TOOL_CALL_FIELDS = (
    "proposed_at",
    "run",
    "tool",
    "status",
    "decided_by",
    "decided_at",
    "latency_ms",
    "called_at",
)


class AgentRunTable(BaseTable):
    # pylint: disable=R0903
    """Table for the Agent Run list view and the ticket detail panel.

    No ToggleColumn and no ButtonsColumn: a run is a record of what happened, and nothing outside
    `services/agent.py` writes one.
    """

    started_at = tables.DateTimeColumn(linkify=True, verbose_name="Started")
    ticket = tables.Column(linkify=True)
    # Not linkified: Nautobot's User model has no absolute URL.
    started_by = tables.Column(verbose_name="Started By", default="—")
    tool_call_count = tables.Column(verbose_name="Tool Calls", orderable=False, default=0)

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = AgentRun
        fields = (*AGENT_RUN_FIELDS, "tool_call_count")
        default_columns = fields


class AgentToolCallTable(BaseTable):
    # pylint: disable=R0903
    """Table for the Agent Tool Call list view, the run panel and the ticket panel.

    The status column is the one that matters: `proposed` is somebody's decision waiting to be
    made, and `outcome_unknown` is a change that somebody has to check on a device.
    """

    proposed_at = tables.DateTimeColumn(linkify=True, verbose_name="Proposed")
    run = tables.Column(linkify=True)
    tool = tables.Column(linkify=True)
    # Not linkified: Nautobot's User model has no absolute URL.
    decided_by = tables.Column(verbose_name="Decided By", default="—")

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = AgentToolCall
        fields = AGENT_TOOL_CALL_FIELDS
        default_columns = fields


class TicketEmbeddingTable(BaseTable):
    # pylint: disable=R0903
    """Table for the Ticket Embedding list view.

    No ToggleColumn and no ButtonsColumn: nothing outside `services/rag.py` writes these rows, and
    there is no per-row action worth offering. The vector itself is deliberately not a column -
    768 floats is not something anybody reads.
    """

    indexed_at = tables.DateTimeColumn(linkify=True, verbose_name="Indexed")
    ticket = tables.Column(linkify=True)
    model = tables.Column(linkify=True)

    class Meta(BaseTable.Meta):
        """Meta attributes."""

        model = TicketEmbedding
        fields = ("indexed_at", "ticket", "model", "dimensions")
        default_columns = fields
