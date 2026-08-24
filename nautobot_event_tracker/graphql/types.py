"""GraphQL types for nautobot_event_tracker.

Nautobot offers two ways to put a model on the GraphQL schema, and a model takes exactly one of
them. `EventType`, `EventTicket`, `LLMProvider` and `LLMModel` take the first: the `graphql` entry
in their `extras_features`, which generates a type for them. `TicketUpdate`, `IngestionStats`,
`LLMUsageRecord`, `AgentRun` and `AgentToolCall` take the second - the explicit types below - and
so deliberately carry no `graphql` feature, which would register a second, competing type for the
same model. They need the explicit form because each wires a `filterset_class` of its own.

`TicketEmbedding` is on neither list, and that is deliberate rather than an omission. Its
`document` is a verbatim copy of its ticket, so reading it has to be gated on the *ticket's*
permissions - which the REST and UI viewsets do by intersecting with
`EventTicket.objects.restrict(user, "view")`. GraphQL has no equivalent hook: Nautobot restricts a
type's root queryset on that type's own model permission, and `OptimizedNautobotObjectType`
documents that overriding `get_queryset` is not the answer either, because in graphene-django
3.1.15+ it defeats the query optimizer and reintroduces FK N+1.

Excluding the field alone would not close it: a filterset with a `q` predicate over an
unrestricted-by-ticket queryset is a content oracle - it leaks by filtering without ever returning
the field. So the model is simply not queryable here. Everything about it that is safe to read -
which model indexed it, when, how wide the vector is - is on the REST API, properly restricted.
Rule R6 is enforced on every surface, or it is enforced on none.
"""

from nautobot.apps.graphql import OptimizedNautobotObjectType

from nautobot_event_tracker.filters import (
    AgentRunFilterSet,
    AgentToolCallFilterSet,
    IngestionStatsFilterSet,
    LLMUsageRecordFilterSet,
    TicketUpdateFilterSet,
)
from nautobot_event_tracker.models import (
    AgentRun,
    AgentToolCall,
    IngestionStats,
    LLMUsageRecord,
    TicketUpdate,
)


class TicketUpdateType(OptimizedNautobotObjectType):
    """GraphQL type for TicketUpdate.

    Query-only, like the rest of Nautobot's GraphQL surface, which suits a model the service layer
    owns exclusively.

    `related_object_type` is exposed as the ContentType relation rather than as a synthesized
    `app_label.model` string: graphene-django generates the relation from the model field and
    overrides a same-named scalar declared on the type, and the nested form is the more idiomatic
    GraphQL shape anyway. Query it as `related_object_type { app_label model }`.
    """

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta attributes."""

        model = TicketUpdate
        filterset_class = TicketUpdateFilterSet


class IngestionStatsType(OptimizedNautobotObjectType):
    """GraphQL type for IngestionStats.

    Query-only, like everything else here, and doubly so for a model only the consumer writes.
    """

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta attributes."""

        model = IngestionStats
        filterset_class = IngestionStatsFilterSet


class LLMUsageRecordType(OptimizedNautobotObjectType):
    """GraphQL type for LLMUsageRecord.

    Query-only, like everything else here, and doubly so for a model only the service layer writes.
    """

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta attributes."""

        model = LLMUsageRecord
        filterset_class = LLMUsageRecordFilterSet


class AgentRunType(OptimizedNautobotObjectType):
    """GraphQL type for AgentRun.

    Query-only, like everything else here, and for the same reason: a run is what happened, and
    `services/agent.py` is the only thing that writes one.
    """

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta attributes."""

        model = AgentRun
        filterset_class = AgentRunFilterSet


class AgentToolCallType(OptimizedNautobotObjectType):
    """GraphQL type for AgentToolCall.

    Query-only. Approving is a decision with its own permission and its own trail entry, so it
    happens through the REST action rather than through a mutation nobody would notice.
    """

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta attributes."""

        model = AgentToolCall
        filterset_class = AgentToolCallFilterSet


graphql_types = [
    TicketUpdateType,
    IngestionStatsType,
    LLMUsageRecordType,
    AgentRunType,
    AgentToolCallType,
]
