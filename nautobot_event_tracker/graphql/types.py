"""GraphQL types for nautobot_event_tracker.

`EventType` and `EventTicket` are exposed automatically through the `graphql` entry in their
`extras_features`. `TicketUpdate` needs an explicit type because it is not a PrimaryModel.
"""

from graphene_django import DjangoObjectType

from nautobot_event_tracker.filters import TicketUpdateFilterSet
from nautobot_event_tracker.models import TicketUpdate


class TicketUpdateType(DjangoObjectType):
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


graphql_types = [TicketUpdateType]
