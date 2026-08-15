"""Generate test data for the Event Tracker app.

Every ticket is built through the service layer, exactly as the test fixtures are, and a ticket
that needs to end in a terminal state walks the workflow graph to get there. A command that
assigned `status` directly would be the first violation of the rule the app exists to enforce, and
it would sit in the app package where somebody could reasonably read it as an example.

Deterministic: the same `--seed` produces the same tickets, so two people comparing screenshots are
looking at the same data. Randomness comes from a seeded `random.Random`, never the module-level
functions, which any other code in the process could have reseeded.

It also creates the handful of devices and interfaces the tickets point at. An earlier draft
refused to, on the grounds that a ticketing app inventing DCIM objects leaves a demo database full
of devices nobody can explain - but that is what `generate_test_data` is *for*, and Nautobot's own
populates a whole demo estate. The objection is answered by tagging: everything this command makes
carries `event-tracker-test-data`, so it is explicable at a glance and `--flush` removes it.
"""

import random

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS
from nautobot.dcim.models import Device, Interface
from nautobot.extras.models import Tag

from nautobot_event_tracker.choices import SeverityChoices, TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.dcim_fixtures import (
    default_status,
    ensure_device,
    ensure_device_type,
    ensure_interface,
    ensure_location,
    ensure_role,
)
from nautobot_event_tracker.models import EventTicket, EventType
from nautobot_event_tracker.services import tickets as ticket_service

#: Every ticket this command creates carries this tag, and `--flush` deletes exactly the tickets
#: that carry it. A person's own tickets are never touched.
TEST_DATA_TAG = "event-tracker-test-data"

#: The default seed. A constant rather than the clock, so the data is the same on two machines.
DEFAULT_SEED = 1337

DEFAULT_COUNT = 50

#: The user tickets are attributed to when the database has no other. Named so that nobody mistakes
#: it for a real account.
DEMO_USERNAME = "event-tracker-demo"

#: The demo estate: the devices the generated tickets are about, and are attached to. Created by
#: this command, tagged, and removed by `--flush`. Where a database already holds a device of the
#: same name - the containerlab lab's `leaf-01`, for instance - that one is used rather than a
#: second one made.
HOSTS = ("edge-rtr-01", "edge-rtr-02", "leaf-01", "leaf-02", "spine-01", "core-sw-01")

INTERFACES = ("ethernet-1/1", "ethernet-1/2", "ethernet-1/3", "Gi0/0/1", "Te0/0/0/3")

#: What the demo estate is filed under. Named so that nobody wonders where it came from.
DEMO_LOCATION_TYPE = "Demo Site"
DEMO_LOCATION = "Event Tracker Demo"
DEMO_MANUFACTURER = "Event Tracker Demo"
DEMO_DEVICE_TYPE = "Demo Switch"
DEMO_ROLE = "Demo Switch"

#: One title template per seeded event type, so a generated list reads like a real one.
TITLES = {
    "Device Unreachable": "{host} stopped responding to reachability checks",
    "Interface Down": "{interface} is down on {host}",
    "BGP Session Down": "BGP session to {host} left the established state",
    "Circuit Down": "Provider circuit to {host} reported loss of service",
    "Optical Degradation": "Optical levels on {interface} at {host} drifted out of range",
    "High CPU Utilization": "CPU on {host} exceeded its threshold",
    "High Memory Utilization": "Memory on {host} exceeded its threshold",
    "Configuration Drift": "{host} configuration diverged from intent",
    "Hardware Alarm": "{host} reported a hardware alarm",
    "Unclassified": "Unrecognised event from {host}",
}

COMMENTS = (
    "Acknowledged, taking a look.",
    "Confirmed on the device; the counters agree with the alert.",
    "Raised with the provider, waiting on their update.",
    "Not reproducible from here - watching for a recurrence.",
    "Same symptom as last week; checking whether the fix regressed.",
)

RESOLUTIONS = (
    "Optics replaced and levels are back in range.",
    "Provider restored the circuit; confirmed stable for an hour.",
    "Interface re-enabled after the maintenance window.",
    "Configuration reapplied from intent and verified.",
    "Cleared on its own; monitored for a day with no repeat.",
)

#: How many of each status to make, out of `DEFAULT_COUNT`. Weighted towards open work, because a
#: list where everything is closed shows nothing about the parts of the UI people use.
STATUS_MIX = (
    (TicketStatusChoices.NEW, 0.30),
    (TicketStatusChoices.TRIAGED, 0.20),
    (TicketStatusChoices.IN_PROGRESS, 0.16),
    (TicketStatusChoices.SUPPRESSED, 0.08),
    (TicketStatusChoices.RESOLVED, 0.14),
    (TicketStatusChoices.CLOSED, 0.12),
)


class Command(BaseCommand):
    """Populate the database with Event Tracker data as a baseline for testing or a demonstration."""

    help = __doc__

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help='The database to generate the test data in. Defaults to the "default" database.',
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help=f"Delete the tickets tagged '{TEST_DATA_TAG}' before generating new ones.",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=DEFAULT_SEED,
            help="Seed for the generated data. The same seed produces the same tickets.",
        )
        parser.add_argument(
            "--count",
            type=int,
            default=DEFAULT_COUNT,
            help="How many tickets to create.",
        )

    def handle(self, *args, **options):
        """Entry point to the management command."""
        if options["database"] != DEFAULT_DB_ALIAS:
            # The service layer holds its own transactions and takes no database argument, so
            # honouring this would mean writing tickets to one database and their trail to another.
            raise CommandError(
                "Event Tracker test data is written through the service layer, which uses the "
                f"default database. Remove --database, or run against '{DEFAULT_DB_ALIAS}'."
            )

        if options["flush"]:
            self._flush()

        # Seeded and local: not the module-level functions, which any other code in the process
        # could have reseeded, and not for anything that needs to be unguessable.
        rng = random.Random(options["seed"])  # noqa: S311
        created = self._generate(rng, options["count"])
        self.stdout.write(self.style.SUCCESS(f"Created {created} Event Tracker tickets."))

    def _flush(self):
        """Delete exactly what this command created, identified by its tag."""
        tickets = EventTicket.objects.filter(tags__name=TEST_DATA_TAG)
        ticket_count = tickets.count()
        # A queryset delete, which cascades to the trail. `TicketUpdate.delete()` refuses one at a
        # time, and it is right to: an update is append-only for as long as its ticket exists.
        tickets.delete()

        # Tickets first: an attachment is a content type and a UUID rather than a foreign key, so
        # nothing stops a device being deleted out from under one - the ticket page just stops
        # listing it. Deleting the tickets first means that never happens even briefly.
        devices = Device.objects.filter(tags__name=TEST_DATA_TAG)
        device_count = devices.count()
        devices.delete()

        # The location, device type, manufacturer and role are left behind: they are empty
        # scaffolding, and somebody may have filed their own objects under them by now.
        self.stdout.write(f"Deleted {ticket_count} tagged Event Tracker tickets and {device_count} demo devices.")

    def _generate(self, rng, count):
        """Create `count` tickets and give each of them a history."""
        if count <= 0:
            # `--flush --count 0` is the way to leave the database clean: there are no tickets to
            # be about, so there is no estate to create either.
            return 0

        tag = self._tag()
        user = self._user()
        event_types = list(EventType.objects.filter(enabled=True))
        if not event_types:
            raise CommandError(
                "No enabled event types exist. Run `nautobot-server migrate` so the seeded "
                "catalogue is in place, then try again."
            )

        self._inventory(tag)
        devices = self._attachable_devices()
        created = 0
        for status, ticket_count in self._status_plan(count):
            for _ in range(ticket_count):
                ticket = self._create_ticket(rng, user, tag, event_types)
                self._add_history(rng, ticket, user, devices)
                self._walk_to(rng, ticket, user, status)
                created += 1
        return created

    @staticmethod
    def _status_plan(count):
        """How many tickets to make in each status, adding up to `count`."""
        planned = [(status, int(count * share)) for status, share in STATUS_MIX]
        shortfall = count - sum(number for _, number in planned)
        # Whatever rounding dropped goes to the first status, so the count is exact.
        return [(planned[0][0], planned[0][1] + shortfall)] + planned[1:]

    def _create_ticket(self, rng, user, tag, event_types):
        """Open one ticket through the service layer."""
        event_type = rng.choice(event_types)
        host = rng.choice(HOSTS)
        interface = rng.choice(INTERFACES)
        title = TITLES.get(event_type.name, "{host} reported an event").format(host=host, interface=interface)

        return ticket_service.create_ticket_for_user(
            user=user,
            title=title,
            event_type=event_type,
            severity=rng.choice(SeverityChoices.values()) if rng.random() < 0.3 else None,
            description=f"Reported by the demo generator for {host}.",
            payload={"host": host, "interface": interface, "event": {"type": event_type.name}},
            assignee=user if rng.random() < 0.4 else None,
            tags=[tag],
        )

    def _add_history(self, rng, ticket, user, devices):
        """Give the ticket the kind of trail a worked ticket has."""
        for _ in range(rng.randint(0, 3)):
            ticket_service.add_comment(
                ticket=ticket,
                message=rng.choice(COMMENTS),
                source=TicketSourceChoices.HUMAN,
                user=user,
            )

        if rng.random() < 0.35:
            ticket_service.set_severity(
                ticket=ticket,
                severity=rng.choice(SeverityChoices.values()),
                source=TicketSourceChoices.HUMAN,
                user=user,
            )

        # The device the ticket is about, not an arbitrary object: a demo where the ticket titled
        # "ethernet-1/1 is down on leaf-02" is attached to a location in another country teaches
        # the reader that the attachment means nothing.
        device = devices.get(ticket.payload.get("host"))
        if device is not None:
            ticket_service.attach_object(
                ticket=ticket,
                obj=device,
                source=TicketSourceChoices.HUMAN,
                user=user,
            )

    def _walk_to(self, rng, ticket, user, status):
        """Move the ticket to `status` one legal transition at a time."""
        ticket_service.walk_to_status(
            ticket=ticket,
            to_status=status,
            source=TicketSourceChoices.HUMAN,
            user=user,
            message=rng.choice(COMMENTS) if rng.random() < 0.4 else "",
            resolution=rng.choice(RESOLUTIONS),
        )

    def _inventory(self, tag):
        """Create the devices and interfaces the tickets are about, and tag them.

        `get_or_create` throughout, so a database that already holds a device of one of these names
        keeps it - which is what happens after the containerlab lab has been populated, and is the
        behaviour you want: the tickets then point at the real thing.
        """
        location = ensure_location(location_type_name=DEMO_LOCATION_TYPE, location_name=DEMO_LOCATION)
        device_type = ensure_device_type(manufacturer_name=DEMO_MANUFACTURER, model_name=DEMO_DEVICE_TYPE)
        role = ensure_role(role_name=DEMO_ROLE)

        # Resolved once rather than per device and per interface: `get_for_model()` returns a lazy
        # queryset, so each call was its own SELECT - thirty-odd per run, for two distinct answers.
        device_status = default_status(Device)
        interface_status = default_status(Interface)

        for name in HOSTS:
            device, created = ensure_device(
                name, location=location, device_type=device_type, role=role, status=device_status
            )
            if not created:
                # Somebody else's device - the lab's `leaf-01`, say. Not ours to tag, and therefore
                # not ours to delete; and not ours to hang Cisco-shaped interface names off either.
                continue
            device.tags.add(tag)
            for interface_name in INTERFACES:
                ensure_interface(device=device, name=interface_name, status=interface_status)

    @staticmethod
    def _attachable_devices():
        """The demo estate, by name, if a device may be attached to a ticket here at all.

        Read after `_inventory` has run, so it holds whichever device answers to each name - the
        one this command made, or the lab's. An installation that has taken `dcim.device` out of
        `attachable_object_types` gets tickets with no attachments rather than a crash halfway
        through: the service layer would refuse every one of them, and it would be right to.
        """
        if "dcim.device" not in ticket_service.get_attachable_object_types():
            return {}
        return {device.name: device for device in Device.objects.filter(name__in=HOSTS)}

    @staticmethod
    def _tag():
        """The tag every generated ticket carries, created if this is the first run."""
        tag, _ = Tag.objects.get_or_create(
            name=TEST_DATA_TAG,
            defaults={"description": "Created by generate_nautobot_event_tracker_test_data."},
        )
        tag.content_types.add(*[ContentType.objects.get_for_model(model) for model in (EventTicket, Device)])
        return tag

    @staticmethod
    def _user():
        """Somebody to attribute the tickets to."""
        user = get_user_model().objects.filter(is_active=True).order_by("username").first()
        if user is not None:
            return user
        return get_user_model().objects.create(username=DEMO_USERNAME, is_active=True)
