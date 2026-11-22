# Configuring LLM Providers

The app calls language models through a registry it does not own. **AI Provider** and **AI Model**
belong to the [AI Models app](https://github.com/jtdub/nautobot-app-ai-models), which this app
depends on (ADR 0010): a provider is an endpoint you allow, a model is one available from it, and
both are managed under **AI Tools → AI Models**. What this app records is the other half — every
call it makes is priced and written to an [LLM Usage Record](../models/llmusagerecord.md), under
**AI Tools → Event Tracker AI**.

Nothing here is required: an installation that registers no provider simply makes no model calls.
The AI Models app is installed with this one and needs no settings of its own for the LLM half.

All calls go through [litellm](https://docs.litellm.ai/), which is an optional dependency. Install
the app with the `llm` extra to get it:

```shell
pip install nautobot-event-tracker[llm]
```

Without the extra, any attempt to call a model fails with a message naming that command.

## Registering a provider

A provider's connection details live in a Nautobot **External Integration**, so credentials are
stored and rotated where every other credential in your deployment is.

1. Create a **Secrets Group** holding the API key: one secret with access type *Generic* and
   secret type *Token* (a *Secret*-typed entry works too). Skip this for an endpoint that needs
   no key.
2. Create an **External Integration**: the *Remote URL* is the endpoint's base URL (for an
   OpenAI-compatible endpoint, the URL up to and including `/v1`; for Ollama, without it), and the
   *Secrets Group* is the one from step 1.
3. Create an **AI Provider** (AI Tools → AI Models → AI Providers) pointing at that
   integration, choosing the provider type:
    - **OpenAI-compatible** — any self-hosted or third-party endpoint speaking the OpenAI
      protocol: vLLM, llama.cpp, a gateway. The integration must carry a remote URL. This is the
      first-class path for deployments that cannot send event data to a third party.
    - **Ollama** — Ollama specifically, reached through its own API rather than its
      OpenAI-compatibility layer. Give the integration Ollama's base URL with **no `/v1`**
      (`http://ollama.example.com:11434`); a key is not needed, though one is sent if you
      configure it, for an Ollama behind an authenticating proxy.

        Use this rather than OpenAI-compatible if you want the [agent](agents.md) to call tools.
        Ollama's compatibility layer does not return tool calls in the `tool_calls` field — a
        model asked for a tool answers with the JSON call written into the message content, where
        nothing can act on it. Triage works either way, since it asks for no tools.
    - **OpenAI** / **Anthropic** — the hosted services. Nautobot requires a remote URL on every
      external integration, so give it the service's own base URL
      (`https://api.openai.com/v1`, `https://api.anthropic.com`).
4. Create one or more **AI Models** on the provider, or run the AI Models app's
   **Discover AI Models** Job against it, which reads `GET /v1/models` from any OpenAI-compatible
   endpoint and creates a row per model it offers.

    Two fields need a person either way. **Kind** says whether a model is for chat or for
    embeddings; discovery cannot tell, because `/v1/models` does not say, so every discovered row
    arrives as one kind and the [retrieval](rag.md) settings will refuse a model of the wrong one.
    **Input** and **output cost per million** are what make usage records carry real prices — they
    are your numbers, and leaving them empty records the call with a cost of zero rather than
    guessing.

The integration decides more than the address. Four of its fields are read on every call:

| Field | What it does |
| --- | --- |
| *Remote URL* | The endpoint. Jinja2 templating works; `{{ obj }}` is the AI Provider |
| *Secrets Group* | The API key, preferring the *Token* secret type and falling back to *Secret* |
| *Headers* | Sent with every request, templated the same way. The template also knows `{{ session_id }}`: the ID of the first run of an agent investigation, the consumer name of a triage call, or a random ID of the process for each other call |
| *SSL Verification* | **Not applied to LLM calls** — see below |
| *CA File Path* | **Not applied to LLM calls** — see below |
| *Timeout* | The call's timeout, unless the model row or the caller states one |

*Extra Config* is deliberately not sent. It is untyped, and splatting it into a call would reopen
the hole that limiting a model's parameters closed.

!!! warning "TLS settings on an LLM provider's integration are not applied"
    litellm takes no per-call TLS argument. It reads the `SSL_VERIFY` and `SSL_CERT_FILE`
    environment variables, or its own process-wide global, and nothing else — so *SSL Verification*
    and *CA File Path* on the integration cannot be honoured for one provider without changing
    every other provider's calls in the same process. A worker runs several at once, so applying
    one provider's setting would silently disable verification on another's connection.

    To reach an LLM endpoint with a **private CA**, set `SSL_CERT_FILE` in the environment of
    every process that calls a model — Nautobot, the worker and the event consumer. That is
    additive and safe: it adds a trust anchor without weakening anything.

    There is **no recommended way to disable verification for one provider**. `SSL_VERIFY=False`
    would work, and it turns verification off for every provider in that process — a wider blast
    radius than the per-provider setting this app declines to apply. If an endpoint's certificate
    cannot be verified, fix the certificate or add its CA.

    The app logs a warning naming the fields it skipped whenever either is set, so this is visible
    rather than silent.

    MCP servers are not affected: `services/mcp.py` builds its own HTTP client per call and does
    honour both fields.

Disabling a provider or a model (the *Enabled* flag on either) refuses every call through it
before any network traffic, everywhere at once.

A model's **Default parameters** and its *Temperature* and *num_predict* columns are passed on
every chat call. The parameters field takes generation parameters only — the endpoint and the
credentials come from the integration, and the model and the messages come from the call — and the
AI Models app refuses anything else at save time. Where both are set, an entry in *Default
parameters* wins over the column of the same name, and a model's own value wins over the
provider's default.

## OpenCode Go

[OpenCode Go](https://opencode.ai/docs/en/go/) is a hosted service with an OpenAI-compatible API.
It needs two settings that other endpoints do not need. The values in this section worked in a
test on 2026-11-18 with the model `deepseek-v4.1-flash`. The test used no other Go model.

!!! warning "Read the Go usage policy before you enable triage"
    Unattended triage can use all of the allowance of your subscription. OpenCode says that Go
    is for agents that write code, and that Go monitors traffic for abuse. Go has a 5-hour limit
    and a weekly limit. Read the [usage limits of Go](https://opencode.ai/docs/en/go/#usage-limits)
    before you continue.

    The LLM Usage list does not show the Go allowance. It shows the cost from the prices on the
    AI Model row (see [Watching the spend](#watching-the-spend)). Use the OpenCode console to
    see the allowance. If Extra Usage is on in your OpenCode account, Go does not stop at a
    limit. Go charges your account credit.

    When Go stops a call, triage accepts the event. See "Failure is safe" in
    [Enabling event triage](#enabling-event-triage).

    The Go facts in this warning come from the vendor page on 2026-11-19. They can change.

!!! warning
    With the `session_id` template in the *Headers* field, Go sees the consumer name of each triage call. The default
    consumer name holds the host name and the process number. Set `ingestion.consumer_name` to a
    text that names nothing, and give each consumer a different name. See
    [Ingestion](ingestion.md).

Follow the steps in [Registering a provider](#registering-a-provider) with these values:

| Object | Field | Value |
| --- | --- | --- |
| External Integration | *Remote URL* | `https://opencode.ai/inference/go/v1` |
| External Integration | *Headers* | The JSON under this table |
| External Integration | *Secrets Group* | The group that holds your Go API key |
| AI Provider | *Provider type* | **OpenAI-compatible** |
| AI Model | *Name* | `deepseek-v4.1-flash` |
| AI Model | *Kind* | *Chat* |
| AI Model | *Default parameters* | `{"extra_body": {"reasoning_effort": "none"}}` |

Put this JSON in the *Headers* field:

```json
{"x-opencode-session": "{{ session_id | default('nautobot-discovery') }}"}
```

Make the AI Model row by hand. The model list of Go also has models that use a different API.

To check the URL, read the model list. This call needs no key:

```shell
curl https://opencode.ai/inference/go/v1/models
```

The answer is a JSON list of the models that Go offers.

**Go needs the session header.** Go refuses a call that does not have the
`x-opencode-session` header. The error is `MissingSessionID`. The template in the table gives
the header a value on every call.

- A fixed value, such as `nautobot-event-tracker-prod`, continues to work. The app then sends
  the same value with every call.
- An agent call sends the ID of the first run of the investigation. A run that resumes after
  an approval sends the same ID.
- A triage call sends the consumer name. The same consumer sends the same value each time.
- Any other call sends one random ID for each process.
- The app replaces each character other than `A-Z a-z 0-9 . _ : -` with `-`. It cuts the
  value at 128 characters.
- Go sees one session for each agent investigation, one for each consumer name, and one for each
  other process. This is the purpose of the template. The Go console then shows each session
  apart.

A renderer that does not know `session_id` sends the literal text `{{ session_id }}`. The
registry Job "Discover AI Models" and an MCP server integration are such renderers. The
`default` filter, with single quotes, gives them a value. Do not use double quotes in the
filter. Double quotes break the stored JSON, and the app then refuses each call.

After you change the Headers, make one agent run or send one event. Open the LLM Usage list and
read the record of that call. If the record shows a session error, put the fixed value back.

If no new record appears, the app did not send the call. A template that does not render stops
the call before the app writes a record. Look for the text "has a template that does not
render" in the result of the agent run or in the log of the consumer. Then put the fixed value
back.

**Stop reasoning for triage.** `deepseek-v4.1-flash` reasons before it answers. The reasoning
tokens count against [`max_output_tokens`](#enabling-event-triage). With a limit of 256 tokens,
which was the default at that time, 2 of 14 triage answers were empty in the test. The
`extra_body` value in the table stops the reasoning. A top-level `reasoning_effort` key gives
the same request. If the AI Model has the two forms, the `extra_body` value applies. The
Default parameters apply to each call to this AI Model, agent calls included.

**Go has no embedding model.** [Retrieval](rag.md) needs an embedding model on a different
provider.

**Some models need the Global region.** OpenCode says that `deepseek-v4.1-flash` is available
only when the privacy settings of your OpenCode workspace permit the Global region. Triage sends
the event payload to the model. Read "Cost and privacy" in
[Enabling event triage](#enabling-event-triage) before you permit the region.

## Settings

The service layer itself has one setting, under the app's `llm` key:

```python
PLUGINS_CONFIG = {
    "nautobot_event_tracker": {
        "llm": {
            "usage_retention_days": 90,
        },
    },
}
```

| Setting | Default | Description |
| --- | --- | --- |
| `usage_retention_days` | `90` | Usage records older than this are deleted automatically, by the service itself, at most once per process per day. |

Providers, models and credentials are deliberately **not** settings — they are the registry
objects above, in the AI Models app, and the API key never appears in `PLUGINS_CONFIG` or in a
log line.

## Enabling event triage

With a provider and model registered, the event consumer can ask the model to triage every event
the [pre-filter](ingestion.md) accepts: open a ticket, attach the event to an open ticket,
suppress it, or drop it. Configure it inside the `ingestion` block:

```python
"ingestion": {
    ...,
    "triage": {
        "enabled": True,
        "provider": "Local Lab",        # AI Provider name
        "model": "llama-3.1-70b",       # AI Model name on that provider
        "timeout_seconds": 15,
        "max_output_tokens": 1024,      # used when the registry has no limit
        "max_context_chars": 4000,      # payload cap in the prompt
        "attach_candidates": 5,         # open tickets the model may attach to
        "model_cache_seconds": 60,      # how long a running consumer holds the model row
    },
    "topics": {
        "network.events": {..., "triage": True},   # per-topic; default True
    },
},
```

The consumer refuses to start when the named provider or model does not exist or is disabled, or
when the app was installed without the `llm` extra, alongside every other configuration fault. A
`--dry-run` skips that last check, because a dry run makes no model call at all.

`model_cache_seconds` is how stale a running consumer's copy of the model row may be. It reads the
row once and holds it for that long rather than joining before every event, which means unticking
**Enabled** — or correcting a price — takes up to that long to reach a consumer that is already
running. Lower it if you want the off switch to bite sooner; raise it on a very busy consumer.

Triage sends a limit of output tokens with each call. It finds the limit in two steps:

1. It reads *num_predict* of the AI Model. If that field is empty, it reads *Default num_predict*
   of the AI Provider.
2. If the value is 1 or more, that value is the limit. If the value is empty, 0 or -1, the limit
   is `max_output_tokens` in the `triage` settings.

A value on the AI Model hides the default of the AI Provider. For example, -1 on the model gives
`max_output_tokens`, also when the provider has a default. A value of -1 does not remove the limit
for triage.

A reasoning model uses tokens from this limit before it writes the answer, so a small limit can
give an empty answer. The default of 1024 leaves room for that reasoning. A model that does not
reason writes one small JSON object, and the limit does not change its cost. To give one reasoning
model more room, set *num_predict* on its AI Model. A running consumer uses the new value within
`model_cache_seconds`.

The agent uses the same *num_predict* value. If triage and the agent use the same AI Model, one
value limits the two. To use different limits, register the model on a second AI Provider, which
can use the same External Integration. The two rows then have their own prices, *Enabled* flags
and Default parameters, and the LLM Usage list shows them as two models.

`timeout_seconds` is the second limit. A slow model can reach the timeout before it reaches the
token limit. If you increase the token limit for a reasoning model, also check `timeout_seconds`.
When a call reaches one of the two limits, triage accepts the event.

What to know before you turn it on:

- **Cost and privacy.** Each surviving event makes one model call, whose prompt carries the
  event's payload (capped at `max_context_chars`). Set `triage: False` on a topic whose payloads
  must not leave the box, or point the provider at a self-hosted endpoint. Recurrences are free:
  an event whose dedup key matches an open ticket joins it without a model call.
- **Triage is a filter, not a control.** The prompt carries text that whoever emits the events
  controls, so an event can try to argue for its own verdict. The app constrains the *shape* of
  the answer — four actions, and a ticket the app itself shortlisted — but not its reasoning.
  Read a triage `drop` as "the model judged this noise", never as evidence the event was harmless.
- **A model's parameters are limited on purpose.** `Default parameters` on an AI Model takes
  generation parameters only — `temperature`, `top_p`, `top_k`, `frequency_penalty`,
  `presence_penalty`, `logit_bias`, `n`, `reasoning_effort`, `seed`, `stop`, `extra_body`,
  `timeout`. Anything that would choose *who* answers, an endpoint or a key or a header above
  all, is refused: those come from the provider's external integration and from nowhere else.
  A parameter outside the list that reached the row another way — a fixture, a data migration —
  is dropped before the call and named in a log line rather than silently ignored. For an
  OpenAI-compatible provider, the app sends `reasoning_effort` as written, and the endpoint
  decides if it accepts the value.
- **Failure is safe.** A timeout, a provider error or an unusable answer accepts the event, so
  the result can be one ticket too many and never a lost event. An answer is unusable when it is
  empty, when it is not JSON, when its action is unknown, or when an `attach` does not name a
  ticket from the list that the app offered. Each such event adds 1 to the `triage_errors`
  counter. A failed call shows as failed on its usage record, and an empty answer is a failed call.
  Other unusable answers show there as a successful call.
  The consumer never exits because a model misbehaved.
- **Latency.** The consumer handles one message at a time, so a slow model bounds throughput.
  The pre-filter and the dedup short-circuit keep the call volume to genuinely new events.
- **Attribution.** Actions the model decided — a suppression, an attach — appear in the ticket
  trail with source *AI* and no user, with the model's reason in the message. Triage needs no
  Nautobot account: it runs inside the consumer process (see the note in
  [Install and Configure](install.md)).
- **Dry runs stay free.** `--dry-run` never consults the model; its output says
  `(triage skipped: dry run)`.

Watch it work on the ingestion stats page: `triaged`, `triage_attached` and `triage_errors`
counters, and triage drops under `llm_triage` in the drop breakdown.

## Watching the spend

The **LLM Usage** list shows each model call with its tokens, cost and result. The image shows the triage calls only.

![The LLM Usage list, with the triage calls and the tokens, cost and result of each call](../media/ss_llm_usage_light.png#only-light)
![The LLM Usage list, with the triage calls and the tokens, cost and result of each call](../media/ss_llm_usage_dark.png#only-dark)

Every call the app makes — including failed ones — writes a usage record with its token counts,
computed cost and latency. Read them at AI Tools → Event Tracker AI → LLM Usage, filter by model,
purpose or success, or query the same rows over REST and GraphQL. A ticket's detail page shows
the calls made about that ticket.

A reasoning model can use all of its output tokens before it writes an answer. The app writes one
warning to the log for an empty answer, and one for an answer that reached the limit of output
tokens (`finish_reason: length`). The warning gives the finish reason, the prompt and completion
token counts, and the reasoning token count when the provider reports it. An empty answer is also
a failed call, and the Error field of its usage record holds the same detail.

If you see these warnings, increase *num_predict* on the AI Model. For triage, you can also
increase `max_output_tokens` in the `triage` settings when no *num_predict* value of 1 or more
applies to the AI Model. Some providers also accept a parameter that decreases the reasoning of the
model. Set it in the **Default parameters** of the AI Model.

The usage list is read-only everywhere. Write permissions on it grant nothing through any shipped
route.
