"""Create fixtures for tests.

Tickets are built **through the service layer**, never by direct ORM creation with an arbitrary
status. Where a test needs a ticket in a later state, it walks the workflow graph to get there.
This is slower than setting `status` directly, and it is deliberate: a fixture that assigned status
would be the first violation of the rule the whole app exists to enforce.
"""

import json
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from django.utils import timezone
from nautobot.dcim.models import Device, DeviceType, Location, LocationType, Manufacturer
from nautobot.extras.models import Role, Status

from nautobot_event_tracker.choices import SeverityChoices, TicketSourceChoices
from nautobot_event_tracker.ingestion.consumers import BrokerMessage, EventConsumer
from nautobot_event_tracker.models import EventType, IngestionStats
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


def create_device(name="somebody-elses-device"):
    """A device this app did not create, for the tests that must not touch one."""
    location_type, _ = LocationType.objects.get_or_create(name="Test Site")
    location_type.content_types.add(ContentType.objects.get_for_model(Device))
    location, _ = Location.objects.get_or_create(
        name="Test Device Location",
        defaults={"location_type": location_type, "status": Status.objects.get_for_model(Location).first()},
    )
    manufacturer, _ = Manufacturer.objects.get_or_create(name="Test Manufacturer")
    device_type, _ = DeviceType.objects.get_or_create(manufacturer=manufacturer, model="Test Model")
    role, _ = Role.objects.get_or_create(name="Test Device Role")
    role.content_types.add(ContentType.objects.get_for_model(Device))

    device, _ = Device.objects.get_or_create(
        name=name,
        defaults={
            "device_type": device_type,
            "role": role,
            "location": location,
            "status": Status.objects.get_for_model(Device).first(),
        },
    )
    return device


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


def create_ticket_in_status(status, user=None, **kwargs):
    """Create a ticket and walk the graph until it reaches `status`."""
    if user is None:
        user = create_user()
    ticket = create_ticket(user=user, **kwargs)
    ticket_service.walk_to_status(
        ticket=ticket,
        to_status=status,
        source=TicketSourceChoices.HUMAN,
        user=user,
        resolution="Fixed in tests.",
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


class FakeEventConsumer(EventConsumer):
    """An in-memory broker, so no test in CI needs a real one.

    Records what was acknowledged, which is how the at-least-once tests check that a message whose
    ticket failed was left on the queue.
    """

    supports_replay = False

    def __init__(self, *, settings=None, topics=(), messages=()):
        """Queue these messages for delivery."""
        super().__init__(settings=settings or {}, topics=topics)
        self.messages = list(messages)
        self.acknowledged = []
        self.connected = False
        self.closed = False

    def connect(self):
        """Nothing to connect to, but the loop expects to be able to say so."""
        self.connected = True

    def poll(self, timeout):
        """Hand over the next queued message, or None once they run out."""
        if not self.messages:
            return None
        return self.messages.pop(0)

    def acknowledge(self, message):
        """Record the acknowledgement rather than sending one."""
        self.acknowledged.append(message)

    def close(self):
        """Note that the loop closed us, which the shutdown tests assert."""
        self.closed = True


def broker_message(payload, *, topic="network.events", **kwargs):
    """Build a BrokerMessage carrying this payload as JSON."""
    return BrokerMessage(topic=topic, value=json.dumps(payload).encode("utf-8"), **kwargs)


#: A topic configuration the ingestion tests share, so the pipeline, the command and the pre-filter
#: are all exercised against the same shape of payload.
INGESTION_TOPIC = {
    "field_map": {"event_type": "event.type", "title": "message", "severity": "event.severity"},
    "defaults": {"event_type": "Test Interface Down"},
    "dedup_key_template": "{event.type}:{host}",
}


def event_payload(**overrides):
    """A payload the pre-filter accepts and the pipeline turns into a ticket."""
    base = {
        "event": {"type": "Test Interface Down", "severity": SeverityChoices.MAJOR},
        "message": "Interface ethernet-1/1 is down",
        "host": "leaf-01",
    }
    base.update(overrides)
    return base


class FakeClock:
    """A monotonic clock a test moves by hand, for flush intervals and token buckets."""

    def __init__(self, start=0.0):
        """Start here."""
        self.now = start

    def __call__(self):
        """Read the clock, as `time.monotonic` would."""
        return self.now

    def advance(self, seconds):
        """Move time forward."""
        self.now += seconds


class FakeWallClock:
    """Wall time a test moves by hand, for the bucket a count lands in."""

    def __init__(self, start=datetime(2026, 8, 15, 3, 14, tzinfo=datetime_timezone.utc)):
        """Start at a fixed moment, so buckets are predictable."""
        self.now = start

    def __call__(self):
        """Read the clock, as `timezone.now` would."""
        return self.now

    def advance(self, **kwargs):
        """Move wall time forward."""
        self.now += timedelta(**kwargs)


def ingestion_settings(**overrides):
    """A PLUGINS_CONFIG override carrying this ingestion block.

    One spelling of the app label and the `ingestion` key, so a test that moves between modules
    cannot find two same-named helpers meaning different things.
    """
    block = {"topics": {"network.events": INGESTION_TOPIC}, **overrides}
    return override_settings(PLUGINS_CONFIG={"nautobot_event_tracker": {"ingestion": block}})


def create_ingestionstats(**overrides):
    """One ingestion counter row."""
    defaults = {
        "consumer_name": "consumer-1",
        "topic": "network.events",
        "bucket_start": timezone.now().replace(second=0, microsecond=0),
    }
    return IngestionStats.objects.create(**{**defaults, **overrides})


class RefusalAssertions:  # pylint: disable=too-few-public-methods
    """Assert that something was refused, and that the message says why.

    Mixed into the tests for both halves of startup validation. The message is the whole point of
    the exercise - an operator reading it at 03:00 is the reason the validation exists - so every
    test asserts on it rather than on the exception's type alone.
    """

    def assert_names(self, message, fragments):
        """Assert the message names each of these faults."""
        for fragment in fragments:
            self.assertIn(fragment, message)  # pylint: disable=no-member
        return message
