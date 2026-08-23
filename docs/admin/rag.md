# Retrieval over Closed Tickets

When a ticket closes it becomes a document: what happened, and what was done about it. New tickets
are matched against those documents, and the closest few appear on the ticket page under **Similar
Tickets**.

The point is the three-in-the-morning case. Somebody solved this exact thing in March and wrote
down what they did. Nobody now on shift knows that.

## What this does not do

**Nothing retrieved is ever put into a prompt.** Not triage's, not the [agent's](agents.md). A
person reads the panel and decides what it is worth.

That is a deliberate refusal, and it costs something real — an agent that knew how this was fixed
last time would be a better agent. The reason is reach. Your corpus is built from tickets whose
payloads were written by whoever can put a line on a topic you consume. One ticket being read by a
model was already true. Indexing changes it: a document that steers one investigation becomes a
document that can be retrieved for *every future ticket that resembles it*, and neither you nor the
model can see that it happened. Somebody being noisy enough that a ticket gets closed to clear the
board is all it takes.

A panel keeps a person in the loop, for the price of them having to read it. The app asserts this in
its test suite rather than trusting itself to remember.

## Before you start

### 1. The database extension

This needs the `pgvector` extension, which [ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md)
has required since Phase 1 and this is the phase that spends.

The app's migration creates it. **On a managed PostgreSQL that will probably fail**, because
`CREATE EXTENSION` needs superuser or `pg_database_owner` and most managed services grant the
application user neither. If your database is managed, have a superuser run this **before** you
upgrade:

```sql
CREATE EXTENSION vector;
```

The migration checks first and does nothing when the extension is already there, so doing this is
always safe. If you skip it and the migration cannot create it, the upgrade stops with a message
telling you this — which is better than the alternative, but it stops mid-upgrade.

### 2. An embedding model

Register it like any other, with **Kind** set to *Embedding*:

- **LLM Provider** — an existing one is fine; embeddings and chat can share a provider.
- **LLM Model** — the model name, Kind *Embedding*. Set the input cost; leave the output cost at
  zero, since an embedding has no output side to price.

Kind is enforced in both directions. Point triage at an embedding model and it refuses before any
network traffic, rather than handing you a provider error about token limits at three in the
morning.

## Switching it on

```python
PLUGINS_CONFIG = {
    "nautobot_event_tracker": {
        "rag": {
            "enabled": True,
            "provider": "Local Ollama",      # LLMProvider name
            "model": "nomic-embed-text",     # an LLMModel of Kind = Embedding on it
        },
    },
}
```

Everything else has a default:

| Key | Default | What it does |
| --- | --- | --- |
| `max_document_chars` | 8000 | How much of a ticket gets embedded. |
| `similar_count` | 5 | How many neighbours the panel shows. |
| `max_distance` | 0.15 | Cosine distance past which the answer is "we have not seen this". |
| `timeout_seconds` | 30 | One embedding call. |

### Tuning `max_distance`

This is the one worth measuring rather than guessing, and the default is low on purpose.

A nearest-neighbour search always has a nearest neighbour. Without a threshold the panel
confidently shows the five least-unlike tickets in your database, and everybody learns to ignore
it. Short network tickets sit closer together in embedding space than intuition suggests, because
they share vocabulary — device names, "down", "alarm" — even when the problems are unrelated.

Measured against `nomic-embed-text` with real tickets:

| Query | Nearest match |
| --- | --- |
| An interface-down ticket, against an interface-down resolution | **0.09** |
| A PSU ticket, against a PSU resolution | **0.10** |
| A billing query, against a corpus of network faults | 0.22 |
| "The coffee machine in the London office is broken" | 0.23 |

So on this model the useful band ends around 0.15, and a first draft of this feature shipped 0.6 —
which would have shown the coffee machine and called it very similar.

**Your numbers will differ**, because the distribution is a property of the embedding model. To
find yours: index a handful of closed tickets, open a ticket you know matches one of them and one
you know matches none, and look at the distances in the run's log or via
`services.rag.similar_tickets`. Put the threshold between the two clusters, and err low — a quiet
panel is ignored far less often than one that cries wolf.

## Indexing on close

Create a **Job Hook** so tickets are indexed as they close:

**Extensibility → Job Hooks → Add.** Content type *Event Ticket*, check **Type update**, and choose
the **Index a Closed Event Ticket** job. Enable the job itself first, under **Jobs** — Nautobot
registers newly installed jobs disabled.

Indexing never prevents a close. If the model endpoint is down the close happens anyway and the
failure is logged; the ticket can be indexed later with the backfill below.

## Backfilling

For the year of closed tickets you already have:

```shell
nautobot-server indexclosedtickets            # everything not yet indexed
nautobot-server indexclosedtickets --limit 20 # try a few first
nautobot-server indexclosedtickets --reindex  # everything, again
```

`--reindex` is what you want after changing embedding model. Every existing row belongs to the old
model and will never be compared with a new one, so the panel goes quiet until the corpus is
rebuilt. That is the safe failure of the two available, and it is why the model is recorded on
every row.

## Reading the panel

**Similar Tickets** appears on open tickets only — a closed ticket's neighbours are of historical
interest at best. Each entry shows the ticket, how close it is in words rather than a number, and
its resolution.

When it shows nothing, that is an answer: nothing in the corpus is within `max_distance`.

## Where to look when it is quiet

- **Apps → Event Tracker → Ticket Embeddings** — what is in the corpus, and under which model.
- **Apps → Event Tracker → LLM Usage** — filter on purpose *Embedding* to see the calls and what
  they cost.
- The Job Hook's own results, under **Jobs → Job Results**.

## Further reading

- [Ticket Embedding](../models/ticketembedding.md) — the record
- [ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md) — why PostgreSQL only
- [Running the Agent](agents.md) — the other AI surface, and the one this deliberately does not feed
