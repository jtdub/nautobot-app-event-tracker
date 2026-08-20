# External Interactions

Event Tracker talks to three things outside Nautobot: an event broker, an LLM provider, and
Nautobot's own REST and GraphQL APIs, which other systems use to reach the app. This page names
each connection, what crosses it, and where its credential lives.

Every credential is a Nautobot [Secret](https://docs.nautobot.com/projects/core/en/stable/user-guide/platform-functionality/secret/),
reached through an `ExternalIntegration` and its secrets group. On that path nothing key-shaped is
stored in `PLUGINS_CONFIG` or on an app model, and no credential reaches a log line. The one way
round it is the broker's plain `url` setting, kept for labs, which can carry a password in the URL
— [ADR 0004](../decisions/0004-pluggable-event-broker-consumers.md) says why that is tolerated and
not supported.

## From the App to Other Systems

### The event broker

The consumer process (`nautobot-server eventconsumer`) subscribes to one or more topics on a
broker and reads messages from it. It never writes to the broker.

| | |
| --- | --- |
| **Direction** | Outbound, long-lived connection |
| **Protocols** | Kafka (via `confluent-kafka`, the optional `kafka` extra) or Redis pub/sub |
| **Runs in** | The standalone consumer process, not the web workers and not Celery |
| **Credential** | The named `ExternalIntegration`'s secrets group, read at connection time |
| **Configured by** | The `ingestion` block — see [Ingestion](../admin/ingestion.md) |

The consumer is the only part of the app that connects to a broker. Nothing in a web request
touches it.

### The LLM provider

When triage is enabled, the consumer asks a language model what each accepted event deserves. Every
model call in the app goes through one function, `services/llm.py::complete()`, which is also the
only module that imports litellm and the only writer of the usage records.

| | |
| --- | --- |
| **Direction** | Outbound HTTPS, one request per triaged event |
| **Protocol** | Whatever litellm speaks to the configured provider; OpenAI-compatible endpoints are first-class |
| **Endpoint** | The provider's `ExternalIntegration.remote_url`. No model parameter can change it; changing it needs `change_llmprovider`, or the rights to edit that integration |
| **Credential** | That integration's secrets group, read at call time; secret type `token`, falling back to `secret` |
| **What is sent** | A fixed system prompt, the event payload capped at `max_context_chars`, and the titles and severities of up to `attach_candidates` open tickets |
| **What is recorded** | One `LLMUsageRecord` per call — including failed calls — with token counts, cost and latency |
| **Configured by** | The `llm` block and `ingestion.triage` — see [Configuring LLM Providers](../admin/llm.md) |

Three things worth knowing before you enable it:

- **The payload leaves the box.** Set `triage: False` on any topic whose payloads must not, or
  point the provider at a self-hosted endpoint. On-premises endpoints are a first-class case, not
  an afterthought.
- **The off switch is a database flag.** Unticking **Enabled** on the provider or the model stops
  every call, including on a consumer already running — within `triage.model_cache_seconds`.
- **Failure is safe.** A timeout, a provider error or an unusable answer accepts the event and is
  counted. The consumer never exits because a model misbehaved.

No other outbound connection exists. The app makes no telemetry, licensing or update calls.

## From Other Systems to the App

Everything Event Tracker exposes is a Nautobot API, authenticated and permission-checked the way
the rest of Nautobot is. There is no app-specific listener, webhook receiver or open port.

Because the ticket service layer owns every mutation ([ADR 0001](../decisions/0001-ticket-service-layer-as-sole-mutation-path.md)),
the API is deliberately narrower than the models: a caller cannot set a ticket's status directly,
and cannot write a `TicketUpdate`, an `IngestionStats` row or an `LLMUsageRecord` through any route.
Those refusals are enforced by the app, not by permissions, so a superuser meets them too.

## Nautobot REST API endpoints

All endpoints live under `/api/plugins/event-tracker/` and appear in Nautobot's own OpenAPI schema
at `/api/docs/`. Authentication is a Nautobot API token, as everywhere else:

```bash
curl -H "Authorization: Token $NAUTOBOT_TOKEN" \
     -H "Accept: application/json" \
     https://nautobot.example.com/api/plugins/event-tracker/tickets/
```

| Endpoint | Methods | Notes |
| --- | --- | --- |
| `event-types/` | Full CRUD | |
| `tickets/` | Full CRUD, plus actions | Service-owned fields are read-only; see below |
| `tickets/<id>/transition/` | `GET`, `POST` | `GET` lists the transitions allowed from here; `POST` performs one |
| `tickets/<id>/comment/`, `attach/`, `detach/` | `POST` | Comment on a ticket, and attach or detach a Nautobot object |
| `ticket-updates/` | `GET`, `HEAD`, `OPTIONS` | Append-only history; writes are 405 |
| `ingestion-stats/` | `GET`, `HEAD`, `OPTIONS` | Written by the consumer |
| `llm-providers/`, `llm-models/` | Full CRUD | The registry |
| `llm-usage/` | `GET`, `HEAD`, `OPTIONS` | Written by the service layer alone; writes are 405 |

To move a ticket through the workflow, post to the transition action rather than patching `status`:

```bash
curl -X POST \
     -H "Authorization: Token $NAUTOBOT_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"to_status": "triaged", "message": "picked up by the on-call"}' \
     https://nautobot.example.com/api/plugins/event-tracker/tickets/<id>/transition/
```

A transition the workflow graph does not permit is refused with a message naming the statuses that
are reachable from the current one.

### GraphQL

Every model is queryable at Nautobot's `/api/graphql/` endpoint. GraphQL is read-only, in this app
as in Nautobot core:

```graphql
{
  event_tickets {
    title
    status
    severity
    event_count
    updates { update_type source message }
  }
}
```

## Further reading

- [Ingestion](../admin/ingestion.md) — broker configuration and the pre-filter
- [Configuring LLM Providers](../admin/llm.md) — providers, models, triage and spend
- [Install and Configure](../admin/install.md) — permissions and the AI actor
