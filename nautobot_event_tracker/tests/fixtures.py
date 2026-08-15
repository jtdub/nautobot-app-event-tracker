"""Create fixtures for tests."""

from nautobot_event_tracker.models import EventTrackerExampleModel


def create_eventtrackerexamplemodel():
    """Fixture to create necessary number of EventTrackerExampleModel for tests."""
    EventTrackerExampleModel.objects.create(name="Test One")
    EventTrackerExampleModel.objects.create(name="Test Two")
    EventTrackerExampleModel.objects.create(name="Test Three")
