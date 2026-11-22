# Configuring LLM Providers

The app calls language models through a registry that it does not own. The
[AI Models app](https://github.com/jtdub/nautobot-app-ai-models) owns **AI Provider** and
**AI Model**. This app depends on the AI Models app (ADR 0010).

- A provider is an endpoint that you allow.
- A model is one model that a provider offers.
- You manage both under **AI Tools > AI Models**.

This app records the other half. It prices each model call and writes the call to an
[LLM Usage Record](../models/llmusagerecord.md). Find the records under
**AI Tools > Event Tracker AI > LLM Usage**.

No setting is required. If you register no provider, the app makes no model call. When you install
this app, you also install the AI Models app. For the LLM features, it needs no setting of its own.

The app makes each model call through [litellm](https://docs.litellm.ai/). litellm is an optional
dependency. To get it, install the app with the `llm` extra:

```shell
pip install nautobot-event-tracker[llm]
```

If you do not install the extra, each model call fails. The error message names this command.

## Register a provider {#registering-a-provider}

A Nautobot **External Integration** holds the connection details of a provider. Credentials stay
where your deployment stores and rotates each other credential.

1. Create a **Secrets Group** that holds the API key. Skip this step for an endpoint that needs no
   key.

    - Add one secret. Set **Access type** to **Generic** and **Secret type** to **Token**.
    - A secret with **Secret type** **Secret** also works. If the group has both, the app uses
      **Token**.

2. Create an **External Integration**.

    - In **Remote URL**, enter the base URL of the endpoint.
    - For an OpenAI-compatible endpoint, enter the URL up to and including `/v1`.
    - For Ollama, do not add `/v1`.
    - In **Secrets group**, select the group from step 1.

3. Create an **AI Provider** under **AI Tools > AI Models > AI Providers**. In
   **External Integration**, select the integration. Then select the **Provider type**:

    - **OpenAI-compatible**: Use this type for a self-hosted or third-party endpoint that speaks
      the OpenAI protocol. Examples are vLLM, llama.cpp, and a gateway. The integration must have a
      remote URL. This is the main path for a deployment that cannot send event data to a third
      party.
    - **Ollama (native API)**: Use this type for Ollama through its own API. This type does not use
      the OpenAI compatibility layer of Ollama. In the integration, enter the base URL of Ollama
      with no `/v1`, for example `http://ollama.example.com:11434`. Ollama does not need a key. If
      you configure a key, the app sends it. Use this for an Ollama behind a proxy that
      authenticates.

        Use this type instead of **OpenAI-compatible** if you want the [agent](agents.md) to call
        tools. The compatibility layer of Ollama does not return tool calls in the `tool_calls`
        field. When you ask a model for a tool, it writes the JSON call into the message content.
        Nothing can act on the call there. Triage works with either type, because triage asks for
        no tools.
    - **OpenAI** and **Anthropic**: Use these types for the hosted services. Nautobot requires a
      remote URL on each external integration. Enter the base URL of the service:
      `https://api.openai.com/v1` or `https://api.anthropic.com`.

4. Create one or more **AI Models** on the provider. Or run the **Discover AI Models** Job of the
   AI Models app against the provider. The Job reads `GET /v1/models` from an OpenAI-compatible
   endpoint. It creates a row for each model that the endpoint offers.

    With either method, you must set two things on each model:

    - **Kind**: Chat or embedding. Discovery cannot know the kind, because `/v1/models` does not
      say. Each discovered row gets the kind **Chat**. The [retrieval](rag.md) settings refuse a
      model of the wrong kind.
    - **Input cost per million tokens** and **Output cost per million tokens**: These are your own
      prices. The usage records use them to show real prices. If you leave them empty, the app
      records the call with a cost of zero. The app does not guess a price.

The integration decides more than the address. The app reads four of its fields on each call. The
app does not apply two more fields to model calls.

| Field | What it does |
| --- | --- |
| **Remote URL** | The endpoint. The field accepts Jinja2 templates. In a template, `{{ obj }}` is the AI Provider |
| **Secrets group** | The API key. The app uses the **Token** secret type first. If the group has none, it uses **Secret** |
| **Headers** | The app sends them with each request. They accept the same templates. A template can also use `{{ session_id }}`. For an agent investigation, it is the ID of the first run. For a triage call, it is the consumer name. For each other call, it is a random ID of the process |
| **Verify SSL** | The app does not apply this field to LLM calls. See the warning below |
| **CA file path** | The app does not apply this field to LLM calls. See the warning below |
| **Timeout** | The timeout of the call. A timeout that the caller sets takes priority. For a chat call, a `timeout` in **Default parameters** of the model also takes priority |

The app does not send **Extra config**. The field has no fixed format. If the app sent it with a
call, it could change the endpoint or the key. The list of allowed model parameters blocks this
change.

!!! warning "The app does not apply the TLS settings of an integration to model calls"
    litellm has no TLS argument for one call. It reads only the environment variables `SSL_VERIFY`
    and `SSL_CERT_FILE`, or its own global value for the process. Therefore, the app cannot apply
    **Verify SSL** and **CA file path** for one provider. A worker runs calls for several providers
    at the same time. If the app applied the setting of one provider, it could turn off
    verification for the connection of another provider.

    To reach an LLM endpoint with a **private CA**, set `SSL_CERT_FILE` in the environment of each
    process that calls a model. These processes are Nautobot, the worker, and the event consumer.
    This adds a trust anchor. It does not weaken verification.

    Do not turn off verification for one provider. `SSL_VERIFY=False` turns it off for each
    provider in that process. This is a larger risk than the per-provider setting that the app does
    not apply. If the certificate of an endpoint cannot be verified, fix the certificate or add its
    CA.

    The app writes a warning to the log when **Verify SSL** is off or **CA file path** has a value.
    The warning names the fields that the app skipped.

    MCP calls are not affected. `services/mcp.py` builds its own HTTP client for each call and
    applies both fields.

If you turn off **Enabled** on a provider or a model, the app refuses each call through it. The app
refuses the call before any network traffic. This applies to each feature of the app at the same
time.

The app sends these values with each chat call: the **Default parameters** of the model, its
**Temperature**, and its **num_predict**. **Default parameters** accepts generation parameters only.
The endpoint and the credentials come from the integration. The model and the messages come from
the call. The AI Models app refuses any other key when you save the row.

If **Default parameters** has a `temperature` entry, it takes priority over the **Temperature**
column. A value on the model takes priority over the default of the provider.

## OpenCode Go

[OpenCode Go](https://opencode.ai/docs/en/go/) is a hosted service with an OpenAI-compatible API.
It needs two settings that other endpoints do not need. The values in this section worked in a
test on 2026-11-18 with the model `deepseek-v4.1-flash`. The test used no other Go model.

!!! warning "Read the Go usage policy before you turn on triage"
    Unattended triage can use all of the allowance of your subscription. OpenCode says that Go
    is for agents that write code. OpenCode also says that Go monitors traffic for abuse. Go has a
    5-hour limit and a weekly limit. Read the
    [usage limits of Go](https://opencode.ai/docs/en/go/#usage-limits) before you continue.

    The LLM Usage list does not show the Go allowance. It shows the cost from the prices on the
    AI Model row (see [Monitor the cost](#watching-the-spend)). Use the OpenCode console to see
    the allowance. If Extra Usage is on in your OpenCode account, Go does not stop at a limit.
    Go charges your account credit.

    When Go stops a call, triage accepts the event. See "Failure is safe" in
    [Turn on event triage](#enabling-event-triage).

    The Go facts in this warning come from the vendor page on 2026-11-19. They can change.

!!! warning
    The `session_id` template in the **Headers** field sends the consumer name of each triage call
    to Go. The default consumer name holds the host name and the process number. Set
    `ingestion.consumer_name` to a text that identifies nothing. Give each consumer a different
    name. See [Ingestion](ingestion.md).

Follow the steps in [Register a provider](#registering-a-provider) with these values:

| Object | Field | Value |
| --- | --- | --- |
| External Integration | **Remote URL** | `https://opencode.ai/inference/go/v1` |
| External Integration | **Headers** | The JSON under this table |
| External Integration | **Secrets group** | The group that holds your Go API key |
| AI Provider | **Provider type** | **OpenAI-compatible** |
| AI Model | **Name** | `deepseek-v4.1-flash` |
| AI Model | **Kind** | **Chat** |
| AI Model | **Default parameters** | `{"extra_body": {"reasoning_effort": "none"}}` |

Put this JSON in the **Headers** field:

```json
{"x-opencode-session": "{{ session_id | default('nautobot-discovery') }}"}
```

Create the AI Model row by hand. The model list of Go also has models that use a different API.

To check the URL, read the model list. This call needs no key:

```shell
curl https://opencode.ai/inference/go/v1/models
```

The answer is a JSON list of the models that Go offers.

**Go needs the session header.** Go refuses a call that has no `x-opencode-session` header. The
error is `MissingSessionID`. The template in the table gives the header a value on each call.

- A fixed value, such as `nautobot-event-tracker-prod`, also works. The app then sends the same
  value with each call.
- An agent call sends the ID of the first run of the investigation. A run that resumes after an
  approval sends the same ID.
- A triage call sends the consumer name. The same consumer sends the same value each time.
- Each other call sends one random ID for each process.
- The app replaces each character other than `A-Z a-z 0-9 . _ : -` with `-`. It cuts the value at
  128 characters.
- Go sees one session for each agent investigation, one for each consumer name, and one for each
  other process. This is the purpose of the template. The Go console then shows each session
  separately.

A renderer that does not know `session_id` sends the literal text `{{ session_id }}`. The registry
Job "Discover AI Models" and an MCP server integration are such renderers. The `default` filter,
with single quotes, gives them a value. Do not use double quotes in the filter. Double quotes break
the stored JSON, and the app then refuses each call.

After you change **Headers**, start one agent run or send one event. Open the LLM Usage list and
read the record of that call. If the record shows a session error, use the fixed value again.

If no new record appears, the app did not send the call. A template that does not render stops the
call before the app writes a record. Look for the text "has a template that does not render" in the
result of the agent run or in the log of the consumer. Then use the fixed value again.

**Turn off reasoning for triage.** `deepseek-v4.1-flash` reasons before it answers. The reasoning
tokens count against [`max_output_tokens`](#enabling-event-triage). In the test, the limit was 256
tokens, which was the default at that time. With this limit, 2 of 14 triage answers were empty. The
`extra_body` value in the table turns off the reasoning. A top-level `reasoning_effort` key gives
the same request. If the AI Model has both forms, the `extra_body` value applies. The
**Default parameters** apply to each call to this AI Model, agent calls included.

**Go has no embedding model.** [Retrieval](rag.md) needs an embedding model from a different
provider.

**Some models need the Global region.** OpenCode says that `deepseek-v4.1-flash` is available only
when the privacy settings of your OpenCode workspace permit the Global region. Triage sends the
event payload to the model. Before you permit the region, read "Cost and privacy" in
[Turn on event triage](#enabling-event-triage).

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
