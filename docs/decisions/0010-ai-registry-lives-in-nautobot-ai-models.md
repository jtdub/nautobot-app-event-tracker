# 0010 — the AI registry lives in `nautobot-ai-models`

**Status:** Accepted
**Phase:** 6

## Context

ADR 0006 made providers and models registry objects, and ADR 0007 did the same for MCP servers and
tools. Four tables followed, with the UI, API, forms, filters and discovery around them. All four
were built here because at the time there was nowhere else to build them.

There is now. `nautobot-ai-models` publishes the same four registries as a shared catalogue:
`AIProvider`, `AIModel`, `MCPServer` and `MCPTool`, with credentials in an `ExternalIntegration`
exactly as ADR 0006 requires, discovery Jobs for both halves, and a documented navigation contract
for sibling apps. Its README is explicit that it is a catalogue: "It contains no AI or LLM client
code and performs no inference. Another app, a Job, or an external system reads these records and
does the work."

Two copies of a registry is not a tidiness problem. An operator registers the same endpoint twice
and rotates its key once. Every AI app added to a Nautobot forks the catalogue again, and none of
them can see what the others may call.

## Decision

**The four registry models are deleted and read from `nautobot-ai-models` instead.**
`LLMProvider`, `LLMModel`, `MCPServer` and `MCPTool` go. `LLMUsageRecord.model`,
`TicketEmbedding.model` and `AgentToolCall.tool` become foreign keys into the other app.

**The dependency is required, not an extra.** litellm and the MCP client are optional because they
are only ever called, and a deployment that calls neither installs neither. This one is different:
`models.py` declares foreign keys into it, so Django resolves them at import time on every start.
The floor is 1.1, which is the release that records a provider's dialect and whether it is enabled,
a model's kind and its generation parameters, and the two registry-policy settings ADR 0007 needs.

**Behaviour stays here.** Inference, usage accounting, tool calling, the approval gate, agents and
retrieval are unchanged and remain this app's. That is not a compromise: it is the division the
other app states as its own design.

**Discovery moves.** This app no longer discovers anything. The `discovermcptools` command and the
per-server discovery view are deleted, and operators run the AI Models app's two Jobs instead.

**No data is migrated.** This app has never been released. Migration 0012 repoints the three
foreign keys and drops the four tables.

## Consequences

**Good.** One registry per Nautobot, whatever else is installed. One place to rotate a key. Two
discovery Jobs this app no longer maintains, and a `GET /v1/models` model catalogue it never had.
An operator standing on a provider can see everything that calls it.

**Bad.** A hard runtime dependency on a second app, on a version floor that a released app cannot
lower. An upgrade of either is now an upgrade of both.

**Bad.** Fields this app needs are somebody else's to add. Every one of them is in 1.1, but the
next one is an issue and a release rather than a migration.

**Bad.** Registry permissions move to another app's permission set. A role that could edit an MCP
tool now needs `nautobot_ai_models.change_mcptool`, which is a deployment change at upgrade time
and is not something a migration can do.

**Neutral.** ADR 0007's default-deny survives as configuration on the dependency rather than as a
model default here. See its amendment.

## Alternatives considered

**Keep both.** Rejected: it is the situation this ADR exists to end.

**Move inference upstream too, and depend on that.** Rejected. It reverses that app's stated design
for the convenience of one consumer, and `LLMUsageRecord` could not go with it anyway - it carries
a foreign key to `EventTicket`.

**Depend on 1.0 and carry the missing fields locally**, as a provider-name-to-dialect map and two
settings keys. Rejected once 1.1 was in prospect: it puts facts about a provider in this app's
configuration, where a rename breaks them silently, to save waiting for a release.

**A local sidecar model** with a one-to-one to `AIModel` carrying the missing fields. Rejected: an
operator would edit two objects to register one model, and every read would need a join.
