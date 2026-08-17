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
    description = "Network event ticketing and AI-assisted network operations for Nautobot."
    base_url = "event-tracker"
    required_settings = []
    default_settings = {
        # Object types that may be attached to a ticket. Anything outside this list is rejected by
        # services.tickets.attach_object(). See the Phase 1 spec, section 3.5.
        "attachable_object_types": [
            "dcim.device",
            "dcim.interface",
            "dcim.cable",
            "dcim.location",
            "ipam.ipaddress",
            "ipam.prefix",
            "circuits.circuit",
        ],
        # Event ingestion. An empty block means the consumer has nothing to subscribe to, which is
        # the right default for an app installed before anyone has pointed it at a broker. Nautobot
        # merges PLUGINS_CONFIG over these defaults one top-level key at a time, so the defaults
        # for the keys inside this one are applied by `ingestion.config`, not here. See the Phase 2
        # spec, section 3.
        "ingestion": {},
        # LLM service settings. Defaults for the keys inside this one are applied by
        # `services.llm`, not here, for the same merge reason as `ingestion`. Providers, models,
        # and credentials are registry objects, never settings (ADR 0006). See the Phase 3 spec,
        # section 3.
        "llm": {},
    }
    docs_view_name = "plugins:nautobot_event_tracker:docs"
    searchable_models = ["eventticket", "eventtype"]


config = EventTrackerConfig  # pylint:disable=invalid-name
