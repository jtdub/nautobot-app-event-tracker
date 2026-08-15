"""API views for nautobot_event_tracker."""

from nautobot.apps.api import NautobotModelViewSet

from nautobot_event_tracker import filters, models
from nautobot_event_tracker.api import serializers


class EventTrackerExampleModelViewSet(NautobotModelViewSet):  # pylint: disable=too-many-ancestors
    """EventTrackerExampleModel viewset."""

    queryset = models.EventTrackerExampleModel.objects.all()
    serializer_class = serializers.EventTrackerExampleModelSerializer
    filterset_class = filters.EventTrackerExampleModelFilterSet

    # Option for modifying the default HTTP methods:
    # http_method_names = ["get", "post", "put", "patch", "delete", "head", "options", "trace"]
