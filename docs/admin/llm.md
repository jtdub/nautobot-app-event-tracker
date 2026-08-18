# Configuring LLM Providers

The app calls language models through a registry you manage in the UI: an
[LLM Provider](../models/llmprovider.md) is an endpoint you allow, an
[LLM Model](../models/llmmodel.md) is a model available from one, and every call the app makes is
priced and recorded as an [LLM Usage Record](../models/llmusagerecord.md). Nothing here is
required: an installation that registers no provider simply makes no model calls.

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
   OpenAI-compatible endpoint, the URL up to and including `/v1`), and the *Secrets Group* is the
   one from step 1.
3. Create an **LLM Provider** (Apps → Event Tracker → LLM Providers) pointing at that
   integration, choosing the provider type:
    - **OpenAI-compatible** — any self-hosted or third-party endpoint speaking the OpenAI
      protocol: vLLM, Ollama, llama.cpp, a gateway. The integration must carry a remote URL.
      This is the first-class path for deployments that cannot send event data to a third party.
    - **OpenAI** / **Anthropic** — the hosted services. Nautobot requires a remote URL on every
      external integration, so give it the service's own base URL
      (`https://api.openai.com/v1`, `https://api.anthropic.com`).
4. Create one or more **LLM Models** on the provider. The model name is what goes on the wire.
   Enter the input and output costs so usage records carry real prices — they are your numbers,
   and zero is a fine answer for a model you run yourself.

Disabling a provider or a model (the `enabled` flag on either) refuses every call through it
before any network traffic, everywhere at once.

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
objects above, and the API key never appears in `PLUGINS_CONFIG` or in a log line.

## Enabling event triage

With a provider and model registered, the event consumer can ask the model to triage every event
the [pre-filter](ingestion.md) accepts: open a ticket, attach the event to an open ticket,
suppress it, or drop it. Configure it inside the `ingestion` block:

```python
"ingestion": {
    ...,
    "triage": {
        "enabled": True,
        "provider": "Local Lab",        # LLMProvider name
        "model": "llama-3.1-70b",       # LLMModel name on that provider
        "timeout_seconds": 15,
        "max_output_tokens": 256,
        "max_context_chars": 4000,      # payload cap in the prompt
        "attach_candidates": 5,         # open tickets the model may attach to
    },
    "topics": {
        "network.events": {..., "triage": True},   # per-topic; default True
    },
},
```

The consumer refuses to start when the named provider or model does not exist or is disabled, or
when the app was installed without the `llm` extra, alongside every other configuration fault.

What to know before you turn it on:

- **Cost and privacy.** Each surviving event makes one model call, whose prompt carries the
  event's payload (capped at `max_context_chars`). Set `triage: False` on a topic whose payloads
  must not leave the box, or point the provider at a self-hosted endpoint. Recurrences are free:
  an event whose dedup key matches an open ticket joins it without a model call.
- **Failure is safe.** A timeout, a provider error or an unusable answer accepts the event — a
  ticket too many, never an event lost — and shows up in the `triage_errors` counter and on the
  failed call's usage record. The consumer never exits because a model misbehaved.
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
computed cost and latency. Read them at Apps → Event Tracker → LLM Usage, filter by model,
purpose or success, or query the same rows over REST and GraphQL. A ticket's detail page shows
the calls made about that ticket.

The usage list is read-only everywhere. Write permissions on it grant nothing through any shipped
route.
