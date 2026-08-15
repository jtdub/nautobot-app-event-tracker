"""Test the choice sets and the workflow graph itself.

These assertions are about the shape of the graph, independent of any ticket. They exist so that a
typo in `TICKET_STATUS_TRANSITIONS` fails at test time rather than stranding a ticket in
production.
"""

from django.test import SimpleTestCase

from nautobot_event_tracker.choices import (
    SEVERITY_WEIGHTS,
    TERMINAL_STATUSES,
    TICKET_STATUS_TRANSITIONS,
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)

#: The number of legal edges in the graph, written out here so that accidentally adding or removing
#: an edge fails loudly rather than passing a self-referential count.
EXPECTED_LEGAL_EDGE_COUNT = 14


class TicketStatusGraphTest(SimpleTestCase):
    """Structural assertions about the ticket status workflow graph."""

    def test_every_status_has_an_entry(self):
        """Every status must appear as a key, or a ticket could reach a state with no exits."""
        self.assertEqual(set(TICKET_STATUS_TRANSITIONS), set(TicketStatusChoices.values()))

    def test_every_target_is_a_valid_status(self):
        """A typo in a target would create an unreachable state; catch it here."""
        valid = set(TicketStatusChoices.values())
        for source, targets in TICKET_STATUS_TRANSITIONS.items():
            for target in targets:
                self.assertIn(target, valid, f"'{source}' points at unknown status '{target}'")

    def test_closed_is_terminal(self):
        """Nothing leaves closed. The AI immutability rule depends on this."""
        self.assertEqual(TICKET_STATUS_TRANSITIONS[TicketStatusChoices.CLOSED], frozenset())

    def test_no_self_edges(self):
        """Transitioning to the status a ticket already holds is never legal."""
        for source, targets in TICKET_STATUS_TRANSITIONS.items():
            self.assertNotIn(source, targets, f"'{source}' has a self-edge")

    def test_legal_edge_count(self):
        """The graph has exactly the number of edges the spec describes."""
        total = sum(len(targets) for targets in TICKET_STATUS_TRANSITIONS.values())
        self.assertEqual(total, EXPECTED_LEGAL_EDGE_COUNT)

    def test_every_status_except_new_is_reachable(self):
        """Every state other than the entry state must be reachable from somewhere."""
        reachable = set()
        for targets in TICKET_STATUS_TRANSITIONS.values():
            reachable |= set(targets)
        expected = set(TicketStatusChoices.values()) - {TicketStatusChoices.NEW}
        self.assertEqual(reachable, expected)

    def test_terminal_statuses(self):
        """The AI immutability rule keys off exactly resolved and closed."""
        self.assertEqual(
            TERMINAL_STATUSES,
            frozenset({TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED}),
        )

    def test_reopen_path_exists(self):
        """A resolved ticket can be reopened; a closed one cannot."""
        self.assertIn(TicketStatusChoices.IN_PROGRESS, TICKET_STATUS_TRANSITIONS[TicketStatusChoices.RESOLVED])
        self.assertEqual(len(TICKET_STATUS_TRANSITIONS[TicketStatusChoices.CLOSED]), 0)


class ChoiceSetTest(SimpleTestCase):
    """Assertions about the remaining choice sets."""

    def test_severity_weights_cover_every_severity(self):
        """Ordering must be defined for every severity, not most of them."""
        self.assertEqual(set(SEVERITY_WEIGHTS), set(SeverityChoices.values()))

    def test_severity_weights_are_strictly_ordered(self):
        """Weights must descend from critical to info with no ties."""
        ordered = [
            SeverityChoices.CRITICAL,
            SeverityChoices.MAJOR,
            SeverityChoices.MINOR,
            SeverityChoices.WARNING,
            SeverityChoices.INFO,
        ]
        weights = [SEVERITY_WEIGHTS[severity] for severity in ordered]
        self.assertEqual(weights, sorted(weights, reverse=True))
        self.assertEqual(len(set(weights)), len(weights))

    def test_source_choices(self):
        """The three actor kinds the service layer distinguishes."""
        self.assertEqual(set(TicketSourceChoices.values()), {"human", "ai", "system"})

    def test_update_type_choices(self):
        """Every update type the service layer can write."""
        self.assertEqual(
            set(UpdateTypeChoices.values()),
            {
                "created",
                "comment",
                "status_change",
                "assignment",
                "severity_change",
                "object_attached",
                "object_detached",
                "recurrence",
            },
        )
