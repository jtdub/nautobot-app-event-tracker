"""Test EventTrackerExampleModel."""

from nautobot.apps.testing import ModelTestCases

from nautobot_event_tracker import models
from nautobot_event_tracker.tests import fixtures


class TestEventTrackerExampleModel(ModelTestCases.BaseModelTestCase):
    """Test EventTrackerExampleModel."""

    model = models.EventTrackerExampleModel

    @classmethod
    def setUpTestData(cls):
        """Create test data for EventTrackerExampleModel Model."""
        super().setUpTestData()
        # Create 3 objects for the model test cases.
        fixtures.create_eventtrackerexamplemodel()

    def test_create_eventtrackerexamplemodel_only_required(self):
        """Create with only required fields, and validate null description and __str__."""
        eventtrackerexamplemodel = models.EventTrackerExampleModel.objects.create(name="Development")
        self.assertEqual(eventtrackerexamplemodel.name, "Development")
        self.assertEqual(eventtrackerexamplemodel.description, "")
        self.assertEqual(str(eventtrackerexamplemodel), "Development")

    def test_create_eventtrackerexamplemodel_all_fields_success(self):
        """Create EventTrackerExampleModel with all fields."""
        eventtrackerexamplemodel = models.EventTrackerExampleModel.objects.create(name="Development", description="Development Test")
        self.assertEqual(eventtrackerexamplemodel.name, "Development")
        self.assertEqual(eventtrackerexamplemodel.description, "Development Test")
