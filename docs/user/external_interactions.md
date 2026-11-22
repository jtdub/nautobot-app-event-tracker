# External Interactions

Event Tracker connects to three things outside Nautobot: an event broker, an LLM provider, and the REST and GraphQL APIs of Nautobot. Other systems use the APIs to reach the app. This page names each connection, the data that crosses it, and the place of its credential.

Each credential is a Nautobot [Secret](https://docs.nautobot.com/projects/core/en/stable/user-guide/platform-functionality/secret/). The app reads it through an `ExternalIntegration` and its secrets group. On this path, the app stores no key in `PLUGINS_CONFIG` or on an app model. No credential goes to a log line.

One setting is an exception: the plain `url` setting of the broker. It is for labs. It can hold a password in the URL. [ADR 0004](../decisions/0004-pluggable-event-broker-consumers.md) tells why the app tolerates this and does not support it.

## From the App to Other Systems

### The event broker

The consumer process (`nautobot-server eventconsumer`) subscribes to one or more topics on a broker. It reads messages from the broker. It never writes to the broker.

| | |
| --- | --- |
| **Direction** | Outbound, long-lived connection |
| **Protocols** | Kafka (with `confluent-kafka`, the optional `kafka` extra) or Redis pub/sub |
| **Runs in** | The standalone consumer process. It does not run in the web workers or in Celery |
| **Credential** | The secrets group of the named `ExternalIntegration`, read when the connection starts |
| **Configured by** | The `ingestion` block. See [Ingestion](../admin/ingestion.md) |

The consumer is the only part of the app that connects to a broker. A web request never connects to the broker.

### The LLM provider

Event triage is optional. When it is on, the consumer asks a language model what to do with each accepted event. Each model call in the app goes through one function: `services/llm.py::complete()`. This module is also the only module that imports litellm. It is the only writer of the usage records.

| | |
| --- | --- |
| **Direction** | Outbound HTTPS, one request for each triaged event |
| **Protocol** | The protocol that litellm uses with the configured provider. The app fully supports OpenAI-compatible endpoints |
| **Endpoint** | The `ExternalIntegration.remote_url` of the AI Provider. No model parameter can change it. To change it, you need `nautobot_ai_models.change_aiprovider`, or the right to edit that integration |
| **Credential** | The secrets group of that integration, read when the call starts. The app uses secret type `token`, or `secret` when no token exists |
| **TLS** | The app does not apply **Verify SSL** and **CA file path** of the integration to the call. See [Configuring LLM Providers](../admin/llm.md) |
| **Headers and timeout** | The app applies Headers and Timeout of the integration to the call. It does not apply Extra Config |
| **What is sent** | A fixed system prompt. The event payload, cut to `max_context_chars`. The titles and severities of up to `attach_candidates` open tickets. A Headers template that uses `session_id` also sends the consumer name. By default, this name holds the host name and the process number |
| **What is recorded** | One `LLMUsageRecord` for each call, also for a failed call. It holds the token counts, the cost and the latency |
| **Configured by** | The `llm` block and `ingestion.triage`. See [Configuring LLM Providers](../admin/llm.md) |

Read these four points before you turn on triage:

- **The payload leaves your network.** To stop this, set `triage: False` on each topic whose payloads must stay in your network. You can also point the provider at an endpoint that you host. The app fully supports an endpoint on your own premises.
- **A database flag turns triage off.** Clear **Enabled** on the provider or on the model. Every call then stops, also in a consumer that already runs. The stop takes effect within `triage.model_cache_seconds`.
- **The consumer name can leave your network.** A Headers template that uses `session_id` sends the name to the provider. Set `ingestion.consumer_name` to a text that names nothing. Give each consumer a different name. See [Ingestion](../admin/ingestion.md).
- **A failure is safe.** A timeout, a provider error or an unusable answer makes the app accept the event. The app counts it. The consumer does not exit when a model fails.

### MCP servers

An operator can register an MCP server and enable tools on it. The [agent](../admin/agents.md) can then call those tools. Nothing else in the app calls a tool. The agent is off by default.

| | |
| --- | --- |
| **Direction** | Outbound HTTPS, one session for each operation |
| **Protocol** | MCP over **streamable HTTP** only. The app refuses stdio. No configuration value can start a local process |
| **Endpoint** | The `ExternalIntegration.remote_url` of the server |
| **Credential** | The secrets group of that integration. The app sends it as `Authorization: Bearer …`, unless the headers of the integration already have an Authorization |
| **TLS** | The app applies **Verify SSL**, **CA file path**, Headers and Timeout of the integration |
| **What is sent** | On discovery, only the protocol handshake. On a tool call, the arguments that a model asked for. For this reason, a writable tool needs a human to approve the exact call |
| **What is recorded** | Each tool that the app can call is a database row. An operator enabled it by hand |
| **Configured by** | The MCP Server and MCP Tool records. See [Registering MCP Servers](../admin/mcp.md) |

Two limits apply to this connection. A prompt does not set either limit.

- **A tool is not callable until somebody enables it.** This is true when `new_tools_enabled` is `False` for `nautobot_ai_models`. Discovery then creates each tool as disabled. See [Registering MCP Servers](../admin/mcp.md) for the two policy settings.
- **The transport cannot run anything on the local host.** The app has no stdio option to turn on.

The app has no other outbound connection. It makes no telemetry call, no licensing call and no update call.

## From Other Systems to the App

Each interface of Event Tracker is a Nautobot API. Nautobot authenticates each request and checks its permissions, as everywhere else. The app has no listener, no webhook receiver and no open port of its own.

The ticket service layer owns each mutation ([ADR 0001](../decisions/0001-ticket-service-layer-as-sole-mutation-path.md)). For this reason, the API is narrower than the models. A caller cannot set the status of a ticket directly. A caller cannot write a `TicketUpdate`, an `IngestionStats` row or an `LLMUsageRecord` by any route. The app enforces these refusals. Permissions do not cause them, so a superuser also gets them.

## Nautobot REST API endpoints

All endpoints are under `/api/plugins/event-tracker/`. They are also in the OpenAPI schema of Nautobot at `/api/docs/`. Authentication uses a Nautobot API token, as everywhere else:

```bash
curl -H "Authorization: Token $NAUTOBOT_TOKEN" \
     -H "Accept: application/json" \
     https://nautobot.example.com/api/plugins/event-tracker/tickets/
```

| Endpoint | Methods | Notes |
| --- | --- | --- |
| `event-types/` | Full CRUD | |
| `tickets/` | Full CRUD, plus actions | The fields that the service owns are read-only. See below |
| `tickets/<id>/transition/` | `GET`, `POST` | `GET` lists the transitions that the ticket allows now. `POST` does one |
| `tickets/<id>/comment/`, `attach/`, `detach/` | `POST` | Add a comment to a ticket. Attach or detach a Nautobot object |
| `ticket-updates/` | `GET`, `HEAD`, `OPTIONS` | Append-only history. A write gives 405 |
| `ingestion-stats/` | `GET`, `HEAD`, `OPTIONS` | The consumer writes it |
| `llm-usage/` | `GET`, `HEAD`, `OPTIONS` | Only the service layer writes it. A write gives 405 |
| `agent-runs/` | `GET`, `HEAD`, `OPTIONS` | The agent service writes it. A write gives 405 |
| `agent-tool-calls/` | `GET`, `HEAD`, `OPTIONS`, plus actions | Read-only, except for the two decisions below |
| `agent-tool-calls/<id>/approve/`, `deny/` | `POST` | Decide a proposed tool call. This needs `approve_agenttoolcall`. The `change` permission does not include it |
| `ticket-embeddings/` | `GET`, `HEAD`, `OPTIONS` | Read-only, without the vector. A user sees only the embeddings of tickets that the user can read |

The routes `ai-providers`, `ai-models`, `mcp-servers` and `mcp-tools` are at `/api/plugins/ai-models/`, in the AI Models app.

To transition a ticket, post to the transition action. Do not patch `status`:

```bash
curl -X POST \
     -H "Authorization: Token $NAUTOBOT_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"to_status": "triaged", "message": "picked up by the on-call"}' \
     https://nautobot.example.com/api/plugins/event-tracker/tickets/<id>/transition/
```

The app refuses a transition that the workflow graph does not allow. The message names the statuses that the ticket can reach from its current status.

To approve a proposed tool call, send the same kind of request. The approval covers exactly what the agent proposed. You cannot send different arguments with an approval:

```bash
curl -X POST \
     -H "Authorization: Token $NAUTOBOT_TOKEN" \
     https://nautobot.example.com/api/plugins/event-tracker/agent-tool-calls/<id>/approve/
```

An approval over REST records the decision and does nothing more. It does not start the resumption run. A Nautobot Job runs the agent. That Job has its own endpoint and its own permission.

### GraphQL

You can query these models at the `/api/graphql/` endpoint of Nautobot: `EventType`, `EventTicket`, `TicketUpdate`, `IngestionStats`, `LLMUsageRecord`, `AgentRun` and `AgentToolCall`. `TicketEmbedding` is not on GraphQL, because GraphQL cannot restrict it to the tickets that a user can read. Use the REST endpoint `ticket-embeddings/`.

GraphQL is read-only, in this app and in Nautobot core:

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
