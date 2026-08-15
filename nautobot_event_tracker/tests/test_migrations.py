"""Test the event type seed migration.

By the time the suite runs, the migration has already been applied to the test database, so these
tests assert its outcome and then exercise its functions directly for idempotency and reversal.
"""

from importlib import import_module

from django.apps import apps
from django.test import TestCase

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.models import EventType
from nautobot_event_tracker.tests import fixtures

# The module name starts with a digit, so it cannot be imported with `import ... from`.
seed_migration = import_module("nautobot_event_tracker.migrations.0002_seed_event_types")


class SeedEventTypesTest(TestCase):
    """The starter catalogue."""

    def test_migration_created_every_seed_type(self):
        """All ten seeded types exist after migration."""
        names = [name for name, _, _ in seed_migration.SEED_EVENT_TYPES]
        self.assertEqual(len(names), 10)
        for name in names:
            self.assertTrue(EventType.objects.filter(name=name).exists(), f"'{name}' was not seeded")

    def test_seed_severities(self):
        """Each seeded type carries the documented default severity."""
        for name, severity, _ in seed_migration.SEED_EVENT_TYPES:
            event_type = EventType.objects.get(name=name)
            self.assertEqual(event_type.default_severity, severity, f"'{name}' has the wrong severity")
            self.assertIn(severity, SeverityChoices.values())

    def test_seed_types_are_enabled_and_described(self):
        """Seeded types are usable and self-describing."""
        for name, _, _ in seed_migration.SEED_EVENT_TYPES:
            event_type = EventType.objects.get(name=name)
            self.assertTrue(event_type.enabled)
            self.assertTrue(event_type.description)

    def test_seeding_again_is_idempotent(self):
        """Re-running the seed must not duplicate rows or fail."""
        before = EventType.objects.count()
        seed_migration.seed_event_types(apps, None)
        self.assertEqual(EventType.objects.count(), before)

    def test_seeding_does_not_overwrite_operator_edits(self):
        """get_or_create keys on name, so an operator's changes survive a re-run."""
        event_type = EventType.objects.get(name="Interface Down")
        event_type.default_severity = SeverityChoices.CRITICAL
        event_type.save()

        seed_migration.seed_event_types(apps, None)

        event_type.refresh_from_db()
        self.assertEqual(event_type.default_severity, SeverityChoices.CRITICAL)

    def test_reverse_removes_unused_seed_types(self):
        """Reversing deletes seeded types that nothing references."""
        seed_migration.remove_event_types(apps, None)
        remaining = EventType.objects.filter(name__in=[name for name, _, _ in seed_migration.SEED_EVENT_TYPES])
        self.assertFalse(remaining.exists())

    def test_reverse_keeps_seed_types_that_have_tickets(self):
        """A seeded type with tickets is left alone; the reverse must not break integrity."""
        event_type = EventType.objects.get(name="Interface Down")
        fixtures.create_ticket(event_type=event_type)

        seed_migration.remove_event_types(apps, None)

        self.assertTrue(EventType.objects.filter(name="Interface Down").exists())
        self.assertFalse(EventType.objects.filter(name="Circuit Down").exists())
