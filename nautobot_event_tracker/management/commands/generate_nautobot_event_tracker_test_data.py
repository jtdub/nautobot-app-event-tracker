"""Generate test data for the Event Tracker app.

Every ticket is built through the service layer, exactly as the test fixtures are, and a ticket
that needs to end in a terminal state walks the workflow graph to get there. A command that
assigned `status` directly would be the first violation of the rule the app exists to enforce, and
it would sit in the app package where somebody could reasonably read it as an example.

Deterministic: the same `--seed` produces the same tickets, so two people comparing screenshots are
looking at the same data. Randomness comes from a seeded `random.Random`, never the module-level
functions, which any other code in the process could have reseeded.
"""

import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS
from nautobot.extras.models import Tag

from nautobot_event_tracker.choices import SeverityChoices, TicketSourceChoices, TicketStatusChoices
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

#: Hostnames the generated titles refer to. Deliberately not created as Devices: inventing DCIM
#: objects from a ticketing app's test data command is how a demo database ends up with devices
#: nobody can explain. Where real objects exist, the tickets attach to those instead.
HOSTS = ("edge-rtr-01", "edge-rtr-02", "leaf-01", "leaf-02", "spine-01", "core-sw-01")

INTERFACES = ("ethernet-1/1", "ethernet-1/2", "ethernet-1/3", "Gi0/0/1", "Te0/0/0/3")

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
        doomed = EventTicket.objects.filter(tags__name=TEST_DATA_TAG)
        count = doomed.count()
        # A queryset delete, which cascades to the trail. `TicketUpdate.delete()` refuses one at a
        # time, and it is right to: an update is append-only for as long as its ticket exists.
        doomed.delete()
        self.stdout.write(f"Deleted {count} tagged Event Tracker tickets.")

    def _generate(self, rng, count):
        """Create `count` tickets and give each of them a history."""
        tag = self._tag()
        user = self._user()
        event_types = list(EventType.objects.filter(enabled=True))
        if not event_types:
            raise CommandError(
                "No enabled event types exist. Run `nautobot-server migrate` so the seeded "
                "catalogue is in place, then try again."
            )

        attachable = self._attachable_objects()
        created = 0
        for status, ticket_count in self._status_plan(count):
            for _ in range(ticket_count):
                ticket = self._create_ticket(rng, user, tag, event_types)
                self._add_history(rng, ticket, user, attachable)
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

    def _add_history(self, rng, ticket, user, attachable):
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

        if attachable and rng.random() < 0.5:
            ticket_service.attach_object(
                ticket=ticket,
                obj=rng.choice(attachable),
                source=TicketSourceChoices.HUMAN,
                user=user,
            )

    def _walk_to(self, rng, ticket, user, status):
        """Move the ticket to `status` one legal transition at a time."""
        for step in _PATHS[status]:
            ticket_service.transition(
                ticket=ticket,
                to_status=step,
                source=TicketSourceChoices.HUMAN,
                user=user,
                message=rng.choice(COMMENTS) if rng.random() < 0.4 else "",
                resolution=rng.choice(RESOLUTIONS) if step == TicketStatusChoices.RESOLVED else "",
            )

    @staticmethod
    def _tag():
        """The tag every generated ticket carries, created if this is the first run."""
        tag, _ = Tag.objects.get_or_create(
            name=TEST_DATA_TAG,
            defaults={"description": "Created by generate_nautobot_event_tracker_test_data."},
        )
        tag.content_types.add(_ticket_content_type())
        return tag

    @staticmethod
    def _user():
        """Somebody to attribute the tickets to."""
        user = get_user_model().objects.filter(is_active=True).order_by("username").first()
        if user is not None:
            return user
        return get_user_model().objects.create(username=DEMO_USERNAME, is_active=True)

    @staticmethod
    def _attachable_objects():
        """Real objects to attach, from whatever this database already holds.

        This command creates none of its own: a ticketing app inventing devices is how a demo
        database ends up with DCIM objects nobody can explain. Populate the lab (or run Nautobot's
        own `generate_test_data`) first and the tickets will point at those.
        """
        objects = []
        for content_type in ticket_service.get_attachable_content_types():
            model = content_type.model_class()
            if model is None:
                continue
            objects.extend(model.objects.all()[:5])
        return objects


def _ticket_content_type():
    """The ticket's content type, which the tag has to be allowed on."""
    from django.contrib.contenttypes.models import ContentType  # pylint: disable=import-outside-toplevel

    return ContentType.objects.get_for_model(EventTicket)


#: The shortest legal route to each status, walked one transition at a time. `new` needs no moves.
_PATHS = {
    TicketStatusChoices.NEW: [],
    TicketStatusChoices.TRIAGED: [TicketStatusChoices.TRIAGED],
    TicketStatusChoices.IN_PROGRESS: [TicketStatusChoices.TRIAGED, TicketStatusChoices.IN_PROGRESS],
    TicketStatusChoices.SUPPRESSED: [TicketStatusChoices.SUPPRESSED],
    TicketStatusChoices.RESOLVED: [TicketStatusChoices.TRIAGED, TicketStatusChoices.RESOLVED],
    TicketStatusChoices.CLOSED: [TicketStatusChoices.TRIAGED, TicketStatusChoices.CLOSED],
}
