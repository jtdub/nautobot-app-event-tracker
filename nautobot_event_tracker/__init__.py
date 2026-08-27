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
    author_email = "james.williams@jtdub.com"
    description = "Network event ticketing and AI-assisted network operations for Nautobot."
    base_url = "event-tracker"
    # Checked by Nautobot at startup, which is the case the pyproject pin cannot cover: a Nautobot
    # upgraded in place underneath an installed app. The app uses v3-only APIs throughout - the UI
    # Component Framework above all - so an older core does not merely warn, it fails to render.
    # Keep in step with `docs/admin/compatibility_matrix.md` and the pin in `pyproject.toml`.
    min_version = "3.2.0"
    max_version = "3.99.99"
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
        # The agent (Phase 4B, spec section 3). Empty for the same merge reason as the two above:
        # the defaults for the keys inside it are applied by `services.agent`. Agents are off until
        # a deployment names a provider and a model and switches them on.
        "agent": {},
        # The analytics dashboard (Phase 5B, spec section 4). Empty for the same merge reason as
        # the blocks above; `services.analytics` applies the defaults for the keys inside it. This
        # is the one block that is on by default, because it reaches no network and spends no
        # money: it runs a handful of GROUP BY queries against tables already in the database.
        "dashboard": {},
    }
    docs_view_name = "plugins:nautobot_event_tracker:docs"
    searchable_models = ["eventticket", "eventtype"]


config = EventTrackerConfig  # pylint:disable=invalid-name
