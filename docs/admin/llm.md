# Configuring LLM Providers

The app calls language models through a registry you manage in the UI: an
[LLM Provider](../models/llmprovider.md) is an endpoint you allow, an
[LLM Model](../models/llmmodel.md) is a model available from one, and every call the app makes is
priced and recorded as an [LLM Usage Record](../models/llmusagerecord.md). Nothing here is
required: an installation that registers no provider simply makes no model calls.

All calls go through [litellm](https://docs.litellm.ai/), which is an optional dependency. Install
the app with the `llm` extra to get it:

```shell
pip install nautobot-app-event-tracker[llm]
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
    - **OpenAI** / **Anthropic** — the hosted services. The remote URL may be left empty; litellm
      knows where they live.
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

## Watching the spend

Every call the app makes — including failed ones — writes a usage record with its token counts,
computed cost and latency. Read them at Apps → Event Tracker → LLM Usage, filter by model,
purpose or success, or query the same rows over REST and GraphQL. A ticket's detail page shows
the calls made about that ticket.

The usage list is read-only everywhere. Write permissions on it grant nothing through any shipped
route.
