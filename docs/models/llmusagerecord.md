# LLM Usage Record

An LLM Usage Record is the accounting row for one model call: which model, what for, how many
tokens, what it cost, how long it took, and whether it worked. Failed calls are recorded too —
a retry storm against a broken endpoint shows up here, priced.

It is the page to look at when you want to know what the app's AI features are costing, or why a
triage decision did not happen.

## Read-only

Nothing outside the app's own LLM service writes these rows. There is no add form, no edit form
and no delete button, and the REST endpoint accepts no write method — `POST`, `PATCH`, `PUT` and
`DELETE` all return `405 Method Not Allowed`. Editing one would be falsifying an account of money
already spent.

Rows are pruned automatically once they pass the configured retention (`llm.usage_retention_days`,
default 90 days), by the service itself.

They are also not change-logged, for the same reason ingestion counters are not: one change log
entry per model call would bury the change log under bookkeeping.

## Fields

| Field | Description |
| --- | --- |
| When | The moment the call was made. |
| Model | The [LLM Model](llmmodel.md) that was called. |
| Purpose | What the call was for — `triage` today; later phases add more. |
| Ticket | The ticket the call was about, when there was one. Deleting the ticket keeps the record. |
| Prompt / Completion Tokens | Token counts as the provider reported them. Zero when the provider reported nothing. |
| Cost | USD, computed from the model's registered prices and the reported tokens. |
| Latency | How long the call took, in milliseconds. |
| Success | Whether the call returned a usable response. |
| Request ID | The provider's own identifier for the call, for finding it in provider-side logs. |
| Error | Why the call failed, when it did. |

## Elsewhere

The same rows are available at `/api/plugins/event-tracker/llm-usage/` and through GraphQL. A
ticket's own calls appear in the LLM Usage panel on its detail page.
