"""Create fixtures for tests.

Tickets are built **through the service layer**, never by direct ORM creation with an arbitrary
status. Where a test needs a ticket in a later state, it walks the workflow graph to get there.
This is slower than setting `status` directly, and it is deliberate: a fixture that assigned status
would be the first violation of the rule the whole app exists to enforce.
"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from nautobot.dcim.models import Location, LocationType
from nautobot.extras.models import Status

from nautobot_event_tracker.choices import SeverityChoices, TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.models import EventType
from nautobot_event_tracker.services import tickets as ticket_service


def create_user(username="tester"):
    """Return a user to act as the human source."""
    user, _ = get_user_model().objects.get_or_create(username=username)
    return user


def create_event_types():
    """Create a small set of event types for tests."""
    return [
        EventType.objects.get_or_create(
            name="Test Interface Down",
            defaults={"default_severity": SeverityChoices.MAJOR},
        )[0],
        EventType.objects.get_or_create(
            name="Test Device Unreachable",
            defaults={"default_severity": SeverityChoices.CRITICAL},
        )[0],
        EventType.objects.get_or_create(
            name="Test Disabled Type",
            defaults={"default_severity": SeverityChoices.INFO, "enabled": False},
        )[0],
    ]


def create_location(name="Test Location"):
    """Create a Location, used as the attachable object in attachment tests."""
    location_status = Status.objects.get_for_model(Location).first()
    location_type, _ = LocationType.objects.get_or_create(name="Test Site")
    location_type.content_types.add(ContentType.objects.get_for_model(Location))
    location, _ = Location.objects.get_or_create(
        name=name,
        defaults={"location_type": location_type, "status": location_status},
    )
    return location


def create_ticket(user=None, event_type=None, **kwargs):
    """Create one ticket through the service layer."""
    if event_type is None:
        event_type = create_event_types()[0]
    if user is None:
        user = create_user()
    kwargs.setdefault("title", "Test ticket")
    return ticket_service.create_ticket(
        event_type=event_type,
        source=TicketSourceChoices.HUMAN,
        user=user,
        **kwargs,
    )


#: Shortest path from `new` to each status, as a list of transitions to walk.
PATHS_TO_STATUS = {
    TicketStatusChoices.NEW: [],
    TicketStatusChoices.TRIAGED: [TicketStatusChoices.TRIAGED],
    TicketStatusChoices.IN_PROGRESS: [TicketStatusChoices.TRIAGED, TicketStatusChoices.IN_PROGRESS],
    TicketStatusChoices.SUPPRESSED: [TicketStatusChoices.SUPPRESSED],
    TicketStatusChoices.RESOLVED: [TicketStatusChoices.TRIAGED, TicketStatusChoices.RESOLVED],
    TicketStatusChoices.CLOSED: [TicketStatusChoices.CLOSED],
}


def create_ticket_in_status(status, user=None, **kwargs):
    """Create a ticket and walk the graph until it reaches `status`."""
    if user is None:
        user = create_user()
    ticket = create_ticket(user=user, **kwargs)
    for step in PATHS_TO_STATUS[status]:
        ticket_service.transition(
            ticket=ticket,
            to_status=step,
            source=TicketSourceChoices.HUMAN,
            user=user,
            resolution="Fixed in tests." if step == TicketStatusChoices.RESOLVED else "",
        )
    ticket.refresh_from_db()
    return ticket


def create_eventticket():
    """Create the standard three tickets used by the generic view and API test cases."""
    user = create_user()
    event_type = create_event_types()[0]
    return [
        create_ticket(user=user, event_type=event_type, title=title)
        for title in ("Ticket One", "Ticket Two", "Ticket Three")
    ]
