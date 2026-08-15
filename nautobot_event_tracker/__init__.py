"""App declaration for nautobot_event_tracker."""

# Metadata is inherited from Nautobot. If not including Nautobot in the environment, this should be added
from importlib import metadata

from nautobot.apps import NautobotAppConfig

__version__ = metadata.version(__name__)


class EventTrackerConfig(NautobotAppConfig):
    """App configuration for the nautobot_event_tracker app."""

    name = "nautobot_event_tracker"
    verbose_name = "Event Tracker"
    version = __version__
    author = "James Williams"
    description = "Network event ticketing and AI-assisted network operations for Nautobot.."
    base_url = "event-tracker"
    required_settings = []
    default_settings = {}
    docs_view_name = "plugins:nautobot_event_tracker:docs"
    searchable_models = ["eventtrackerexamplemodel"]


config = EventTrackerConfig  # pylint:disable=invalid-name
