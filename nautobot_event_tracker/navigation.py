"""Menu items."""

from nautobot.apps.ui import NavMenuAddButton, NavMenuGroup, NavMenuItem, NavMenuTab

from nautobot_event_tracker.services import analytics as analytics_service

#: The dashboard's menu item, present only when the dashboard is. Turning the block off removes
#: the route as well, so a menu item left behind would reverse a name that no longer exists.
#:
#: `is_enabled()` rather than `get_settings()` for the reason `urls.py` gives: this is read while
#: the app is loading, and a malformed key elsewhere in the block must not stop Nautobot starting.
#:
#: Gated on `view_eventticket` rather than on a permission of its own: the page shows nothing a
#: ticket reader may not already see, and rule D2 narrows every number on it per user anyway.
DASHBOARD_ITEMS = (
    (
        NavMenuItem(
            link="plugins:nautobot_event_tracker:dashboard",
            name="Analytics",
            permissions=["nautobot_event_tracker.view_eventticket"],
        ),
    )
    if analytics_service.is_enabled()
    else ()
)

items = DASHBOARD_ITEMS + (
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
    NavMenuItem(
        link="plugins:nautobot_event_tracker:agentrun_list",
        name="Agent Runs",
        permissions=["nautobot_event_tracker.view_agentrun"],
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:agenttoolcall_list",
        name="Agent Tool Calls",
        permissions=["nautobot_event_tracker.view_agenttoolcall"],
    ),
    NavMenuItem(
        link="plugins:nautobot_event_tracker:ticketembedding_list",
        name="Ticket Embeddings",
        permissions=["nautobot_event_tracker.view_ticketembedding"],
    ),
)

menu_items = (
    NavMenuTab(
        name="Apps",
        groups=(NavMenuGroup(name="Event Tracker", items=items),),
    ),
)
