"""Test the Event Tracker models, their validation rules C1-C3, and the append-only guard."""

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.utils import timezone
from nautobot.apps.testing import ModelTestCases, TestCase

from nautobot_event_tracker import models
from nautobot_event_tracker.choices import (
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
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


class TestLLMUsageRecord(ModelTestCases.BaseModelTestCase):
    """The accounting row. Constructed directly only here, as the guard tests allow."""

    model = models.LLMUsageRecord

    @classmethod
    def setUpTestData(cls):
        """Three records, so the generic suite has a queryset to work through."""
        super().setUpTestData()
        llm_model = fixtures.create_llmmodel()
        for purpose in (LLMPurposeChoices.TRIAGE, LLMPurposeChoices.TRIAGE, LLMPurposeChoices.TRIAGE):
            models.LLMUsageRecord.objects.create(model=llm_model, purpose=purpose, success=True)

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
        ordered = models.LLMUsageRecord.objects.filter(pk__in=[older.pk, newer.pk])
        self.assertEqual(list(ordered), [newer, older])

    def test_a_deleted_ticket_leaves_the_spend_history(self):
        """The money was spent whether or not the ticket survived."""
        ticket = fixtures.create_ticket()
        record = self._record(ticket=ticket)
        ticket.delete()
        record.refresh_from_db()
        self.assertIsNone(record.ticket)

    def test_the_model_is_protected_while_records_exist(self):
        """Deleting a registry entry must not silently delete its accounting.

        pylint-django cannot resolve the cross-app foreign key, so the line keeps the `no-member` directive.
        """
        record = self._record(model=fixtures.create_llmmodel(name="Protected Model"))
        with self.assertRaises(ProtectedError):
            record.model.delete()  # pylint: disable=no-member

    def test_records_are_not_change_logged(self):
        """One ObjectChange per model call would bury the change log, as for IngestionStats."""
        self.assertFalse(hasattr(self._record(), "to_objectchange"))


class TestAgentRun(ModelTestCases.BaseModelTestCase):
    """The record of one agent run. Constructed directly only here and in the fixtures."""

    model = models.AgentRun

    @classmethod
    def setUpTestData(cls):
        """Three runs, so the generic suite has a queryset to work through."""
        super().setUpTestData()
        ticket = fixtures.create_ticket()
        for _ in range(3):
            fixtures.create_agentrun(ticket=ticket)

    def test_a_run_starts_running_with_an_empty_transcript(self):
        """A row exists from the first moment, so a killed worker still left something."""
        run = fixtures.create_agentrun()

        self.assertEqual(run.status, AgentRunStatusChoices.RUNNING)
        self.assertEqual(run.transcript, [])
        self.assertEqual(run.iterations, 0)
        self.assertTrue(run.is_live)

    def test_a_completed_run_is_not_live(self):
        """A9 reads this: only a live run holds a ticket."""
        run = fixtures.create_agentrun(status=AgentRunStatusChoices.COMPLETED)

        self.assertFalse(run.is_live)

    def test_a_run_waiting_for_approval_is_live(self):
        """The chain is not over: somebody still has a decision to make."""
        run = fixtures.create_agentrun(status=AgentRunStatusChoices.WAITING_APPROVAL)

        self.assertTrue(run.is_live)

    def test_a_deleted_ticket_takes_its_runs_with_it(self):
        """A run is about exactly one ticket and means nothing without it."""
        ticket = fixtures.create_ticket(title="Doomed")
        run = fixtures.create_agentrun(ticket=ticket)

        ticket.delete()

        self.assertFalse(models.AgentRun.objects.filter(pk=run.pk).exists())

    def test_a_deleted_user_leaves_the_run(self):
        """Who launched it is worth losing; the record of what happened is not."""
        user = fixtures.create_user(username="leaving")
        run = fixtures.create_agentrun(started_by=user)

        user.delete()
        run.refresh_from_db()

        self.assertIsNone(run.started_by)

    def test_runs_are_not_change_logged(self):
        """One ObjectChange per message appended would bury the change log."""
        self.assertFalse(hasattr(fixtures.create_agentrun(), "to_objectchange"))

    def test_ordering_is_newest_first(self):
        """The list view answers "what just happened" without a sort."""
        older = fixtures.create_agentrun(started_at=timezone.now() - timedelta(minutes=5))
        newer = fixtures.create_agentrun()

        ordered = models.AgentRun.objects.filter(pk__in=[older.pk, newer.pk])

        self.assertEqual(list(ordered), [newer, older])


class TestAgentToolCall(ModelTestCases.BaseModelTestCase):
    """The record of one tool call, and the two properties the gate reads."""

    model = models.AgentToolCall

    @classmethod
    def setUpTestData(cls):
        """Three calls, so the generic suite has a queryset to work through."""
        super().setUpTestData()
        run = fixtures.create_agentrun()
        server = fixtures.create_mcpserver()
        for name in ("call_one", "call_two", "call_three"):
            fixtures.create_agenttoolcall(run=run, tool=fixtures.create_mcptool(server=server, name=name))

    def test_str_names_the_tool_and_what_became_of_it(self):
        """The two things a person scanning a list is looking for."""
        call = fixtures.create_agenttoolcall()

        self.assertEqual(str(call), f"{call.tool} ({call.status})")

    def test_a_new_call_is_waiting(self):
        """Every call starts proposed, whether or not anybody has to decide it."""
        call = fixtures.create_agenttoolcall()

        self.assertTrue(call.awaits_decision)
        self.assertFalse(call.is_decided)

    def test_a_decided_call_is_not_waiting(self):
        """A call is decided once (7.3), and this is the check that says so."""
        call = fixtures.create_agenttoolcall(status=AgentToolCallStatusChoices.APPROVED)

        self.assertFalse(call.awaits_decision)
        self.assertTrue(call.is_decided)

    def test_an_executed_read_only_call_is_not_waiting_on_anybody(self):
        """13.4 - reading needs no approval, so an executed row has no decider."""
        call = fixtures.create_agenttoolcall(status=AgentToolCallStatusChoices.EXECUTED)

        self.assertIsNone(call.decided_by)
        self.assertTrue(call.is_decided)

    def test_the_tool_is_protected_while_calls_exist(self):
        """The record of a call outlives a tidy-up of the registry.

        pylint-django cannot resolve the cross-app foreign key, so the line keeps the `no-member` directive.
        """
        call = fixtures.create_agenttoolcall(tool=fixtures.create_mcptool(name="protected_tool"))

        with self.assertRaises(ProtectedError):
            call.tool.delete()  # pylint: disable=no-member

    def test_a_deleted_run_takes_its_calls_with_it(self):
        """A call means nothing without the run that asked for it."""
        run = fixtures.create_agentrun()
        call = fixtures.create_agenttoolcall(run=run)

        run.delete()

        self.assertFalse(models.AgentToolCall.objects.filter(pk=call.pk).exists())

    def test_calls_are_not_change_logged(self):
        """A record of what happened, like every other row of this kind."""
        self.assertFalse(hasattr(fixtures.create_agenttoolcall(), "to_objectchange"))


class TestTicketEmbedding(ModelTestCases.BaseModelTestCase):
    """The retrieval corpus row. Constructed directly only here and in the fixtures."""

    model = models.TicketEmbedding

    @classmethod
    def setUpTestData(cls):
        """Three embeddings on three closed tickets - one row per ticket, so three tickets."""
        super().setUpTestData()
        embedding_model = fixtures.create_embedding_model()
        for index in range(3):
            ticket = fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, title=f"Closed {index}")
            fixtures.create_ticketembedding(ticket=ticket, model=embedding_model)

    def test_one_embedding_per_ticket(self):
        """R4 held by the database rather than by the service remembering to."""
        row = fixtures.create_ticketembedding()

        with self.assertRaises(IntegrityError):
            models.TicketEmbedding.objects.create(
                ticket=row.ticket,
                embedding=[1.0, 0.0, 0.0],
                document="a second one",
                model=row.model,
                dimensions=3,
                document_fingerprint="x",
            )

    def test_a_deleted_ticket_takes_its_embedding_with_it(self):
        """A vector of a ticket that no longer exists is not worth keeping."""
        row = fixtures.create_ticketembedding()
        ticket = row.ticket

        ticket.delete()

        self.assertFalse(models.TicketEmbedding.objects.filter(pk=row.pk).exists())

    def test_the_model_is_protected_while_embeddings_exist(self):
        """Tidying the registry must not silently orphan the corpus.

        pylint-django cannot resolve the cross-app foreign key, so the line keeps the `no-member` directive.
        """
        row = fixtures.create_ticketembedding(model=fixtures.create_embedding_model(name="protected-embed"))

        with self.assertRaises(ProtectedError):
            row.model.delete()  # pylint: disable=no-member

    def test_the_vector_round_trips(self):
        """pgvector is doing real work here, not storing a string."""
        row = fixtures.create_ticketembedding(vector=[0.5, -0.25, 0.125])
        row.refresh_from_db()

        # pylint cannot see through `VectorField` to know its value is an array, so it reads this
        # as iterating something non-iterable. It is a numpy array at runtime.
        stored = [round(float(value), 3) for value in row.embedding]  # pylint: disable=not-an-iterable

        self.assertEqual(stored, [0.5, -0.25, 0.125])

    def test_embeddings_are_not_change_logged(self):
        """Re-indexing would otherwise file an ObjectChange recording that a number changed."""
        self.assertFalse(hasattr(fixtures.create_ticketembedding(), "to_objectchange"))
