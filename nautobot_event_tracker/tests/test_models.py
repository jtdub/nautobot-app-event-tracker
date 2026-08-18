"""Test the Event Tracker models, their validation rules C1-C3, and the append-only guard."""

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone
from nautobot.apps.testing import ModelTestCases

from nautobot_event_tracker import models
from nautobot_event_tracker.choices import (
    LLMProviderTypeChoices,
    LLMPurposeChoices,
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.models import TicketUpdateImmutableError
from nautobot_event_tracker.tests import fixtures


class TestEventType(ModelTestCases.BaseModelTestCase):
    """Test EventType."""

    model = models.EventType

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        super().setUpTestData()
        fixtures.create_event_types()

    def test_str(self):
        """An event type stringifies as its name."""
        event_type = models.EventType.objects.create(name="Stringify Me")
        self.assertEqual(str(event_type), "Stringify Me")

    def test_default_severity_defaults_to_minor(self):
        """A type created without a severity gets the documented default."""
        event_type = models.EventType.objects.create(name="Defaulted")
        self.assertEqual(event_type.default_severity, SeverityChoices.MINOR)

    def test_protected_while_tickets_exist(self):
        """An event type with tickets cannot be deleted."""
        ticket = fixtures.create_ticket()
        with self.assertRaises(ProtectedError):
            ticket.event_type.delete()


class TestEventTicket(ModelTestCases.BaseModelTestCase):
    """Test EventTicket."""

    model = models.EventTicket

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        super().setUpTestData()
        fixtures.create_eventticket()

    def test_str(self):
        """A ticket stringifies as its title."""
        ticket = fixtures.create_ticket(title="Stringify Me")
        self.assertEqual(str(ticket), "Stringify Me")

    def test_new_ticket_defaults(self):
        """A freshly created ticket starts new, open, and counted once."""
        ticket = fixtures.create_ticket()
        self.assertEqual(ticket.status, TicketStatusChoices.NEW)
        self.assertEqual(ticket.event_count, 1)
        self.assertTrue(ticket.is_open)
        self.assertIsNone(ticket.resolved_at)
        self.assertIsNone(ticket.closed_at)
        self.assertEqual(ticket.resolution, "")

    def test_severity_defaults_from_event_type(self):
        """Creating without a severity takes the event type's default."""
        event_type = models.EventType.objects.create(name="Critical Type", default_severity=SeverityChoices.CRITICAL)
        ticket = fixtures.create_ticket(event_type=event_type)
        self.assertEqual(ticket.severity, SeverityChoices.CRITICAL)

    def test_is_open_across_statuses(self):
        """is_open must agree with the terminal status set."""
        for status in TicketStatusChoices.values():
            ticket = fixtures.create_ticket_in_status(status)
            expected = status not in (TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED)
            self.assertEqual(ticket.is_open, expected, f"is_open wrong for '{status}'")


class TestEventTicketValidation(TestCase):
    """C1 and C2: a ticket's timestamps and resolution must agree with its status."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        cls.event_type = fixtures.create_event_types()[0]

    def _ticket(self, **overrides):
        """Build (without saving) a ticket with sane defaults."""
        now = timezone.now()
        values = {
            "title": "Validation ticket",
            "event_type": self.event_type,
            "status": TicketStatusChoices.NEW,
            "severity": SeverityChoices.MAJOR,
            "source": TicketSourceChoices.HUMAN,
            "first_seen": now,
            "last_seen": now,
        }
        values.update(overrides)
        return models.EventTicket(**values)

    # --- C1: timestamps agree with status ---

    def test_c1_resolved_requires_resolved_at(self):
        """A resolved ticket without a resolved time is invalid."""
        ticket = self._ticket(status=TicketStatusChoices.RESOLVED, resolution="done")
        with self.assertRaises(ValidationError) as context:
            ticket.full_clean()
        self.assertIn("resolved_at", context.exception.message_dict)

    def test_c1_open_ticket_rejects_resolved_at(self):
        """An open ticket carrying a resolved time is invalid."""
        ticket = self._ticket(status=TicketStatusChoices.NEW, resolved_at=timezone.now())
        with self.assertRaises(ValidationError) as context:
            ticket.full_clean()
        self.assertIn("resolved_at", context.exception.message_dict)

    def test_c1_closed_requires_closed_at(self):
        """A closed ticket without a closed time is invalid."""
        ticket = self._ticket(
            status=TicketStatusChoices.CLOSED,
            resolved_at=timezone.now(),
            resolution="done",
        )
        with self.assertRaises(ValidationError) as context:
            ticket.full_clean()
        self.assertIn("closed_at", context.exception.message_dict)

    def test_c1_non_closed_rejects_closed_at(self):
        """Only a closed ticket may carry a closed time."""
        now = timezone.now()
        ticket = self._ticket(
            status=TicketStatusChoices.RESOLVED,
            resolved_at=now,
            closed_at=now,
            resolution="done",
        )
        with self.assertRaises(ValidationError) as context:
            ticket.full_clean()
        self.assertIn("closed_at", context.exception.message_dict)

    def test_c1_closed_before_resolved_is_invalid(self):
        """A ticket cannot be closed before it was resolved."""
        now = timezone.now()
        ticket = self._ticket(
            status=TicketStatusChoices.CLOSED,
            resolved_at=now,
            closed_at=now - timezone.timedelta(hours=1),
            resolution="done",
        )
        with self.assertRaises(ValidationError) as context:
            ticket.full_clean()
        self.assertIn("closed_at", context.exception.message_dict)

    def test_c1_valid_resolved_ticket_passes(self):
        """The happy path for a resolved ticket validates."""
        ticket = self._ticket(
            status=TicketStatusChoices.RESOLVED,
            resolved_at=timezone.now(),
            resolution="Replaced the optic.",
        )
        ticket.full_clean()

    def test_c1_valid_closed_ticket_passes(self):
        """The happy path for a closed ticket validates."""
        now = timezone.now()
        ticket = self._ticket(
            status=TicketStatusChoices.CLOSED,
            resolved_at=now,
            closed_at=now,
            resolution="Replaced the optic.",
        )
        ticket.full_clean()

    # --- C2: resolution agrees with status ---

    def test_c2_resolved_requires_resolution(self):
        """A resolved ticket must say how it was resolved."""
        ticket = self._ticket(status=TicketStatusChoices.RESOLVED, resolved_at=timezone.now())
        with self.assertRaises(ValidationError) as context:
            ticket.full_clean()
        self.assertIn("resolution", context.exception.message_dict)

    def test_c2_open_ticket_rejects_resolution(self):
        """An open ticket must not carry a stale resolution.

        This is the half that makes reopening safe: the service has to clear the resolution, and
        this rule is what proves it did.
        """
        ticket = self._ticket(status=TicketStatusChoices.IN_PROGRESS, resolution="left over")
        with self.assertRaises(ValidationError) as context:
            ticket.full_clean()
        self.assertIn("resolution", context.exception.message_dict)


class TestTicketUpdateValidation(TestCase):
    """C3: an update's payload and actor must match its type."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        cls.ticket = fixtures.create_ticket(user=cls.user)
        cls.location = fixtures.create_location()

    def _update(self, **overrides):
        """Build (without saving) an update with sane defaults."""
        values = {
            "ticket": self.ticket,
            "update_type": UpdateTypeChoices.COMMENT,
            "source": TicketSourceChoices.HUMAN,
            "user": self.user,
            "message": "a message",
        }
        values.update(overrides)
        return models.TicketUpdate(**values)

    def test_c3_comment_requires_message(self):
        """A comment with no message is invalid."""
        update = self._update(message="")
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("message", context.exception.message_dict)

    def test_c3_status_change_requires_both_statuses(self):
        """A status change must record where it came from and where it went."""
        update = self._update(update_type=UpdateTypeChoices.STATUS_CHANGE, message="")
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("from_status", context.exception.message_dict)
        self.assertIn("to_status", context.exception.message_dict)

    def test_c3_status_change_rejects_identical_statuses(self):
        """A status change must actually change the status."""
        update = self._update(
            update_type=UpdateTypeChoices.STATUS_CHANGE,
            from_status=TicketStatusChoices.NEW,
            to_status=TicketStatusChoices.NEW,
            message="",
        )
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("to_status", context.exception.message_dict)

    def test_c3_non_status_change_rejects_statuses(self):
        """Only a status change may carry status fields."""
        update = self._update(to_status=TicketStatusChoices.TRIAGED)
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("to_status", context.exception.message_dict)

    def test_c3_attachment_requires_object(self):
        """An attachment update must name the object it attached."""
        update = self._update(update_type=UpdateTypeChoices.OBJECT_ATTACHED, message="")
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("related_object_type", context.exception.message_dict)
        self.assertIn("related_object_id", context.exception.message_dict)

    def test_c3_non_attachment_rejects_object(self):
        """Only an attachment update may carry an object reference."""
        update = self._update(
            related_object_type=ContentType.objects.get_for_model(self.location),
            related_object_id=self.location.pk,
        )
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("related_object_type", context.exception.message_dict)

    def test_c3_human_requires_user(self):
        """A human update must record who acted."""
        update = self._update(source=TicketSourceChoices.HUMAN, user=None)
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("user", context.exception.message_dict)

    def test_c3_ai_rejects_user(self):
        """An AI update must not be recorded as though a person took it."""
        update = self._update(source=TicketSourceChoices.AI, user=self.user)
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("user", context.exception.message_dict)

    def test_c3_system_rejects_user(self):
        """A system update must not carry a user either."""
        update = self._update(source=TicketSourceChoices.SYSTEM, user=self.user)
        with self.assertRaises(ValidationError) as context:
            update.full_clean()
        self.assertIn("user", context.exception.message_dict)

    def test_c3_valid_comment_passes(self):
        """The happy path validates."""
        self._update().full_clean()


class TestTicketUpdateAppendOnly(TestCase):
    """The append-only guard on TicketUpdate."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        cls.ticket = fixtures.create_ticket(user=cls.user)

    def test_existing_update_cannot_be_saved(self):
        """Re-saving a persisted update raises."""
        update = self.ticket.updates.first()
        update.message = "tampered"
        with self.assertRaises(TicketUpdateImmutableError):
            update.save()

    def test_update_cannot_be_deleted(self):
        """Deleting an update raises."""
        update = self.ticket.updates.first()
        with self.assertRaises(TicketUpdateImmutableError):
            update.delete()

    def test_message_is_unchanged_in_the_database(self):
        """The failed save above must not have reached the database."""
        update = self.ticket.updates.first()
        original = update.message
        update.message = "tampered"
        with self.assertRaises(TicketUpdateImmutableError):
            update.save()
        update.refresh_from_db()
        self.assertEqual(update.message, original)

    def test_creating_a_new_update_still_works(self):
        """The guard must not block the service layer from appending."""
        before = self.ticket.updates.count()
        models.TicketUpdate.objects.create(
            ticket=self.ticket,
            update_type=UpdateTypeChoices.COMMENT,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
            message="a new row",
        )
        self.assertEqual(self.ticket.updates.count(), before + 1)

    def test_deleting_the_ticket_still_removes_updates(self):
        """Cascade deletion bypasses Model.delete(), which is the documented limitation."""
        ticket = fixtures.create_ticket(user=self.user, title="Doomed")
        update_ids = list(ticket.updates.values_list("pk", flat=True))
        self.assertTrue(update_ids)
        ticket.delete()
        self.assertFalse(models.TicketUpdate.objects.filter(pk__in=update_ids).exists())


class TestIngestionStats(TestCase):
    """The ingestion counter row."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.bucket = timezone.now().replace(second=0, microsecond=0)

    def _create(self, **kwargs):
        """Create one counter row with the given overrides."""
        defaults = {"consumer_name": "consumer-1", "topic": "network.events", "bucket_start": self.bucket}
        return models.IngestionStats.objects.create(**{**defaults, **kwargs})

    def test_counters_default_to_zero(self):
        """A fresh bucket has counted nothing."""
        stats = self._create()
        for field in ("received", "errored", "dropped", "tickets_opened", "tickets_joined", "suppressed"):
            self.assertEqual(getattr(stats, field), 0, field)
        self.assertEqual(stats.drops_by_reason, {})
        self.assertIsNone(stats.last_message_at)

    def test_str_names_the_consumer_topic_and_window(self):
        """A row stringifies as the three things that identify it."""
        stats = self._create()
        self.assertIn("consumer-1", str(stats))
        self.assertIn("network.events", str(stats))

    def test_accounted_for_sums_the_terminal_outcomes(self):
        """Every message ends in exactly one of four places, and suppressed is not one of them."""
        stats = self._create(received=10, errored=1, dropped=2, tickets_opened=4, tickets_joined=3, suppressed=2)
        self.assertEqual(stats.accounted_for, 10)
        self.assertEqual(stats.received, stats.accounted_for)

    def test_one_row_per_consumer_topic_and_bucket(self):
        """The unique constraint is what makes a flush able to find its row."""
        self._create()
        with self.assertRaises(IntegrityError):
            self._create()

    def test_the_same_bucket_for_a_different_consumer_is_a_different_row(self):
        """Two instances count into their own rows, so their flushes never contend."""
        self._create()
        self._create(consumer_name="consumer-2")
        self.assertEqual(models.IngestionStats.objects.count(), 2)

    def test_ordering_is_newest_bucket_first(self):
        """The list view answers 'what is happening now' without a sort."""
        older = self._create(bucket_start=self.bucket - timedelta(minutes=5))
        newer = self._create()
        self.assertEqual(list(models.IngestionStats.objects.all()), [newer, older])

    def test_stats_are_not_change_logged(self):
        """A row rewritten every few seconds must not fill the change log."""
        self.assertFalse(hasattr(self._create(), "to_objectchange"))


class TestLLMProvider(ModelTestCases.BaseModelTestCase):
    """The LLM provider registry entry."""

    model = models.LLMProvider

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        super().setUpTestData()
        fixtures.create_llmprovider(name="Provider One")
        fixtures.create_llmprovider(name="Provider Two")
        fixtures.create_llmprovider(name="Provider Three")

    def test_str(self):
        """A provider stringifies as its name."""
        self.assertEqual(str(fixtures.create_llmprovider(name="Stringify Me")), "Stringify Me")

    def test_an_openai_compatible_provider_needs_a_remote_url(self):
        """There is no default endpoint to fall back to for a self-hosted protocol."""
        integration = fixtures.create_external_integration(name="No URL", remote_url="")
        provider = models.LLMProvider(
            name="Missing Endpoint",
            provider_type=LLMProviderTypeChoices.OPENAI_COMPATIBLE,
            external_integration=integration,
        )
        with self.assertRaises(ValidationError) as raised:
            provider.full_clean()
        self.assertIn("external_integration", raised.exception.message_dict)

    def test_a_hosted_provider_needs_no_remote_url(self):
        """OpenAI and Anthropic have well-known endpoints litellm already knows."""
        integration = fixtures.create_external_integration(name="Hosted", remote_url="")
        provider = models.LLMProvider(
            name="Hosted Provider",
            provider_type=LLMProviderTypeChoices.OPENAI,
            external_integration=integration,
        )
        provider.full_clean()

    def test_protected_while_models_exist(self):
        """A provider with models cannot be deleted out from under them."""
        model = fixtures.create_llmmodel()
        with self.assertRaises(ProtectedError):
            model.provider.delete()


class TestLLMModel(ModelTestCases.BaseModelTestCase):
    """The LLM model registry entry."""

    model = models.LLMModel

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        super().setUpTestData()
        provider = fixtures.create_llmprovider()
        fixtures.create_llmmodel(name="model-one", provider=provider)
        fixtures.create_llmmodel(name="model-two", provider=provider)
        fixtures.create_llmmodel(name="model-three", provider=provider)

    def test_str_names_the_provider_and_the_model(self):
        """Two providers may offer the same model name, so both halves matter."""
        model = models.LLMModel.objects.get(name="model-one")
        self.assertEqual(str(model), "Test Provider: model-one")

    def test_one_name_per_provider(self):
        """The same name on the same provider is a duplicate; on another provider it is not."""
        provider = fixtures.create_llmprovider()
        with self.assertRaises(IntegrityError):
            models.LLMModel.objects.create(provider=provider, name="model-one")

    def test_the_same_name_on_another_provider_is_allowed(self):
        """A model name is only unique within its provider."""
        other = fixtures.create_llmprovider(name="Other Provider")
        models.LLMModel.objects.create(provider=other, name="model-one")
        self.assertEqual(models.LLMModel.objects.filter(name="model-one").count(), 2)

    def test_a_credential_in_the_default_parameters_is_refused(self):
        """Rule L3: a key here would be change-logged and served over REST and GraphQL."""
        model = models.LLMModel(
            provider=fixtures.create_llmprovider(),
            name="smuggler",
            default_parameters={"api_key": "sk-not-here", "temperature": 0.1},
        )
        with self.assertRaises(ValidationError) as raised:
            model.full_clean()
        self.assertIn("default_parameters", raised.exception.message_dict)
        self.assertIn("api_key", str(raised.exception))

    def test_the_calls_own_arguments_are_refused(self):
        """Passing `model` or `messages` here fails the call rather than configuring it."""
        model = models.LLMModel(
            provider=fixtures.create_llmprovider(),
            name="confuser",
            default_parameters={"model": "something-else", "messages": []},
        )
        with self.assertRaises(ValidationError) as raised:
            model.full_clean()
        # Both are named, so one edit fixes the object rather than one per attempt.
        self.assertIn("messages", str(raised.exception))
        self.assertIn("model", str(raised.exception))

    def test_ordinary_parameters_are_allowed(self):
        """The field's actual purpose still works."""
        model = models.LLMModel(
            provider=fixtures.create_llmprovider(),
            name="tuned",
            default_parameters={"temperature": 0.1, "top_p": 0.9},
        )
        model.full_clean()


class TestLLMUsageRecord(TestCase):
    """The accounting row. Constructed directly only here, as the guard tests allow."""

    def _record(self, **kwargs):
        """One usage record, built directly to exercise the model itself."""
        defaults = {
            "model": fixtures.create_llmmodel(),
            "purpose": LLMPurposeChoices.TRIAGE,
            "success": True,
        }
        record = models.LLMUsageRecord(**{**defaults, **kwargs})
        record.full_clean()
        record.save()
        return record

    def test_counters_default_to_zero(self):
        """A record carries zeros until the provider reports usage."""
        record = self._record()
        self.assertEqual(record.prompt_tokens, 0)
        self.assertEqual(record.completion_tokens, 0)
        self.assertEqual(record.cost, 0)
        self.assertEqual(record.latency_ms, 0)

    def test_ordering_is_newest_first(self):
        """The list view answers 'what just happened' without a sort."""
        older = self._record(called_at=timezone.now() - timedelta(minutes=5))
        newer = self._record()
        self.assertEqual(list(models.LLMUsageRecord.objects.all()), [newer, older])

    def test_a_deleted_ticket_leaves_the_spend_history(self):
        """The money was spent whether or not the ticket survived."""
        ticket = fixtures.create_ticket()
        record = self._record(ticket=ticket)
        ticket.delete()
        record.refresh_from_db()
        self.assertIsNone(record.ticket)

    def test_the_model_is_protected_while_records_exist(self):
        """Deleting a registry entry must not silently delete its accounting."""
        record = self._record()
        with self.assertRaises(ProtectedError):
            record.model.delete()

    def test_records_are_not_change_logged(self):
        """One ObjectChange per model call would bury the change log, as for IngestionStats."""
        self.assertFalse(hasattr(self._record(), "to_objectchange"))
