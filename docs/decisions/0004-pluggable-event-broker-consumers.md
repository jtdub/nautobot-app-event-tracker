# 0004 — Pluggable event broker consumers

**Status:** Accepted
**Phase:** 2

## Context

Events arrive from syslog collectors, SNMP trap receivers, and streaming telemetry pipelines. Between those sources and this app sits a broker, and which broker that is depends entirely on the site. A large operator already runs Kafka. A lab has Redis and nothing else.

Two properties differ sharply between those two worlds. Kafka tracks consumer offsets, so a consumer that dies can resume where it stopped and replay history. Redis pub/sub does neither: a message published while nobody is listening is gone. Pretending these are interchangeable would mean either promising replay the lab cannot deliver, or giving up replay for everyone.

## Decision

Broker access sits behind an abstract `EventConsumer` interface. The interface is deliberately small — connect, iterate messages, acknowledge, close — and explicitly declares whether an implementation supports replay, so callers can adapt rather than guess.

Two implementations ship:

- **Kafka** is the reference implementation. Offsets are committed only after a message has been fully processed, which gives at-least-once delivery and makes replay after an outage a supported operation.
- **Redis pub/sub** is supported for development and lab use, marked best-effort. Messages published while the consumer is down are lost, and the documentation says so without euphemism.

Broker credentials follow the rule ADR 0006 sets for LLM credentials: a topic block names an `ExternalIntegration`, and the consumer reads that integration's secrets group at connection time. No broker username, password or token is ever written to `PLUGINS_CONFIG`, to an app model, or to a log line. This is the same decision applied to a second kind of credential, not a new one, which is why it is recorded here rather than in an ADR of its own.

Because delivery is at-least-once rather than exactly-once, duplicate delivery is normal and the ticket layer must be idempotent about it. That is what the dedup key on ticket creation is for (see the Phase 1 spec, rule S5) — the property is built in Phase 1, before the thing that needs it exists.

## Consequences

**Good.** Sites use the broker they already run. The dedup requirement is surfaced early, so Phase 1 builds for it instead of retrofitting.

**Bad.** The interface can only express the intersection of both brokers' capabilities without leaking. Consumer groups, partition assignment, and offset seeking are Kafka-specific and reachable only through the Kafka implementation, so code that uses them is not portable — which is honest, but does mean the abstraction is not total.

**Bad.** Two implementations mean two sets of failure modes to test, and the Redis one is hard to test meaningfully because its defining characteristic is losing things.

## Alternatives considered

**Kafka only.** Simpler and more honest about delivery guarantees, but it puts a heavyweight dependency in front of anyone wanting to evaluate the app.

**Nautobot's own event broker.** Nautobot 3.x publishes change events to a configurable broker. Rejected as the ingestion path: it carries Nautobot object-change notifications, not raw network events from external collectors, so it solves a different problem.
