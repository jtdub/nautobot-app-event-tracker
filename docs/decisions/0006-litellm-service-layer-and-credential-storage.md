# 0006 — litellm service layer and credential storage

**Status:** Accepted
**Phase:** 3

## Context

The app calls LLMs from several places: triage in the consumer, agents working a ticket, embedding generation for RAG. Which provider serves those calls is a deployment decision, not an application one. Some operators will use a hosted API; many network operators cannot send device configuration to a third party at all and need an on-premises OpenAI-compatible endpoint.

Two things follow. The provider must be swappable without touching call sites, and provider credentials must be stored the way Nautobot stores every other credential — not in `PLUGINS_CONFIG`, where they end up in configuration management and version control.

Cost is the third pressure. LLM spend is invisible until it is a surprise, and a triage pipeline processing network events can generate a great deal of it.

## Decision

**All model calls go through litellm**, wrapped in the app's own service layer. No call site imports a provider SDK. litellm is the one new runtime dependency this introduces, and it is named here because it is the decision.

**Providers and models are registry objects.** `LLMProvider` records an endpoint and its credentials; `LLMModel` records a specific model available from a provider along with its parameters and cost metadata. Operators add a provider through the UI rather than editing settings.

**Credentials live in Nautobot.** An `LLMProvider` points at an `ExternalIntegration`, which carries the endpoint and a `SecretsGroup`. The app therefore inherits Nautobot's secrets providers — environment variables, files, HashiCorp Vault, AWS Secrets Manager — with no key material in app configuration and none in the database in plaintext.

**Every call is recorded.** The service layer writes an `LLMUsageRecord` for each request: model, token counts, computed cost, latency, and the ticket it belongs to. Recording happens in the service layer rather than at call sites, so a new caller cannot forget to do it. Failed calls are recorded too — a retry storm against a broken endpoint should be visible.

## Consequences

**Good.** Switching providers is configuration. On-premises deployments are a first-class path, not a workaround. Cost is attributable per ticket and per model, which is what makes the Phase 5 analytics dashboard worth building.

**Bad.** litellm becomes a dependency in the request path for every AI feature, and its release cadence is brisk. It needs a conservative version constraint and attention at upgrade time.

**Bad.** The lowest common denominator problem: provider-specific features reachable only through a native SDK are not available through litellm's unified interface.

**Bad.** Usage records grow without bound on a busy system. A retention policy is needed, and is deferred to Phase 3 rather than solved here.

## Alternatives considered

**Provider SDKs directly with an internal adapter.** Rejected: it is writing and maintaining litellm.

**Credentials in `PLUGINS_CONFIG`.** Rejected: it puts secrets in configuration files and ignores the secrets infrastructure Nautobot already provides.

**Usage accounting at call sites.** Rejected: optional instrumentation is instrumentation that gets skipped.
