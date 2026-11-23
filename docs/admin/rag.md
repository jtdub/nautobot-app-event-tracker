# Retrieval over Closed Tickets

When a ticket closes, the app turns it into a document. The document holds what happened and what a person did
about it. The app matches each new ticket against these documents. The panel **Similar Tickets** on the ticket
page shows the closest few.

The panel helps when a person on shift sees a fault that a colleague already solved. The colleague wrote down
the fix. The person on shift does not know about it.

## What this does not do

**The app never puts retrieved text in a prompt.** This applies to triage and to the [agent](agents.md). A person
reads the panel and decides what a result is worth.

The refusal is on purpose. An agent that knew the last fix would be a better agent. The corpus comes from tickets.
Whoever can write a line to a topic that you consume also writes the payload of a ticket.

A model already reads one ticket at a time. When the app indexes a ticket, the ticket can reach more investigations.
A document that steers one investigation can also steer each future ticket that looks like it. Neither you nor the
model can see that this happened.

A noisy source is enough. A person closes the ticket to remove it from the list of open tickets, and the ticket
enters the corpus.

A person must read the panel before a result has an effect. The test suite of the app checks this rule (R9).

## What you need first {#before-you-start}

### 1. The database extension

Retrieval needs the `pgvector` extension. [ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md) requires
PostgreSQL with this extension.

The migration of the app creates the extension.

!!! warning
    On a managed PostgreSQL, the migration can fail. `CREATE EXTENSION` needs superuser or `pg_database_owner`,
    and most managed services give the application user neither. Before you upgrade, ask a superuser to run the
    command below.

```sql
CREATE EXTENSION vector;
```

The migration first checks for the extension. If the extension exists, the migration does nothing. It is safe to
run the command before the upgrade.

If you skip the command and the migration cannot create the extension, the upgrade stops at that migration. The
message names the command to run.

### 2. An embedding model

Register the model like any other model, with **Kind** set to **Embedding**:

- **AI Provider**: Use an existing provider. Embeddings and chat can share a provider.
- **AI Model**: Enter the model name. Set **Kind** to **Embedding**. Set **Input cost per million tokens**. You
  can leave **Output cost per million tokens** empty. The app does not use it for an embedding, because an
  embedding has no output tokens.

The app checks **Kind** in both directions. If you point triage at an embedding model, the app refuses before it
sends any network traffic. The app does not wait for a provider error about token limits.

## Turn on retrieval {#switching-it-on}

Set `enabled`, `provider`, and `model`. The value of `provider` is the name of an **AI Provider**. The value of
`model` is the name of an **AI Model** on that provider, with **Kind** set to **Embedding**.

```python
PLUGINS_CONFIG = {
    "nautobot_event_tracker": {
        "rag": {
            "enabled": True,
            "provider": "Local Ollama",      # AI Provider name
            "model": "nomic-embed-text",     # an AI Model of Kind = Embedding on it
        },
    },
}
```

Each other setting has a default:

| Key | Default | What it does |
| --- | --- | --- |
| `max_document_chars` | 8000 | The number of characters of a ticket that the app embeds. |
| `similar_count` | 5 | The number of similar tickets that the panel shows. |
| `max_distance` | 0.15 | The cosine distance above which the panel does not show a ticket. |
| `timeout_seconds` | 30 | The time limit, in seconds, of one full embedding call. See [The time limit of an embedding call](#embedding-time-limit). |

### The time limit of an embedding call {#embedding-time-limit}

An embedding call makes one attempt and stops after one `timeout_seconds`, on each provider type. The limit is
for the full call, from the name lookup to the answer. The app does not send the call again after a timeout, a
connection error, or an HTTP 408, 409, 429, or 5xx answer.

When a call fails, this occurs:

- At ticket close, the ticket stays out of the index. The Job Result of the Job Hook says that nothing was indexed.
  The Nautobot log has a warning.
- The Similar Tickets panel does not show for that page load.
- To index each closed ticket that has no embedding, run `nautobot-server indexclosedtickets`. When the provider
  gives HTTP 429 answers, use `--limit` to index small batches. See [Backfill the corpus](#backfilling).

To get one attempt, the app sets the litellm value `DEFAULT_MAX_RETRIES` to 0 in each Nautobot process that makes an
embedding call. This value replaces the `DEFAULT_MAX_RETRIES` environment variable for embedding calls. Chat calls
(triage and the agent) do not use this value. Other code in the same process that makes an embedding call through
litellm gets the same value.

!!! note "REQUEST_TIMEOUT is not necessary"

    An earlier release told you to set the environment variable `REQUEST_TIMEOUT` for an Ollama provider. The
    app now stops an embedding call on Ollama after `timeout_seconds`. You can remove the variable.

### Tune `max_distance` {#tuning-max_distance}

Measure this setting. Do not guess it. The default is low on purpose.

A nearest-neighbour search always finds a nearest neighbour. Without a threshold, the panel shows the five
least-different tickets in your database. Then everyone learns to ignore the panel. Short network tickets are
closer together in embedding space than you expect. They share words, such as device names, "down", and "alarm",
even when the problems are not related.

The table shows distances from a test with `nomic-embed-text` and real tickets:

| Query | Nearest match |
| --- | --- |
| An interface-down ticket, against an interface-down resolution | **0.09** |
| A PSU ticket, against a PSU resolution | **0.10** |
| A billing query, against a corpus of network faults | 0.22 |
| "The coffee machine in the London office is broken" | 0.23 |

On this model, the useful band ends at about 0.15. If `max_distance` is 0.6, the panel shows the coffee machine ticket as a
match.

**Your numbers can differ.** The distances depend on the embedding model. To find your numbers:

1. Index a few closed tickets.
2. Set `max_distance` to `2` for the test. Restart Nautobot.
3. Open a ticket that you know matches one of the closed tickets. Open a ticket that you know matches none of them.
4. On each ticket page, read the number after the word in each **Similar Tickets** entry, for example
   `(similar, 0.12)`.
5. Set `max_distance` between the two groups of distances. Restart Nautobot.

Prefer a low value. People ignore a panel that shows wrong matches far more often than a panel that shows few
matches.

## Index tickets when they close {#indexing-on-close}

Create a **Job Hook**. Then the app indexes each ticket when it closes.

1. Enable the **Index a Closed Event Ticket** Job. Nautobot registers a newly installed Job as disabled.
    1. In **Jobs > Jobs**, open the row menu of the Job.
    2. Click **Edit job**.
    3. Select **Enabled**.
    4. Click **Update**.
2. Go to **Jobs > Jobs > Job Hooks**.
3. Click **Add Job Hook**.
4. In **Name**, enter a name, for example `Index closed tickets`.
5. In **Content Type(s)**, select **Event Tracker | Event Ticket**.
6. In **Job**, select **Index a Closed Event Ticket**.
7. Select **Type update**.
8. Click **Create**.

The index operation never prevents a close. If the model endpoint is down, the ticket closes and the app logs
the failure. Index the ticket later with the backfill below.

## Backfill the corpus {#backfilling}

Use the command to index the closed tickets that you already have:

```shell
nautobot-server indexclosedtickets            # everything not yet indexed
nautobot-server indexclosedtickets --limit 20 # try a few first
nautobot-server indexclosedtickets --reindex  # everything, again
```

Use `--reindex` after you change the embedding model. Every existing row belongs to the old model. The app never
compares these rows with the new model, so the panel shows no match until you index the corpus again. This is the safe
result, because the panel shows no match instead of wrong matches. It is also the reason that the app records the
model on every row.

## Read the panel {#reading-the-panel}

A ticket page shows the closed tickets that look like the open ticket.

![A ticket page with the Similar Tickets panel, which lists two closed tickets that look like the open ticket](../media/ss_similar_tickets_light.png#only-light)
![A ticket page with the Similar Tickets panel, which lists two closed tickets that look like the open ticket](../media/ss_similar_tickets_dark.png#only-dark)

**Similar Tickets** shows only on a ticket that is not resolved or closed. It shows only when the app finds a
match. Each entry shows the ticket, a word and a number that tell how close it is, and its resolution.

If the panel does not show on an open ticket, one of these is true:

- No closed ticket is within `max_distance`.
- Retrieval is off.
- The `rag` settings are not valid.
- The app cannot find the embedding model, or **Kind** of the model is wrong.
- The app could not embed the ticket.
- The user cannot view the closed ticket that matches.

## Where to look when the panel does not show {#where-to-look-when-it-is-quiet}

- **AI Tools > Event Tracker AI > Ticket Embeddings**: the corpus, and the model of each row.
- **AI Tools > Event Tracker AI > LLM Usage**: Filter **Purpose** on **Embedding** to see the calls and their cost.
- **Jobs > Jobs > Job Results**: the results of the Job Hook.

## When the corpus gets large

The vector column has no index. Every similarity query is a sequential scan. This is the right choice while the
corpus is small. At some size, it is not the right choice. The corpus of this app is not that large yet.

Measure before you change it. The query of the panel is one `ORDER BY embedding <=> %s LIMIT n` for each render.
Time it against your own corpus:

```sql
EXPLAIN ANALYZE
SELECT id FROM nautobot_event_tracker_ticketembedding
ORDER BY embedding <=> (SELECT embedding FROM nautobot_event_tracker_ticketembedding LIMIT 1)
LIMIT 5;
```

If the time is low enough for a ticket page, do nothing. If it is not, the next step is an HNSW index. Note two
things before you add one:

- An HNSW index needs a fixed number of dimensions. Build it only after you choose an embedding model and index
  everything again with that model.
- An HNSW index is approximate. It can miss a real neighbour. For a panel that suggests and does not decide, this
  loss is acceptable.

## Further reading

- [Ticket Embedding](../models/ticketembedding.md): the record
- [ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md): why PostgreSQL only
- [Running the Agent](agents.md): the other AI surface. Retrieval does not feed it
