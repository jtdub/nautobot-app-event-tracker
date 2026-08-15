"""GraphQL types for nautobot_event_tracker.

`EventType` and `EventTicket` are exposed automatically through the `graphql` entry in their
`extras_features`. `TicketUpdate` needs an explicit type because it is not a PrimaryModel.
"""

import graphene
from graphene_django import DjangoObjectType

from nautobot_event_tracker.filters import TicketUpdateFilterSet
from nautobot_event_tracker.models import TicketUpdate


class TicketUpdateType(DjangoObjectType):
    """GraphQL type for TicketUpdate.

    Query-only, like the rest of Nautobot's GraphQL surface, which suits a model the service layer
    owns exclusively.
    """

    related_object_type = graphene.String()

    class Meta:
        """Meta attributes."""

        model = TicketUpdate
        filterset_class = TicketUpdateFilterSet

    def resolve_related_object_type(self, info):  # pylint: disable=unused-argument
        """Return the related object type as an `app_label.model` string."""
        if self.related_object_type_id is None:
            return None
        content_type = self.related_object_type
        return f"{content_type.app_label}.{content_type.model}"


graphql_types = [TicketUpdateType]
