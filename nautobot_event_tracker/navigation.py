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

menu_items = (
    NavMenuTab(
        name="Apps",
        groups=(NavMenuGroup(name="Event Tracker", items=tuple(items)),),
    ),
)
