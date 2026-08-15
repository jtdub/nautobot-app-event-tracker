"""Choice sets and the ticket status workflow graph for nautobot_event_tracker."""

from nautobot.apps.choices import ChoiceSet


class TicketStatusChoices(ChoiceSet):
    """Lifecycle states of an EventTicket.

    Defined in code rather than as Nautobot `Status` objects: the transition graph below and the
    AI immutability rule both key off specific states, which must not be renameable or deletable
    by an administrator. See ADR 0002.
    """

    NEW = "new"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    SUPPRESSED = "suppressed"
    RESOLVED = "resolved"
    CLOSED = "closed"

    CHOICES = (
        (NEW, "New"),
        (TRIAGED, "Triaged"),
        (IN_PROGRESS, "In Progress"),
        (SUPPRESSED, "Suppressed"),
        (RESOLVED, "Resolved"),
        (CLOSED, "Closed"),
    )


class SeverityChoices(ChoiceSet):
    """Severity of the underlying network event, ordered most to least severe."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    WARNING = "warning"
    INFO = "info"

    CHOICES = (
        (CRITICAL, "Critical"),
        (MAJOR, "Major"),
        (MINOR, "Minor"),
        (WARNING, "Warning"),
        (INFO, "Info"),
    )


class TicketSourceChoices(ChoiceSet):
    """The kind of actor responsible for a ticket or an update to one."""

    HUMAN = "human"
    AI = "ai"
    SYSTEM = "system"

    CHOICES = (
        (HUMAN, "Human"),
        (AI, "AI"),
        (SYSTEM, "System"),
    )


class UpdateTypeChoices(ChoiceSet):
    """The kind of change a TicketUpdate records."""

    CREATED = "created"
    COMMENT = "comment"
    STATUS_CHANGE = "status_change"
    ASSIGNMENT = "assignment"
    SEVERITY_CHANGE = "severity_change"
    OBJECT_ATTACHED = "object_attached"
    OBJECT_DETACHED = "object_detached"
    RECURRENCE = "recurrence"

    CHOICES = (
        (CREATED, "Created"),
        (COMMENT, "Comment"),
        (STATUS_CHANGE, "Status Change"),
        (ASSIGNMENT, "Assignment"),
        (SEVERITY_CHANGE, "Severity Change"),
        (OBJECT_ATTACHED, "Object Attached"),
        (OBJECT_DETACHED, "Object Detached"),
        (RECURRENCE, "Recurrence"),
    )


#: Numeric weights for severity, so that ordering and comparison do not depend on alphabetical
#: accident. Higher is more severe.
SEVERITY_WEIGHTS = {
    SeverityChoices.CRITICAL: 50,
    SeverityChoices.MAJOR: 40,
    SeverityChoices.MINOR: 30,
    SeverityChoices.WARNING: 20,
    SeverityChoices.INFO: 10,
}

#: The ticket status workflow graph, mapping each status to the set of statuses reachable from it.
#:
#: This is the ONLY definition of transition legality in the codebase. The service layer consults
#: it to accept or reject a transition, the UI consults it (through the service) to decide which
#: buttons to render, and the API consults it to report allowed transitions. Never restate it.
TICKET_STATUS_TRANSITIONS = {
    TicketStatusChoices.NEW: frozenset(
        {
            TicketStatusChoices.TRIAGED,
            TicketStatusChoices.SUPPRESSED,
            TicketStatusChoices.CLOSED,
        }
    ),
    TicketStatusChoices.TRIAGED: frozenset(
        {
            TicketStatusChoices.IN_PROGRESS,
            TicketStatusChoices.SUPPRESSED,
            TicketStatusChoices.RESOLVED,
            TicketStatusChoices.CLOSED,
        }
    ),
    TicketStatusChoices.IN_PROGRESS: frozenset(
        {
            TicketStatusChoices.TRIAGED,
            TicketStatusChoices.RESOLVED,
            TicketStatusChoices.CLOSED,
        }
    ),
    TicketStatusChoices.SUPPRESSED: frozenset(
        {
            TicketStatusChoices.TRIAGED,
            TicketStatusChoices.CLOSED,
        }
    ),
    TicketStatusChoices.RESOLVED: frozenset(
        {
            TicketStatusChoices.IN_PROGRESS,
            TicketStatusChoices.CLOSED,
        }
    ),
    TicketStatusChoices.CLOSED: frozenset(),
}

#: The finished states: resolved or closed. A ticket in one of these is not open work, and an AI
#: actor may not mutate it (rule S3). Every module that needs "is this ticket finished" reads this
#: rather than spelling the pair out.
TERMINAL_STATUSES = frozenset({TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED})

#: The update types that together derive a ticket's attached objects (spec 3.4).
ATTACHMENT_UPDATE_TYPES = (UpdateTypeChoices.OBJECT_ATTACHED, UpdateTypeChoices.OBJECT_DETACHED)
