"""GraphQL types for nautobot_event_tracker.

`EventType`, `EventTicket`, `LLMProvider` and `LLMModel` are exposed automatically through the
`graphql` entry in their `extras_features`. `TicketUpdate`, `IngestionStats` and `LLMUsageRecord`
need explicit types because none of them is a PrimaryModel.
"""

from nautobot.apps.graphql import OptimizedNautobotObjectType

from nautobot_event_tracker.filters import IngestionStatsFilterSet, LLMUsageRecordFilterSet, TicketUpdateFilterSet
from nautobot_event_tracker.models import IngestionStats, LLMUsageRecord, TicketUpdate


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


graphql_types = [TicketUpdateType, IngestionStatsType, LLMUsageRecordType]
