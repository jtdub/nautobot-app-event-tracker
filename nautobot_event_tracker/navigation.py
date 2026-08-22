"""Menu items."""

from nautobot.apps.ui import NavMenuAddButton, NavMenuGroup, NavMenuItem, NavMenuTab

items = (
    NavMenuItem(
        link="plugins:nautobot_event_tracker:eventticket_list",
        name="Tickets",
        permissions=["nautobot_event_tracker.view_eventticket"],
        buttons=(
            NavMenuAddButton(
                link="plugins:nautobot_event_tracker:eventticket_add",
                permissions=["nautobot_event_tracker.add_eventticket"],
            ),
        ),
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:ingestionstats_list",
        name="Ingestion Stats",
        permissions=["nautobot_event_tracker.view_ingestionstats"],
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:eventtype_list",
        name="Event Types",
        permissions=["nautobot_event_tracker.view_eventtype"],
        buttons=(
            NavMenuAddButton(
                link="plugins:nautobot_event_tracker:eventtype_add",
                permissions=["nautobot_event_tracker.add_eventtype"],
            ),
        ),
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:llmprovider_list",
        name="LLM Providers",
        permissions=["nautobot_event_tracker.view_llmprovider"],
        buttons=(
            NavMenuAddButton(
                link="plugins:nautobot_event_tracker:llmprovider_add",
                permissions=["nautobot_event_tracker.add_llmprovider"],
            ),
        ),
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:llmmodel_list",
        name="LLM Models",
        permissions=["nautobot_event_tracker.view_llmmodel"],
        buttons=(
            NavMenuAddButton(
                link="plugins:nautobot_event_tracker:llmmodel_add",
                permissions=["nautobot_event_tracker.add_llmmodel"],
            ),
        ),
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:llmusagerecord_list",
        name="LLM Usage",
        permissions=["nautobot_event_tracker.view_llmusagerecord"],
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:mcpserver_list",
        name="MCP Servers",
        permissions=["nautobot_event_tracker.view_mcpserver"],
        buttons=(
            NavMenuAddButton(
                link="plugins:nautobot_event_tracker:mcpserver_add",
                permissions=["nautobot_event_tracker.add_mcpserver"],
            ),
        ),
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:mcptool_list",
        name="MCP Tools",
        permissions=["nautobot_event_tracker.view_mcptool"],
    ),
)

menu_items = (
    NavMenuTab(
        name="Apps",
        groups=(NavMenuGroup(name="Event Tracker", items=items),),
    ),
)
