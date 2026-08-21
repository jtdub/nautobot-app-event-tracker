# Ingestion Stats

An Ingestion Stats row records what one event consumer saw, on one topic, during one window of
time. It is the page to look at when you expected a ticket and did not get one.

## Read-only

Nothing outside the consumer writes these rows. There is no add form, no edit form and no delete
button, and the REST endpoint accepts no write method — `POST`, `PATCH`, `PUT` and `DELETE` all
return `405 Method Not Allowed`. Editing a counter would be falsifying a record of what happened.

Rows are pruned automatically once they pass the configured retention, by the consumer itself.

They are also not change-logged. The consumer rewrites the current window's row every few seconds,
so an entry in the change log per write would bury it under a record of arithmetic.

## Fields

| Field | Description |
| --- | --- |
| Consumer Name | Which process these counts came from. Defaults to `hostname:pid`. |
| Topic | The broker topic or channel. |
| Window | The start of the period these counts cover. |
| Received | Messages taken from the broker, whatever became of them. |
| Tickets Opened | Messages that opened a new ticket. |
| Tickets Joined | Messages that matched an open ticket's dedup key and became a recurrence. |
| Suppressed | Messages a suppression rule accepted. Counted under opened or joined as well. |
| Dropped | Messages the pre-filter or LLM triage discarded. |
| Errored | Messages that could not be read — not JSON, or not a JSON object. |
| Triaged | Messages LLM triage actually judged. Recurrences and opted-out topics are not judged, so this is also the model-call count. |
| Triage Attached | Messages triage attached to an existing ticket. Counted under Tickets Joined as well. |
| Triage Errors | Triage calls that failed and fell back to accepting the event. |
| Enriched | Messages that attached at least one object the enrichment resolver found. Counted under opened or joined as well. |
| Enrichment Misses | Enrichment rules that found nothing they should have found. Counts rule evaluations, not messages. |
| Drops by Reason | Drop counts broken down by the rule or filter that refused each message. Triage drops appear under `llm_triage`. |
| Last Message At | The broker timestamp of the newest message counted here. |

## Reading the numbers

Every message ends in exactly one of four places:

```
Received = Errored + Dropped + Tickets Opened + Tickets Joined
```

**Suppressed** is not a term in that sum. A suppressed message still opened or joined a ticket, and
is counted there too; the column says how many of those arrived through a suppression rule. The
three **Triage** columns are the same kind of commentary: an attached message is already counted
under joined, and an errored triage call still accepted its event.

The two **Enrichment** columns are commentary of the same kind, and they count different things
from each other on purpose. **Enriched** counts messages: one event that attached a device and an
interface is one. **Enrichment Misses** counts rule evaluations: one event whose two rules both
found nothing is two. A rule that was skipped rather than failed — because the payload said there
was no interface, or because the device it would have been scoped to was not found — is neither.

**Drops by Reason** is the useful one. Keys are either a filter's name — `unknown_topic`,
`event_type_disabled`, `below_severity_floor`, `rate_limited` — or the name of a rule an
administrator wrote. A key you do not recognise is a rule someone configured; the name is theirs.

Counters are held in memory and written every few seconds, so a very recent window may lag slightly
behind reality, and an abrupt kill loses at most one flush interval. Both are deliberate: a lost
count is a much better outcome than a lost ticket.

## Elsewhere

The same rows are available at `/api/plugins/event-tracker/ingestion-stats/` and through GraphQL,
filterable by consumer name, topic and window.

Configuring what the consumer does is covered in
[Running the Event Consumer](../admin/ingestion.md).
