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
| *Headers* | Sent with every request, templated the same way |
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

Follow the steps in [Registering a provider](#registering-a-provider) with these values:

| Object | Field | Value |
| --- | --- | --- |
| External Integration | *Remote URL* | `https://opencode.ai/inference/go/v1` |
| External Integration | *Headers* | `{"x-opencode-session": "nautobot-event-tracker-prod"}` |
| External Integration | *Secrets Group* | The group that holds your Go API key |
| AI Provider | *Provider type* | **OpenAI-compatible** |
| AI Model | *Name* | `deepseek-v4.1-flash` |
| AI Model | *Kind* | *Chat* |
| AI Model | *Default parameters* | `{"extra_body": {"reasoning_effort": "none"}}` |

Make the AI Model row by hand. The model list of Go also has models that use a different API.

To check the URL, read the model list. This call needs no key:

```shell
curl https://opencode.ai/inference/go/v1/models
```

The answer is a JSON list of the models that Go offers.

**Go needs the session header.** Go refuses a call that does not have the
`x-opencode-session` header. The error is `MissingSessionID`. Use a text that names this
installation. Do not change that text. The app sends the same value with every triage call and
every agent call.

**Stop reasoning for triage.** `deepseek-v4.1-flash` reasons before it answers. The reasoning
tokens count against [`max_output_tokens`](#enabling-event-triage). With the default settings,
2 of 14 triage answers were empty in the test. The `extra_body` value in the table stops the
reasoning. Do not set `reasoning_effort` as a top-level key, because the call fails for the
OpenAI-compatible provider type. The Default parameters apply to each call to this AI Model,
agent calls included.

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
        "max_output_tokens": 256,
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
  is dropped before the call and named in a log line rather than silently ignored.
- **Failure is safe.** A timeout, a provider error or an unusable answer accepts the event, so
  the result can be one ticket too many and never a lost event. An answer is unusable when it is
  empty, when it is not JSON, when its action is unknown, or when an `attach` does not name a
  ticket from the list that the app offered. Each such event adds 1 to the `triage_errors`
  counter. A failed call also shows as failed on its usage record. An unusable answer shows there
  as a successful call. The consumer never exits because a model misbehaved.
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

Every call the app makes — including failed ones — writes a usage record with its token counts,
computed cost and latency. Read them at AI Tools → Event Tracker AI → LLM Usage, filter by model,
purpose or success, or query the same rows over REST and GraphQL. A ticket's detail page shows
the calls made about that ticket.

The usage list is read-only everywhere. Write permissions on it grant nothing through any shipped
route.
