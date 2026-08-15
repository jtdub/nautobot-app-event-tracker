"""Filtering for nautobot_event_tracker."""

from nautobot.apps.filters import NameSearchFilterSet, NautobotFilterSet

from nautobot_event_tracker import models


class EventTrackerExampleModelFilterSet(NameSearchFilterSet, NautobotFilterSet):  # pylint: disable=too-many-ancestors
    """Filter for EventTrackerExampleModel."""

    class Meta:
        """Meta attributes for filter."""

        model = models.EventTrackerExampleModel

        # add any fields from the model that you would like to filter your searches by using those
        fields = "__all__"
