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
    # The approval gate's three moments (Phase 4B, section 7). They are on the ticket's own trail
    # rather than only on the run, because the trail is where a person looks to answer "what was
    # done to this ticket, by whom" - and a tool call against the network is the loudest possible
    # answer to that question.
    TOOL_PROPOSED = "tool_proposed"
    TOOL_DECIDED = "tool_decided"
    TOOL_EXECUTED = "tool_executed"

    CHOICES = (
        (CREATED, "Created"),
        (COMMENT, "Comment"),
        (STATUS_CHANGE, "Status Change"),
        (ASSIGNMENT, "Assignment"),
        (SEVERITY_CHANGE, "Severity Change"),
        (OBJECT_ATTACHED, "Object Attached"),
        (OBJECT_DETACHED, "Object Detached"),
        (RECURRENCE, "Recurrence"),
        (TOOL_PROPOSED, "Tool Proposed"),
        (TOOL_DECIDED, "Tool Decided"),
        (TOOL_EXECUTED, "Tool Executed"),
    )


class LLMProviderTypeChoices(ChoiceSet):
    """The kind of API an LLMProvider speaks.

    This selects how the service layer builds the litellm model string and which credentials it
    expects, nothing more. An on-premises endpoint speaking the OpenAI protocol is a first-class
    citizen here (ADR 0006): many network operators cannot send configuration to a third party.

    Ollama has a type of its own rather than being one more OpenAI-compatible endpoint, and it is
    worth saying why, because "it speaks the OpenAI protocol" is true and not sufficient. Ollama's
    OpenAI-compatibility layer does not return tool calls in the `tool_calls` field: a model asked
    for a tool answers with the JSON call written into the message content, where nothing may act
    on it. Its native API does return them, and litellm reaches that through the `ollama/` prefix.
    So on the OpenAI-compatible path an Ollama-backed agent cannot call a tool at all - which is
    most of Phase 4B - and on this one it can. Measured against Ollama 0.x with qwen2.5-coder and
    llama3.2; if the compatibility layer ever grows the field, this type still costs nothing.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"

    CHOICES = (
        (OPENAI, "OpenAI"),
        (ANTHROPIC, "Anthropic"),
        (OPENAI_COMPATIBLE, "OpenAI-compatible"),
        (OLLAMA, "Ollama"),
    )


#: The litellm routing prefix for each provider type, kept beside the ChoiceSet the same way
#: SEVERITY_WEIGHTS sits beside SeverityChoices: a new provider type is one edit in one file, not
#: a choices entry that compiles everywhere and KeyErrors at call time. An OpenAI-compatible
#: endpoint uses the `openai` prefix with its own `api_base`.
LITELLM_PROVIDER_PREFIXES = {
    LLMProviderTypeChoices.OPENAI: "openai",
    LLMProviderTypeChoices.ANTHROPIC: "anthropic",
    LLMProviderTypeChoices.OPENAI_COMPATIBLE: "openai",
    # `ollama`, not `ollama_chat`. litellm offers both and the second is the one usually
    # recommended; it was the first that returned native tool calls when this was measured, and
    # the second that did not.
    LLMProviderTypeChoices.OLLAMA: "ollama",
}

#: Provider types that are an address rather than a service: litellm would otherwise fall back to a
#: default endpoint, which for `openai` is somebody else's API and for `ollama` is a loopback
#: address that means nothing inside a container. Both are refused without a URL, at save time and
#: again at call time.
PROVIDER_TYPES_REQUIRING_A_URL = frozenset({LLMProviderTypeChoices.OPENAI_COMPATIBLE, LLMProviderTypeChoices.OLLAMA})


class LLMPurposeChoices(ChoiceSet):
    """What an LLM call was for. Every LLMUsageRecord carries one.

    Phase 3 has a single purpose; later phases append (agents in Phase 4, embeddings in Phase 5)
    without a migration, since choices are code.
    """

    TRIAGE = "triage"
    AGENT = "agent"

    CHOICES = (
        (TRIAGE, "Triage"),
        (AGENT, "Agent"),
    )


class AgentRunStatusChoices(ChoiceSet):
    """Where one agent run got to.

    Five of the six are ends. `waiting_approval` is the one that is not an end and is still a
    finished run: the loop stops at a mutating proposal and hands the worker slot back, so a run in
    this state is not executing anything and is not waiting on a lock (ADR 0009).
    """

    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    DENIED = "denied"
    FAILED = "failed"
    SUPERSEDED = "superseded"

    CHOICES = (
        (RUNNING, "Running"),
        (WAITING_APPROVAL, "Waiting for Approval"),
        (COMPLETED, "Completed"),
        (DENIED, "Denied"),
        (FAILED, "Failed"),
        (SUPERSEDED, "Superseded"),
    )


class AgentToolCallStatusChoices(ChoiceSet):
    """What became of one tool call an agent asked for.

    A read-only call is written straight to `executed` or `failed` and never has a decider; a
    mutating one passes through `proposed` and then `approved` or `denied`. That is the entire
    difference between the two kinds, and it is one column.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    DENIED = "denied"
    #: Claimed by a caller and in flight. Exists so that "may this run" and "this is running" are
    #: one atomic step: without it the check and the call are separate, and two callers can both
    #: pass the check before either writes. A row left here is a process that died mid-call, which
    #: is worth being able to see.
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"

    CHOICES = (
        (PROPOSED, "Proposed"),
        (APPROVED, "Approved"),
        (DENIED, "Denied"),
        (EXECUTING, "Executing"),
        (EXECUTED, "Executed"),
        (FAILED, "Failed"),
    )


#: Run states in which a run is part of the ticket's current chain, so a second launch would be a
#: second agent on one ticket (rule A9). `waiting_approval` is in here because the chain is not
#: over: somebody still has a decision to make, or has made one that nothing has acted on yet.
AGENT_RUN_LIVE_STATUSES = frozenset({AgentRunStatusChoices.RUNNING, AgentRunStatusChoices.WAITING_APPROVAL})

#: The statuses an agent may move a ticket into (rule A5). Resolving and closing are a person's
#: judgement: a wrongly closed ticket looks exactly like a solved one, which is what makes it the
#: one mistake nobody sees.
AGENT_ALLOWED_TRANSITIONS = frozenset({TicketStatusChoices.TRIAGED, TicketStatusChoices.IN_PROGRESS})


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
