# Architecture Decision Records

The design of this app is recorded in two places:

- [Architecture overview](../architecture.md) — the shape of the whole system, its components, and how the phases divide it up.
- [Decision records](../decisions/README.md) — one record per cross-cutting decision, with the pressure that forced it and what it costs.

Records are immutable once accepted. To change a decision, add a new record that supersedes the old one rather than editing history.

Per-phase execution specs live under [`docs/specs/`](../specs/phase-1-tickets.md).
