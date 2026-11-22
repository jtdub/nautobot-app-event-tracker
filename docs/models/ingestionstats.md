# Ingestion Stats

An Ingestion Stats row records what one consumer saw on one topic in one window of time. Use it when you expected a ticket and did not get one.

## Read-only

Only the consumer writes these rows. The web interface has no add form, no edit form and no delete button. The REST endpoint accepts no write method. `POST`, `PATCH`, `PUT` and `DELETE` all return `405 Method Not Allowed`. The counters must show what happened, so nobody can change them.

The consumer deletes each row that is older than the retention period. The setting is `ingestion.stats_retention_days`.

The app does not log changes to these rows. The consumer rewrites the row of the current window every few seconds. A change log entry for each write would fill the change log with entries about counters.

## Fields

The detail page of a row shows the names below. To open it, click **Window** in the list. The list shows the same names with a capital letter on the first word only, for example **Tickets opened**.

| Field | Description |
| --- | --- |
| **Consumer Name** (`consumer_name`) | The consumer process that made these counts. If you set no name, the consumer uses `hostname:pid`. |
| **Topic** (`topic`) | The broker topic or channel. |
| **Bucket Start** (`bucket_start`) | The start of the period that these counts cover. The list shows this field as **Window**. |
| **Received** (`received`) | The messages that the consumer took from the broker. It counts each message, whatever happened to it. |
| **Tickets Opened** (`tickets_opened`) | The messages that opened a new ticket. |
| **Tickets Joined** (`tickets_joined`) | The messages that matched the dedup key of an open ticket. Each one became a recurrence. |
| **Suppressed** (`suppressed`) | The messages that a suppression rule or LLM triage suppressed. The app also counts them under **Tickets Opened** or **Tickets Joined**. |
| **Dropped** (`dropped`) | The messages that the pre-filter or LLM triage dropped. |
| **Errored** (`errored`) | The messages that the consumer could not read. The body is not JSON, or it is not a JSON object. |
| **Triaged** (`triaged`) | The messages that LLM triage judged. Triage does not judge a recurrence or a message from a topic without triage. This value is also the count of model calls. |
| **Triage Attached** (`triage_attached`) | The messages that triage attached to a ticket that already exists. The app also counts them under **Tickets Joined**. |
| **Triage Errors** (`triage_errors`) | The triage runs that failed or that gave an unusable answer. In each case, the app accepted the event. |
| **Enriched** (`enriched`) | The messages that attached at least one object that the enrichment resolver found. The app also counts them under **Tickets Opened** or **Tickets Joined**. |
| **Enrichment Misses** (`enrichment_misses`) | The enrichment rules that found nothing but should have found something. It counts rule evaluations, not messages. |
| **Drops by reason** (`drops_by_reason`) | The drop counts, by the rule or the filter that dropped each message. LLM triage drops use the key `llm_triage`. |
| **Last Message At** (`last_message_at`) | The broker timestamp of the newest message that the row counts. |

## How to read the numbers {#reading-the-numbers}

Every message ends in exactly one of four places:

```
Received = Errored + Dropped + Tickets Opened + Tickets Joined
```

**Suppressed** is not a term in this sum. A suppressed message still opens a ticket or joins a ticket. The app counts it there too. The column shows how many of those messages a suppression rule or LLM triage suppressed.

The three triage columns are also outside the sum. **Triage Attached** counts messages that **Tickets Joined** already counts. A triage run that failed still accepted its event.

The two enrichment columns are also outside the sum, and they count different things on purpose. **Enriched** counts messages. One event that attached a device and an interface counts one time. **Enrichment Misses** counts rule evaluations. One event with two rules that both found nothing counts two times. Neither column counts a rule that the app skipped. The app skips a rule when the payload says there is no interface. It also skips a rule when the app does not find the device that the rule needs.

The **Drops by reason** panel is the most useful. It is on the detail page of a row, and only when the row has drops. Each key is the name of a filter or the name of a rule that an administrator wrote. The filter names are `unknown_topic`, `unknown_event_type`, `event_type_disabled`, `below_severity_floor` and `rate_limited`. If you do not know a key, someone configured a rule with that name.

The consumer keeps the counters in memory and writes them every few seconds. A recent window can show a count that is lower than the real count. If the consumer process stops without warning, the app loses at most the counts of one flush interval. This is intentional. A lost count is a better result than a lost ticket.

## Elsewhere

The REST API serves the same rows at `/api/plugins/event-tracker/ingestion-stats/`. GraphQL also serves them. You can filter them by consumer name, topic and window.

To configure what the consumer does, see [Running the Event Consumer](../admin/ingestion.md).
