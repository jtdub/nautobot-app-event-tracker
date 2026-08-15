# 0003 — PostgreSQL with pgvector as the only database

**Status:** Accepted
**Phase:** 1 (constraint), 5 (pgvector use)

## Context

Nautobot supports both PostgreSQL and MySQL. Apps that want to run anywhere therefore avoid backend-specific features, or write conditionals around them.

This app cannot honestly do that. Phase 5 indexes closed tickets as embeddings and runs similarity search over them, which needs pgvector. There is no MySQL equivalent worth maintaining a second code path for. Beyond that, ticket payloads are JSON documents that want `JSONB` containment queries, and the ingestion path wants `INSERT ... ON CONFLICT` for dedup — both PostgreSQL-shaped.

The choice is between supporting MySQL with a materially worse feature set, or supporting one backend properly.

## Decision

PostgreSQL is the only supported database. The app declares this in its installation documentation and does not carry MySQL conditionals in any phase, including Phase 1 where nothing yet depends on a PostgreSQL feature.

The `pgvector` extension is required from Phase 5 onward. Phase 1 does not require it and does not check for it.

The cookiecutter template shipped MySQL development plumbing (`invoke.mysql.yml`, `development/docker-compose.mysql.yml`, `development/development_mysql.env`) and a MySQL leg in the CI matrix. Those have been removed: a CI job testing a configuration the documentation tells operators not to run is worse than no job at all, and it would have started failing the moment a PostgreSQL-only feature landed.

## Consequences

**Good.** One backend to test, one set of query semantics to reason about, and later phases can use the right tool without relitigating this.

**Bad.** MySQL-based Nautobot deployments cannot install this app. That is a real reduction in the addressable install base and is stated up front in the install guide rather than discovered at migration time.

**Bad.** Contributors have to remember the rule even in phases where nothing enforces it, since Phase 1 code would run happily on MySQL. Review is the only guard.

## Alternatives considered

**Support MySQL, degrade RAG to keyword search.** Rejected: it makes the flagship Phase 5 capability a second-class feature on one backend, and doubles the test matrix for a feature nobody would want in its degraded form.

**Support MySQL, put embeddings in an external vector store.** Rejected: a separate vector service is a whole operational component — its own backups, its own failure mode, its own consistency problem with the ticket rows it indexes. Keeping embeddings in the same transactional database is a large simplification.
