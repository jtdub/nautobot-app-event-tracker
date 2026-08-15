"""Seed the starter catalogue of event types."""

from django.db import migrations

SEED_EVENT_TYPES = [
    ("Device Unreachable", "critical", "A device stopped responding to reachability checks."),
    ("Interface Down", "major", "An interface transitioned to a down state."),
    ("BGP Session Down", "major", "A BGP session left the established state."),
    ("Circuit Down", "critical", "A provider circuit reported a loss of service."),
    ("Optical Degradation", "minor", "Optical light levels drifted outside their expected range."),
    ("High CPU Utilization", "minor", "Device CPU utilization exceeded its threshold."),
    ("High Memory Utilization", "minor", "Device memory utilization exceeded its threshold."),
    ("Configuration Drift", "warning", "Running configuration diverged from the intended configuration."),
    ("Hardware Alarm", "major", "A device reported a hardware fault or environmental alarm."),
    ("Unclassified", "info", "An event that did not match any known type."),
]


def seed_event_types(apps, schema_editor):
    """Create the starter event types.

    Idempotent: `get_or_create` keyed on name, so a database where an operator already made a type
    of the same name is left alone rather than failing.
    """
    EventType = apps.get_model("nautobot_event_tracker", "EventType")
    for name, default_severity, description in SEED_EVENT_TYPES:
        EventType.objects.get_or_create(
            name=name,
            defaults={
                "default_severity": default_severity,
                "description": description,
                "enabled": True,
            },
        )


def remove_event_types(apps, schema_editor):
    """Remove seeded event types that are still unused.

    A seeded type with tickets attached is left in place: the reverse of a seed is not a licence to
    break referential integrity, and `EventTicket.event_type` is PROTECT anyway.
    """
    EventType = apps.get_model("nautobot_event_tracker", "EventType")
    names = [name for name, _, _ in SEED_EVENT_TYPES]
    EventType.objects.filter(name__in=names, tickets__isnull=True).delete()


class Migration(migrations.Migration):
    """Seed the starter catalogue of event types."""

    dependencies = [
        ("nautobot_event_tracker", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_event_types, remove_event_types),
    ]
