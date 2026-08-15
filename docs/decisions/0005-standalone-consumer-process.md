# 0005 — Standalone consumer process

**Status:** Accepted
**Phase:** 2

## Context

Something has to hold a long-lived connection to the broker, pull messages continuously, and process them as they arrive. Nautobot offers two obvious homes for background work, and neither fits.

**Nautobot Jobs** are units of work with a beginning and an end, launched on demand or on a schedule, with their execution recorded as a `JobResult`. A consumer loop has no end, and recording one `JobResult` for a process that runs for weeks is meaningless.

**Celery tasks** are worse: a task that never returns occupies a worker slot forever, starving every other background job in the deployment. Polling on a schedule instead — a task that wakes, drains the queue, and exits — would work but reintroduces latency, gives up Kafka's streaming consumer semantics, and turns offset management into a per-invocation problem.

## Decision

Ingestion runs as a standalone process, started by a Django management command:

```
nautobot-server eventconsumer
```

It is a first-class deployment unit, supervised like Nautobot's own web and worker processes — a systemd unit, a container, a Kubernetes deployment — and it is documented that way. It runs inside the full Nautobot application context, so it has ORM access and the app's configuration without any RPC layer between it and the ticket service.

It handles `SIGTERM` and `SIGINT` by finishing the message in flight, committing the offset, and exiting, so a restart neither loses nor double-processes work beyond the at-least-once guarantee of ADR 0004.

Multiple instances may run concurrently against a Kafka consumer group for throughput and redundancy. Against Redis pub/sub, every instance receives every message, so running more than one duplicates work — dedup (rule S5) makes that harmless rather than correct.

## Consequences

**Good.** Streaming semantics are preserved, offsets are managed once per process rather than per invocation, and the consumer cannot starve the Celery pool. Direct ORM access keeps the ticket service call in-process.

**Bad.** It is another process for operators to run, monitor, and restart. An app that needed no new deployment units would be easier to adopt, and this cost is real.

**Bad.** Its health is invisible to Nautobot's Job history. Phase 2 therefore records `IngestionStats` so that throughput, drop counts, and last-message time are observable from the UI rather than only from process logs.

## Alternatives considered

**Scheduled Job that drains and exits.** Rejected: latency floor set by the schedule interval, and it fights Kafka's consumer model rather than using it.

**Celery task with an infinite loop.** Rejected: permanently consumes a worker slot.

**External service outside Nautobot.** Rejected: it would need the REST API for every ticket operation, which is slower, and it would duplicate configuration and credentials outside Nautobot's control.
