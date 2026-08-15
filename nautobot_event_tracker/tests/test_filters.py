"""Test EventTrackerExampleModel Filter."""

from nautobot.apps.testing import FilterTestCases

from nautobot_event_tracker import filters, models
from nautobot_event_tracker.tests import fixtures


class EventTrackerExampleModelFilterTestCase(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """EventTrackerExampleModel Filter Test Case."""

    queryset = models.EventTrackerExampleModel.objects.all()
    filterset = filters.EventTrackerExampleModelFilterSet
    generic_filter_tests = (
        ("id",),
        ("created",),
        ("last_updated",),
        ("name",),
    )

    @classmethod
    def setUpTestData(cls):
        """Setup test data for EventTrackerExampleModel Model."""
        fixtures.create_eventtrackerexamplemodel()

    def test_q_search_name(self):
        """Test using Q search with name of EventTrackerExampleModel."""
        params = {"q": "Test One"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_q_invalid(self):
        """Test using invalid Q search for EventTrackerExampleModel."""
        params = {"q": "test-five"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 0)
