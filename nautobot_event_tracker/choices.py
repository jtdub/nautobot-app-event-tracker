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


class LLMProviderTypeChoices(ChoiceSet):
    """The kind of API an LLMProvider speaks.

    This selects how the service layer builds the litellm model string and which credentials it
    expects, nothing more. An on-premises endpoint speaking the OpenAI protocol is a first-class
    citizen here (ADR 0006): many network operators cannot send configuration to a third party.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"

    CHOICES = (
        (OPENAI, "OpenAI"),
        (ANTHROPIC, "Anthropic"),
        (OPENAI_COMPATIBLE, "OpenAI-compatible"),
    )


#: The litellm routing prefix for each provider type, kept beside the ChoiceSet the same way
#: SEVERITY_WEIGHTS sits beside SeverityChoices: a new provider type is one edit in one file, not
#: a choices entry that compiles everywhere and KeyErrors at call time. An OpenAI-compatible
#: endpoint uses the `openai` prefix with its own `api_base`.
LITELLM_PROVIDER_PREFIXES = {
    LLMProviderTypeChoices.OPENAI: "openai",
    LLMProviderTypeChoices.ANTHROPIC: "anthropic",
    LLMProviderTypeChoices.OPENAI_COMPATIBLE: "openai",
}


class LLMPurposeChoices(ChoiceSet):
    """What an LLM call was for. Every LLMUsageRecord carries one.

    Phase 3 has a single purpose; later phases append (agents in Phase 4, embeddings in Phase 5)
    without a migration, since choices are code.
    """

    TRIAGE = "triage"

    CHOICES = ((TRIAGE, "Triage"),)


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


def shortest_transition_path(to_status, from_status=TicketStatusChoices.NEW):
    """The fewest legal transitions that get a ticket from one status to another.

    Derived from the graph by breadth-first search rather than written out beside it: a hand-kept
    list of routes is a second definition of legality, and it would go stale the first time an edge
    changed. Returns an empty tuple when the ticket is already there, and None when no route exists
    - which, since `closed` is terminal, is every route out of it.

    Used by the test fixtures and by the test data command, both of which need a ticket in a given
    state and must not get there by assigning `status`.
    """
    if from_status == to_status:
        return ()

    queue = [(from_status, ())]
    seen = {from_status}
    while queue:
        status, route = queue.pop(0)
        for step in sorted(TICKET_STATUS_TRANSITIONS[status]):
            if step in seen:
                continue
            if step == to_status:
                return route + (step,)
            seen.add(step)
            queue.append((step, route + (step,)))
    return None
