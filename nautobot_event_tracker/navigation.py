"""Menu items.

Two tabs rather than one. Tickets, event types, ingestion statistics and the analytics dashboard
are what this app is for, and they stay under **Apps** where they have always been. The records of
what the AI layer did - usage, agent runs, tool calls, embeddings - go under **AI Tools**, beside
the registry they read (ADR 0010), because that is where an operator who is looking at a provider
or a tool is already standing.

The AI Tools tab is shared, and Nautobot merges two `NavMenuTab` declarations only when the name,
weight and icon all match exactly - one character of difference makes a second tab. So the three
values are imported from the app that publishes them rather than copied, which is what its
`docs/dev/extending.md` asks a sibling app that already depends on it to do. Group weight 300:
that app claims 100 and 200 for its own two groups.
"""

from nautobot.apps.ui import NavMenuAddButton, NavMenuGroup, NavMenuItem, NavMenuTab
from nautobot_ai_models.constants import AI_TOOLS_TAB_ICON, AI_TOOLS_TAB_NAME, AI_TOOLS_TAB_WEIGHT

from nautobot_event_tracker.services import analytics as analytics_service

#: The group weight this app claims under the AI Tools tab. See the module docstring.
AI_TOOLS_GROUP_WEIGHT = 300

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
)

#: The AI half. Every one of these is a record of something that happened rather than something an
#: operator configures, which is why not one of them carries an add button: they are written by
#: `services.llm`, `services.agent` and `services.rag`, and by nothing else.
ai_items = (
    NavMenuItem(
        link="plugins:nautobot_event_tracker:llmusagerecord_list",
        name="LLM Usage",
        permissions=["nautobot_event_tracker.view_llmusagerecord"],
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
    NavMenuTab(
        name=AI_TOOLS_TAB_NAME,
        weight=AI_TOOLS_TAB_WEIGHT,
        icon=AI_TOOLS_TAB_ICON,
        groups=(
            NavMenuGroup(
                name="Event Tracker AI",
                weight=AI_TOOLS_GROUP_WEIGHT,
                items=ai_items,
            ),
        ),
    ),
)
