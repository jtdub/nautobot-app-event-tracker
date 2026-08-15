"""Test the ticket service layer: the transition matrix and rules S1-S5."""

import uuid
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from nautobot_event_tracker.choices import (
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.models import EventTicket, EventType, TicketUpdate
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import (
    InvalidActorError,
    InvalidTransitionError,
    TicketImmutableError,
)
from nautobot_event_tracker.tests import fixtures

NEW = TicketStatusChoices.NEW
TRIAGED = TicketStatusChoices.TRIAGED
IN_PROGRESS = TicketStatusChoices.IN_PROGRESS
SUPPRESSED = TicketStatusChoices.SUPPRESSED
RESOLVED = TicketStatusChoices.RESOLVED
CLOSED = TicketStatusChoices.CLOSED

ALL_STATUSES = [NEW, TRIAGED, IN_PROGRESS, SUPPRESSED, RESOLVED, CLOSED]

#: The complete transition matrix, written out longhand rather than derived from
#: TICKET_STATUS_TRANSITIONS. Deriving it from the same map the service reads would make this test
#: incapable of catching a wrong map, which is the only thing it is here to catch.
LEGAL_TRANSITIONS = {
    (NEW, TRIAGED),
    (NEW, SUPPRESSED),
    (NEW, CLOSED),
    (TRIAGED, IN_PROGRESS),
    (TRIAGED, SUPPRESSED),
    (TRIAGED, RESOLVED),
    (TRIAGED, CLOSED),
    (IN_PROGRESS, TRIAGED),
    (IN_PROGRESS, RESOLVED),
    (IN_PROGRESS, CLOSED),
    (SUPPRESSED, TRIAGED),
    (SUPPRESSED, CLOSED),
    (RESOLVED, IN_PROGRESS),
    (RESOLVED, CLOSED),
}


class TransitionMatrixTest(TestCase):
    """S2: every one of the 36 ordered status pairs behaves as specified."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        fixtures.create_event_types()

    def test_matrix_size(self):
        """Guard the matrix itself: 6 states, 36 pairs, 14 of them legal."""
        self.assertEqual(len(ALL_STATUSES), 6)
        self.assertEqual(len(ALL_STATUSES) ** 2, 36)
        self.assertEqual(len(LEGAL_TRANSITIONS), 14)

    def test_every_ordered_pair(self):
        """Walk all 36 pairs, asserting permitted or rejected per the matrix."""
        for from_status in ALL_STATUSES:
            for to_status in ALL_STATUSES:
                with self.subTest(f"{from_status} -> {to_status}"):
                    ticket = fixtures.create_ticket_in_status(from_status, user=self.user)
                    self.assertEqual(ticket.status, from_status, "fixture did not reach the starting status")

                    should_succeed = (from_status, to_status) in LEGAL_TRANSITIONS
                    if should_succeed:
                        update = ticket_service.transition(
                            ticket=ticket,
                            to_status=to_status,
                            source=TicketSourceChoices.HUMAN,
                            user=self.user,
                            resolution="Resolved in tests." if to_status == RESOLVED else "",
                        )
                        ticket.refresh_from_db()
                        self.assertEqual(ticket.status, to_status)
                        self.assertEqual(update.update_type, UpdateTypeChoices.STATUS_CHANGE)
                        self.assertEqual(update.from_status, from_status)
                        self.assertEqual(update.to_status, to_status)
                    else:
                        with self.assertRaises(InvalidTransitionError):
                            ticket_service.transition(
                                ticket=ticket,
                                to_status=to_status,
                                source=TicketSourceChoices.HUMAN,
                                user=self.user,
                                resolution="Resolved in tests." if to_status == RESOLVED else "",
                            )
                        ticket.refresh_from_db()
                        self.assertEqual(ticket.status, from_status, "a rejected transition changed the status")

    def test_self_transitions_all_rejected(self):
        """Every status refuses a move to itself."""
        for status in ALL_STATUSES:
            with self.subTest(status):
                ticket = fixtures.create_ticket_in_status(status, user=self.user)
                with self.assertRaises(InvalidTransitionError):
                    ticket_service.transition(
                        ticket=ticket,
                        to_status=status,
                        source=TicketSourceChoices.HUMAN,
                        user=self.user,
                        resolution="x",
                    )

    def test_get_allowed_transitions_matches_the_matrix(self):
        """The set the UI and API read must equal the matrix row."""
        for status in ALL_STATUSES:
            with self.subTest(status):
                ticket = fixtures.create_ticket_in_status(status, user=self.user)
                expected = {to for (frm, to) in LEGAL_TRANSITIONS if frm == status}
                self.assertEqual(set(ticket_service.get_allowed_transitions(ticket)), expected)

    def test_get_allowed_transitions_is_empty_for_ai_on_terminal(self):
        """An AI actor is offered nothing on a resolved or closed ticket."""
        for status in (RESOLVED, CLOSED):
            with self.subTest(status):
                ticket = fixtures.create_ticket_in_status(status, user=self.user)
                self.assertEqual(
                    ticket_service.get_allowed_transitions(ticket, source=TicketSourceChoices.AI),
                    frozenset(),
                )


class TransitionSideEffectsTest(TestCase):
    """S2: timestamp and resolution bookkeeping around terminal states."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        fixtures.create_event_types()

    def test_resolving_stamps_time_and_resolution(self):
        """Entering resolved records when and how."""
        ticket = fixtures.create_ticket_in_status(TRIAGED, user=self.user)
        ticket_service.transition(
            ticket=ticket,
            to_status=RESOLVED,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
            resolution="Replaced the optic.",
        )
        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.resolved_at)
        self.assertIsNone(ticket.closed_at)
        self.assertEqual(ticket.resolution, "Replaced the optic.")

    def test_resolving_requires_a_resolution(self):
        """Resolving without saying how is rejected."""
        ticket = fixtures.create_ticket_in_status(TRIAGED, user=self.user)
        with self.assertRaises(ValidationError):
            ticket_service.transition(
                ticket=ticket,
                to_status=RESOLVED,
                source=TicketSourceChoices.HUMAN,
                user=self.user,
            )
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TRIAGED)

    def test_closing_stamps_both_timestamps(self):
        """Closing straight from new backfills resolved_at so C1 holds."""
        ticket = fixtures.create_ticket_in_status(NEW, user=self.user)
        ticket_service.transition(
            ticket=ticket,
            to_status=CLOSED,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.resolved_at)
        self.assertIsNotNone(ticket.closed_at)
        self.assertTrue(ticket.resolution, "closing must leave a resolution so C2 holds")

    def test_reopening_clears_terminal_bookkeeping(self):
        """Reopening a resolved ticket wipes the timestamps and the resolution."""
        ticket = fixtures.create_ticket_in_status(RESOLVED, user=self.user)
        self.assertIsNotNone(ticket.resolved_at)

        ticket_service.transition(
            ticket=ticket,
            to_status=IN_PROGRESS,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, IN_PROGRESS)
        self.assertIsNone(ticket.resolved_at)
        self.assertIsNone(ticket.closed_at)
        self.assertEqual(ticket.resolution, "")


class AIImmutabilityTest(TestCase):
    """S3: an AI actor may not mutate a resolved or closed ticket."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        fixtures.create_event_types()
        cls.location = fixtures.create_location()

    def _terminal_tickets(self):
        """Yield one ticket in each terminal status."""
        for status in (RESOLVED, CLOSED):
            yield status, fixtures.create_ticket_in_status(status, user=self.user)

    def test_ai_cannot_comment(self):
        """Comments are blocked."""
        for status, ticket in self._terminal_tickets():
            with self.subTest(status), self.assertRaises(TicketImmutableError):
                ticket_service.add_comment(ticket=ticket, message="hi", source=TicketSourceChoices.AI)

    def test_ai_cannot_transition(self):
        """Transitions are blocked, including otherwise-legal ones."""
        for status, ticket in self._terminal_tickets():
            with self.subTest(status), self.assertRaises(TicketImmutableError):
                ticket_service.transition(ticket=ticket, to_status=CLOSED, source=TicketSourceChoices.AI)

    def test_ai_cannot_attach(self):
        """Object attachment is blocked."""
        for status, ticket in self._terminal_tickets():
            with self.subTest(status), self.assertRaises(TicketImmutableError):
                ticket_service.attach_object(ticket=ticket, obj=self.location, source=TicketSourceChoices.AI)

    def test_ai_cannot_detach(self):
        """Object detachment is blocked."""
        for status, ticket in self._terminal_tickets():
            with self.subTest(status), self.assertRaises(TicketImmutableError):
                ticket_service.detach_object(ticket=ticket, obj=self.location, source=TicketSourceChoices.AI)

    def test_ai_cannot_assign(self):
        """Assignment is blocked."""
        for status, ticket in self._terminal_tickets():
            with self.subTest(status), self.assertRaises(TicketImmutableError):
                ticket_service.assign(ticket=ticket, assignee=self.user, source=TicketSourceChoices.AI)

    def test_ai_cannot_change_severity(self):
        """Severity changes are blocked."""
        for status, ticket in self._terminal_tickets():
            with self.subTest(status), self.assertRaises(TicketImmutableError):
                ticket_service.set_severity(ticket=ticket, severity=SeverityChoices.INFO, source=TicketSourceChoices.AI)

    def test_immutability_takes_precedence_over_invalid_transition(self):
        """An AI move that is also illegal reports immutability, not illegality.

        The precedence is observable, so it is pinned here rather than left to chance.
        """
        ticket = fixtures.create_ticket_in_status(CLOSED, user=self.user)
        # closed -> triaged is not an edge in the graph, and the source is AI. S3 must win.
        with self.assertRaises(TicketImmutableError):
            ticket_service.transition(ticket=ticket, to_status=TRIAGED, source=TicketSourceChoices.AI)

    def test_immutability_takes_precedence_over_invalid_actor(self):
        """An AI call that also violates S4 still reports immutability."""
        ticket = fixtures.create_ticket_in_status(CLOSED, user=self.user)
        with self.assertRaises(TicketImmutableError):
            # user is not allowed with source=ai, but S3 is checked first.
            ticket_service.add_comment(ticket=ticket, message="hi", source=TicketSourceChoices.AI, user=self.user)

    def test_ai_may_act_on_open_tickets(self):
        """The rule is about terminal states only, not about AI generally."""
        ticket = fixtures.create_ticket_in_status(TRIAGED, user=self.user)
        update = ticket_service.add_comment(ticket=ticket, message="AI triage note", source=TicketSourceChoices.AI)
        self.assertEqual(update.source, TicketSourceChoices.AI)
        self.assertIsNone(update.user)

    def test_humans_may_still_act_on_terminal_tickets(self):
        """People can comment on closed tickets and reopen resolved ones."""
        closed = fixtures.create_ticket_in_status(CLOSED, user=self.user)
        ticket_service.add_comment(
            ticket=closed, message="post-mortem note", source=TicketSourceChoices.HUMAN, user=self.user
        )

        resolved = fixtures.create_ticket_in_status(RESOLVED, user=self.user)
        ticket_service.transition(
            ticket=resolved, to_status=IN_PROGRESS, source=TicketSourceChoices.HUMAN, user=self.user
        )
        resolved.refresh_from_db()
        self.assertEqual(resolved.status, IN_PROGRESS)

    def test_system_may_act_on_terminal_tickets(self):
        """Deterministic automation is not restricted the way AI is."""
        ticket = fixtures.create_ticket_in_status(CLOSED, user=self.user)
        update = ticket_service.add_comment(ticket=ticket, message="system note", source=TicketSourceChoices.SYSTEM)
        self.assertEqual(update.source, TicketSourceChoices.SYSTEM)


class ActorBindingTest(TestCase):
    """S4: source and user must agree."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        fixtures.create_event_types()

    def test_human_without_user_is_rejected(self):
        """A human action must record who took it."""
        ticket = fixtures.create_ticket(user=self.user)
        with self.assertRaises(InvalidActorError):
            ticket_service.add_comment(ticket=ticket, message="x", source=TicketSourceChoices.HUMAN)

    def test_ai_with_user_is_rejected(self):
        """An AI action must not be attributed to a person."""
        ticket = fixtures.create_ticket(user=self.user)
        with self.assertRaises(InvalidActorError):
            ticket_service.add_comment(ticket=ticket, message="x", source=TicketSourceChoices.AI, user=self.user)

    def test_system_with_user_is_rejected(self):
        """Nor must a system action."""
        ticket = fixtures.create_ticket(user=self.user)
        with self.assertRaises(InvalidActorError):
            ticket_service.add_comment(ticket=ticket, message="x", source=TicketSourceChoices.SYSTEM, user=self.user)

    def test_human_with_user_is_accepted(self):
        """The three valid combinations all work."""
        ticket = fixtures.create_ticket(user=self.user)
        ticket_service.add_comment(ticket=ticket, message="x", source=TicketSourceChoices.HUMAN, user=self.user)
        ticket_service.add_comment(ticket=ticket, message="y", source=TicketSourceChoices.AI)
        ticket_service.add_comment(ticket=ticket, message="z", source=TicketSourceChoices.SYSTEM)
        self.assertEqual(ticket.updates.filter(update_type=UpdateTypeChoices.COMMENT).count(), 3)

    def test_unknown_source_is_rejected(self):
        """A source outside the choice set is not silently accepted."""
        ticket = fixtures.create_ticket(user=self.user)
        with self.assertRaises(InvalidActorError):
            ticket_service.add_comment(ticket=ticket, message="x", source="robot")


class AtomicityAndTrailTest(TestCase):
    """S1: one transaction, one update row, no partial writes."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        fixtures.create_event_types()
        cls.location = fixtures.create_location()

    def test_creation_writes_a_created_update(self):
        """A new ticket arrives with its first trail entry."""
        ticket = fixtures.create_ticket(user=self.user)
        updates = list(ticket.updates.all())
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].update_type, UpdateTypeChoices.CREATED)

    def test_each_mutation_writes_exactly_one_update(self):
        """Every mutating call adds one row and no more."""
        ticket = fixtures.create_ticket(user=self.user)
        actions = [
            lambda: ticket_service.add_comment(
                ticket=ticket, message="c", source=TicketSourceChoices.HUMAN, user=self.user
            ),
            lambda: ticket_service.transition(
                ticket=ticket, to_status=TRIAGED, source=TicketSourceChoices.HUMAN, user=self.user
            ),
            lambda: ticket_service.assign(
                ticket=ticket, assignee=self.user, source=TicketSourceChoices.HUMAN, user=self.user
            ),
            lambda: ticket_service.set_severity(
                ticket=ticket,
                severity=SeverityChoices.CRITICAL,
                source=TicketSourceChoices.HUMAN,
                user=self.user,
            ),
            lambda: ticket_service.attach_object(
                ticket=ticket, obj=self.location, source=TicketSourceChoices.HUMAN, user=self.user
            ),
            lambda: ticket_service.detach_object(
                ticket=ticket, obj=self.location, source=TicketSourceChoices.HUMAN, user=self.user
            ),
        ]
        for action in actions:
            before = ticket.updates.count()
            action()
            self.assertEqual(ticket.updates.count(), before + 1)

    def test_failed_update_rolls_back_the_ticket_change(self):
        """If the trail entry cannot be written, the ticket change is undone too."""
        ticket = fixtures.create_ticket(user=self.user)
        original_status = ticket.status

        with mock.patch.object(TicketUpdate, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ticket_service.transition(
                    ticket=ticket, to_status=TRIAGED, source=TicketSourceChoices.HUMAN, user=self.user
                )

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, original_status)
        self.assertEqual(ticket.updates.count(), 1)

    def test_no_op_actions_write_nothing(self):
        """Unchanged assignment and severity leave the trail alone."""
        ticket = fixtures.create_ticket(user=self.user)
        before = ticket.updates.count()

        self.assertIsNone(
            ticket_service.assign(ticket=ticket, assignee=None, source=TicketSourceChoices.HUMAN, user=self.user)
        )
        self.assertIsNone(
            ticket_service.set_severity(
                ticket=ticket,
                severity=ticket.severity,
                source=TicketSourceChoices.HUMAN,
                user=self.user,
            )
        )
        self.assertEqual(ticket.updates.count(), before)


class AttachmentTest(TestCase):
    """Attachment behaviour and the allowlist."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        fixtures.create_event_types()
        cls.location = fixtures.create_location()

    def test_attach_then_detach_then_reattach(self):
        """The derived set follows the trail through a full cycle."""
        ticket = fixtures.create_ticket(user=self.user)
        human = {"source": TicketSourceChoices.HUMAN, "user": self.user}

        ticket_service.attach_object(ticket=ticket, obj=self.location, **human)
        self.assertEqual(list(ticket_service.get_related_objects(ticket).values()), [[self.location]])

        ticket_service.detach_object(ticket=ticket, obj=self.location, **human)
        self.assertEqual(ticket_service.get_related_objects(ticket), {})

        ticket_service.attach_object(ticket=ticket, obj=self.location, **human)
        self.assertEqual(list(ticket_service.get_related_objects(ticket).values()), [[self.location]])

        # Three rows written, nothing deleted: the history is intact.
        self.assertEqual(
            ticket.updates.filter(
                update_type__in=[UpdateTypeChoices.OBJECT_ATTACHED, UpdateTypeChoices.OBJECT_DETACHED]
            ).count(),
            3,
        )

    def test_attaching_twice_is_a_no_op(self):
        """A re-delivered event must not pollute the timeline."""
        ticket = fixtures.create_ticket(user=self.user)
        human = {"source": TicketSourceChoices.HUMAN, "user": self.user}
        ticket_service.attach_object(ticket=ticket, obj=self.location, **human)
        before = ticket.updates.count()
        self.assertIsNone(ticket_service.attach_object(ticket=ticket, obj=self.location, **human))
        self.assertEqual(ticket.updates.count(), before)

    def test_detaching_what_is_not_attached_is_a_no_op(self):
        """Detaching an unattached object writes nothing."""
        ticket = fixtures.create_ticket(user=self.user)
        before = ticket.updates.count()
        self.assertIsNone(
            ticket_service.detach_object(
                ticket=ticket, obj=self.location, source=TicketSourceChoices.HUMAN, user=self.user
            )
        )
        self.assertEqual(ticket.updates.count(), before)

    def test_disallowed_type_is_rejected(self):
        """A type outside the allowlist cannot be attached."""
        ticket = fixtures.create_ticket(user=self.user)
        event_type = EventType.objects.first()
        with self.assertRaises(ValidationError):
            ticket_service.attach_object(
                ticket=ticket, obj=event_type, source=TicketSourceChoices.HUMAN, user=self.user
            )

    def test_detach_ignores_the_allowlist(self):
        """An object attached under an older configuration stays removable."""
        ticket = fixtures.create_ticket(user=self.user)
        human = {"source": TicketSourceChoices.HUMAN, "user": self.user}
        ticket_service.attach_object(ticket=ticket, obj=self.location, **human)

        with mock.patch.object(ticket_service, "get_attachable_object_types", return_value=[]):
            update = ticket_service.detach_object(ticket=ticket, obj=self.location, **human)
        self.assertIsNotNone(update)
        self.assertEqual(ticket_service.get_related_objects(ticket), {})

    def test_related_objects_skips_deleted_targets(self):
        """A dangling pointer is skipped rather than raising."""
        ticket = fixtures.create_ticket(user=self.user)
        ticket_service.attach_object(ticket=ticket, obj=self.location, source=TicketSourceChoices.HUMAN, user=self.user)
        self.location.delete()
        self.assertEqual(ticket_service.get_related_objects(ticket), {})

    def test_create_with_related_objects(self):
        """Objects passed at creation are attached with their own updates."""
        ticket = fixtures.create_ticket(user=self.user, related_objects=[self.location])
        self.assertEqual(list(ticket_service.get_related_objects(ticket).values()), [[self.location]])
        self.assertEqual(ticket.updates.filter(update_type=UpdateTypeChoices.OBJECT_ATTACHED).count(), 1)


class DedupTest(TestCase):
    """S5: creation with a dedup key joins an open ticket instead of duplicating it."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        fixtures.create_event_types()

    def test_recurrence_joins_the_open_ticket(self):
        """A second event with the same key bumps the count instead of opening a ticket."""
        first = fixtures.create_ticket(user=self.user, dedup_key="if-down-ar1-gi0/0/1")
        second = fixtures.create_ticket(user=self.user, dedup_key="if-down-ar1-gi0/0/1")

        self.assertEqual(first.pk, second.pk)
        second.refresh_from_db()
        self.assertEqual(second.event_count, 2)
        self.assertEqual(EventTicket.objects.filter(dedup_key="if-down-ar1-gi0/0/1").count(), 1)
        self.assertEqual(second.updates.filter(update_type=UpdateTypeChoices.RECURRENCE).count(), 1)

    def test_recurrence_advances_last_seen(self):
        """last_seen tracks the most recent occurrence."""
        first = fixtures.create_ticket(user=self.user, dedup_key="key-1")
        later = timezone.now() + timezone.timedelta(hours=1)
        fixtures.create_ticket(user=self.user, dedup_key="key-1", occurred_at=later)
        first.refresh_from_db()
        self.assertEqual(first.last_seen, later)

    def test_last_seen_never_moves_backwards(self):
        """Out-of-order delivery must not rewind last_seen; at-least-once makes this normal."""
        first = fixtures.create_ticket(user=self.user, dedup_key="key-2")
        original_last_seen = first.last_seen
        earlier = original_last_seen - timezone.timedelta(hours=1)
        fixtures.create_ticket(user=self.user, dedup_key="key-2", occurred_at=earlier)
        first.refresh_from_db()
        self.assertEqual(first.last_seen, original_last_seen)

    def test_recurrence_after_resolution_opens_a_new_ticket(self):
        """A recurrence after a fix is a new problem, not a continuation."""
        first = fixtures.create_ticket_in_status(RESOLVED, user=self.user, dedup_key="key-3")
        second = fixtures.create_ticket(user=self.user, dedup_key="key-3")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(EventTicket.objects.filter(dedup_key="key-3").count(), 2)

    def test_recurrence_after_close_opens_a_new_ticket(self):
        """Same for a closed ticket."""
        first = fixtures.create_ticket_in_status(CLOSED, user=self.user, dedup_key="key-4")
        second = fixtures.create_ticket(user=self.user, dedup_key="key-4")
        self.assertNotEqual(first.pk, second.pk)

    def test_empty_dedup_key_disables_the_behaviour(self):
        """Without a key, every event opens its own ticket."""
        first = fixtures.create_ticket(user=self.user)
        second = fixtures.create_ticket(user=self.user)
        self.assertNotEqual(first.pk, second.pk)

    def test_was_created_distinguishes_a_new_ticket_from_a_recurrence(self):
        """Callers can tell whether they opened the ticket or joined one."""
        first = fixtures.create_ticket(user=self.user, dedup_key="key-5")
        second = fixtures.create_ticket(user=self.user, dedup_key="key-5")
        self.assertTrue(first.was_created)
        self.assertFalse(second.was_created)

    def test_recurrence_attaches_newly_implicated_objects(self):
        """A later occurrence can name objects the first one did not."""
        location = fixtures.create_location(name="Recurrence Location")
        fixtures.create_ticket(user=self.user, dedup_key="key-6")
        joined = fixtures.create_ticket(user=self.user, dedup_key="key-6", related_objects=[location])
        self.assertEqual(list(ticket_service.get_related_objects(joined).values()), [[location]])

    def test_a_chosen_primary_key_is_honoured(self):
        """A caller may supply the ticket's ID; the REST API passes one through."""
        chosen = uuid.uuid4()
        ticket = fixtures.create_ticket(user=self.user, pk=chosen)
        self.assertEqual(ticket.pk, chosen)

    def test_disabled_event_type_cannot_open_a_ticket(self):
        """A disabled type is refused at creation."""
        disabled = EventType.objects.get(name="Test Disabled Type")
        with self.assertRaises(ValidationError):
            fixtures.create_ticket(user=self.user, event_type=disabled)


class TestCreateTicketForUserOnADedupJoin(TestCase):
    """A join hands back somebody else's ticket, and must not rewrite it."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.user = fixtures.create_user()
        cls.other = fixtures.create_user(username="second-caller")
        cls.event_type = fixtures.create_event_types()[0]

    def _create(self, **kwargs):
        """Create through the human-facing helper both transports use."""
        defaults = {
            "user": self.user,
            "title": "Interface down",
            "event_type": self.event_type,
            "dedup_key": "join-me",
        }
        return ticket_service.create_ticket_for_user(**{**defaults, **kwargs})

    def test_a_join_keeps_the_original_assignee(self):
        """The ticket belongs to an earlier event, and somebody may be working it."""
        first = self._create(assignee=self.user)
        joined = self._create(assignee=self.other)
        self.assertEqual(first.pk, joined.pk)
        joined.refresh_from_db()
        self.assertEqual(joined.assigned_to, self.user)

    def test_a_join_writes_no_assignment_update(self):
        """An assignment that did not happen must not appear in the trail."""
        ticket = self._create(assignee=self.user)
        self._create(assignee=self.other)
        ticket.refresh_from_db()
        self.assertEqual(ticket.updates.filter(update_type=UpdateTypeChoices.ASSIGNMENT).count(), 1)

    def test_a_join_keeps_the_original_tags(self):
        """Same argument: the tags belong to whoever opened it."""
        first = self._create(tags=["first-tag"])
        self._create(tags=["second-tag"])
        first.refresh_from_db()
        self.assertEqual([tag.name for tag in first.tags.all()], ["first-tag"])

    def test_a_new_ticket_still_gets_its_assignee_and_tags(self):
        """The guard must not stop the ordinary case from working."""
        ticket = self._create(assignee=self.other, tags=["a-tag"], dedup_key="")
        self.assertEqual(ticket.assigned_to, self.other)
        self.assertEqual([tag.name for tag in ticket.tags.all()], ["a-tag"])
