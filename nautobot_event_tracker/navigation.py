"""Menu items."""

from nautobot.apps.ui import NavMenuAddButton, NavMenuGroup, NavMenuItem, NavMenuTab

items = (
    NavMenuItem(
        link="plugins:nautobot_event_tracker:eventtrackerexamplemodel_list",
        name="Event Tracker",
        permissions=["nautobot_event_tracker.view_eventtrackerexamplemodel"],
        buttons=(
            NavMenuAddButton(
                link="plugins:nautobot_event_tracker:eventtrackerexamplemodel_add",
                permissions=["nautobot_event_tracker.add_eventtrackerexamplemodel"],
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
