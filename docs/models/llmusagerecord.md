# LLM Usage Record

An LLM Usage Record is the record of one model call. It shows the model, the purpose, the tokens, the cost, the time and the result. A failed call also has a record.

Use this page to see what the AI features of the app cost. Use it also to find out why a triage decision did not happen.

## Read-only

Only the service layer of the app writes these rows. The web interface has no add form, no edit form and no delete button. The REST endpoint accepts no write method. `POST`, `PATCH`, `PUT` and `DELETE` all return `405 Method Not Allowed`. A change to a record would falsify the account of money that the app already spent.

The service layer deletes each row that is older than the retention period. The setting is `llm.usage_retention_days`. The default is 90 days.

The app does not log changes to these rows, for the same reason as for the ingestion counters. One change log entry for each model call would fill the change log with bookkeeping.

## Fields

The detail page of a record shows the names below. The list shows the same names with a capital letter on the first word only, for example **Prompt tokens**.

| Field | Description |
| --- | --- |
| **Called At** (`called_at`) | The time when the app made the call. The list shows this field as **When**. |
| **Model** (`model`) | The AI Model that the app called. The AI Models app registers it (ADR 0010). |
| **Purpose** (`purpose`) | What the call was for. The value is **Triage**, **Agent** or **Embedding**. |
| **Ticket** (`ticket`) | The ticket that the call was about, if any. If you delete the ticket, the record stays. |
| **Prompt Tokens** (`prompt_tokens`) | The number of prompt tokens that the provider reported. The value is zero if the provider reported nothing. |
| **Completion Tokens** (`completion_tokens`) | The number of completion tokens that the provider reported. The value is zero if the provider reported nothing. |
| **Cost** (`cost`) | The cost in USD. The app computes it from the registered prices of the model and the reported tokens. |
| **Latency Ms** (`latency_ms`) | The time that the call took, in milliseconds. |
| **Success** (`success`) | Shows if the call returned a usable response. A response with no text and no tool call is not usable. |
| **Request Id** (`request_id`) | The identifier that the provider gave to the call. Use it to find the call in the logs of the provider. |
| **Error** (`error`) | Why the call failed, if it did. For an empty response, it shows the finish reason and the token counts. |

## Elsewhere

The REST API serves the same rows at `/api/plugins/event-tracker/llm-usage/`. GraphQL also serves them. The **LLM Usage** panel of a ticket page shows the calls about that ticket.
