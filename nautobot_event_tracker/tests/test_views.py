"""Unit tests for views."""

from nautobot.apps.testing import ViewTestCases

from nautobot_event_tracker import models
from nautobot_event_tracker.tests import fixtures


class EventTrackerExampleModelViewTest(ViewTestCases.PrimaryObjectViewTestCase):
    # pylint: disable=too-many-ancestors
    """Test the EventTrackerExampleModel views."""

    model = models.EventTrackerExampleModel
    bulk_edit_data = {"description": "Bulk edit views"}
    form_data = {
        "name": "Test 1",
        "description": "Initial model",
    }

    update_data = {
        "name": "Test 2",
        "description": "Updated model",
    }

    @classmethod
    def setUpTestData(cls):
        fixtures.create_eventtrackerexamplemodel()
