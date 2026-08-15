"""API serializers for nautobot_event_tracker."""

from nautobot.apps.api import NautobotModelSerializer, TaggedModelSerializerMixin

from nautobot_event_tracker import models


class EventTrackerExampleModelSerializer(NautobotModelSerializer, TaggedModelSerializerMixin):  # pylint: disable=too-many-ancestors
    """EventTrackerExampleModel Serializer."""

    class Meta:
        """Meta attributes."""

        model = models.EventTrackerExampleModel
        fields = "__all__"

        # Option for disabling write for certain fields:
        # read_only_fields = []
