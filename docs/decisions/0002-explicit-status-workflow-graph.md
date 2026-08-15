# 0002 — Explicit status workflow graph in code

**Status:** Accepted
**Phase:** 1

## Context

A ticket moves through states, and not every move is legal. Reopening a resolved ticket is fine; jumping a brand new ticket straight to closed without triage is not. Something has to hold that knowledge.

Nautobot's own convention for this is the `Status` model with `StatusField`: statuses are database rows, editable by an administrator, and shared across models via content types. That convention exists for good reasons and departing from it needs justification.

The problem is that a `Status` row is data, and the transition graph is code. If an administrator adds a status named "Pending Vendor" through the UI, the graph has no edges for it — the ticket becomes unreachable and unleaveable, and the failure appears at runtime in production rather than at import time in CI. Worse, the AI immutability rule keys off two specific states (resolved and closed). If those are editable rows, an administrator can rename or delete the very states the safety rule depends on.

## Decision

Ticket status is a `ChoiceSet` defined in code (`TicketStatusChoices`), not a Nautobot `Status` object. Alongside it lives an explicit adjacency map:

```python
TICKET_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    TicketStatusChoices.NEW: frozenset({TRIAGED, SUPPRESSED, CLOSED}),
    ...
}
```

This map is the single source of truth for legality. The service layer consults it to accept or reject a transition; the UI consults it to decide which buttons to render; the API consults it to populate the allowed values on the transition endpoint. There is no second copy of this knowledge anywhere.

The graph is a plain data structure rather than a state-machine library, so tests can enumerate it: for every (from, to) pair in the full cross product of states, a test asserts that the service either permits or rejects the move, and the expected matrix is written out longhand in the test rather than derived from the same map it is checking.

## Consequences

**Good.** Illegal states are unreachable by construction, and the whole space of transitions is small enough to assert exhaustively. The states the safety rules depend on cannot be renamed by an administrator. Adding a state is a code change that goes through review and lands with its edges.

**Bad.** Operators cannot add a workflow state without a code change. This is intentional — the graph is behaviour, not configuration — but it will be an unwelcome surprise to anyone expecting Nautobot's usual status flexibility, so the user documentation says so plainly.

**Bad.** The app does not participate in Nautobot's cross-model status filtering and colouring conventions. Ticket status is rendered from the choice set's own colour mapping instead.

## Alternatives considered

**Nautobot `Status` with a separate transition table.** Keeps the Nautobot convention and makes the graph editable. Rejected: it moves a safety-critical invariant into user-editable data, and it makes "which transitions are legal" a database query on every button render.

**`django-fsm` or a state machine library.** Rejected: it would be a new dependency for something a `dict` of `frozenset`s expresses completely, and it wants to own `save()`, which conflicts with ADR 0001.
