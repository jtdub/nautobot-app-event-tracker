# 0001 — Ticket service layer as the sole mutation path

**Status:** Accepted
**Phase:** 1

## Context

Tickets in this system are written by four different kinds of caller: a human in the web UI, a script against the REST API, the ingestion consumer, and — from Phase 3 — an LLM agent. Each of those callers wants to do roughly the same things: open a ticket, comment, move it through a workflow, attach network objects to it.

Two rules have to hold no matter who is calling:

- Every mutation leaves an audit row. A ticket's history must explain itself without reading the changelog.
- An AI actor must not be able to modify a ticket a human has already resolved or closed.

The obvious Django approach — put the logic in `Model.save()` or in a serializer — fails both. `save()` cannot see *who* is calling or *why*, so it cannot distinguish an AI comment from a human one. A serializer only covers the REST path, leaving the UI and the consumer to reimplement the same rules, which is how the rules drift apart.

## Decision

All ticket mutations go through `nautobot_event_tracker/services/tickets.py`. Nothing else writes to `EventTicket` or `TicketUpdate`.

Concretely:

- Every service function takes an explicit `source` (one of `human`, `ai`, `system`) and, for human calls, the acting `user`. The caller's identity is a required argument, not something inferred from thread-local state.
- Every service function that changes a ticket writes the ticket row and its corresponding `TicketUpdate` row inside one `transaction.atomic()` block.
- Views, serializers, forms, and tests call the service. None of them assign `ticket.status` directly.
- The service raises typed errors — `TicketImmutableError`, `InvalidTransitionError` — that each transport maps to its own idiom: an HTTP 409 in the API, a form error in the UI.

## Consequences

**Good.** The AI immutability rule is enforced in exactly one place, so it can be tested exhaustively in exactly one place. New callers get the rules for free. The audit trail cannot be bypassed by accident, because the only way to change a ticket also writes the trail.

**Bad.** It is a convention, not a mechanism: nothing in Django stops a future contributor from calling `ticket.save()` directly. That gap is covered by review and by a test that asserts the app's own views and serializers do not expose `status` as a writable field. A stronger mechanism (blocking `save()` outside the service via a sentinel argument) was considered and rejected as too clever for the benefit — it makes migrations, bulk operations, and the Django admin awkward.

**Bad.** Service functions grow long argument lists. Keyword-only arguments keep call sites readable.

## Alternatives considered

**Django signals.** Rejected: signals fire after the fact and cannot veto with a useful error message, and ordering between handlers is implicit.

**Custom model manager methods.** Rejected: a manager still has no notion of an acting source, and `objects.create()` remains available beside it.

**Nautobot custom validators.** These run on `full_clean()` and are the right home for *shape* validation (rules C1–C3 in the Phase 1 spec), but they cannot express "this actor may not do this action," because they never see the actor. Used alongside the service layer, not instead of it.
