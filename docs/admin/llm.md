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

The app needs no setting. If you register no provider, the app makes no model call. When you install
this app, you also install the AI Models app. The AI Models app needs no setting of its own for the LLM features.

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

    - Add one secret. Set **Access type** to **Generic**. Set **Secret type** to **Token**.
    - A secret with **Secret type** **Secret** also works. If the group has both, the app uses
      **Token**.

2. Create an **External Integration**.

    - In **Remote URL**, enter the base URL of the endpoint.
    - For an OpenAI-compatible endpoint, enter the URL up to and including `/v1`.
    - For Ollama, do not add `/v1`.
    - In **Secrets group**, select the group from step 1.

3. Create an **AI Provider** under **AI Tools > AI Models > AI Providers**. In
   **External Integration**, select the integration. Then select the **Provider type**:

    - **OpenAI-compatible**: Use this type for a self-hosted or third-party endpoint that uses
      the OpenAI protocol. Examples are vLLM, llama.cpp, and a gateway. The integration must have a
      remote URL. This is the main path for a deployment that cannot send event data to a third
      party.
    - **Ollama (native API)**: Use this type for Ollama through its own API. This type does not use
      the OpenAI compatibility layer of Ollama. In the integration, enter the base URL of Ollama
      with no `/v1`, for example `http://ollama.example.com:11434`. Ollama does not need a key. If
      you configure a key, the app sends it. Configure a key only for an Ollama behind a proxy that
      authenticates.

        Use this type instead of **OpenAI-compatible** if you want the [agent](agents.md) to call
        tools. The compatibility layer of Ollama does not return tool calls in the `tool_calls`
        field. When the agent asks a model for a tool, the model writes the JSON call into the message content.
        Nothing can act on the call there. Triage works with either type, because triage asks for
        no tools.
    - **OpenAI** and **Anthropic**: Use these types for the hosted services. Nautobot requires a
      remote URL on each external integration. Enter the base URL of the service:
      `https://api.openai.com/v1` or `https://api.anthropic.com`.

4. Create one or more **AI Models** on the provider. Or run the **Discover AI Models** Job of the
   AI Models app against the provider. The Job reads `GET /v1/models` from an OpenAI-compatible
   endpoint. It creates a row for each model that the endpoint offers.

    Discovery does not set these values. Set them yourself:

    - **Kind**: **Chat** or **Embedding**. Discovery cannot know the kind, because `/v1/models`
      does not say. Each discovered row gets the kind **Chat**. The [retrieval](rag.md) settings refuse a
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

The app does not send **Extra config**. The field has no fixed format. The app limits
**Default parameters** to a list of allowed keys, so that no parameter can change the endpoint or
the key. **Extra config** has no such limit.

!!! warning "The app does not apply the TLS settings of an integration to model calls"
    litellm has no TLS argument for one call. It reads only the environment variables `SSL_VERIFY`
    and `SSL_CERT_FILE`, or its own global value for the process. Therefore, the app cannot apply
    **Verify SSL** and **CA file path** for one provider. A worker runs calls for several providers
    at the same time. If the app applied the setting of one provider, it could turn off
    verification for the connection of another provider.

    Set `SSL_CERT_FILE` in the environment of each process that calls a model. These processes are
    Nautobot, the worker, and the event consumer. This lets the app reach an LLM endpoint with a
    **private CA**. It adds a trust anchor. It does not weaken verification.

    Do not turn off verification for one provider. `SSL_VERIFY=False` turns it off for each
    provider in that process. This is a larger risk than the per-provider setting that the app does
    not apply. If the app cannot verify the certificate of an endpoint, fix the certificate or add its
    CA.

    The app writes a warning to the log when **Verify SSL** is off or **CA file path** has a value.
    The warning names the fields that the app skipped.

    This does not affect MCP calls. `services/mcp.py` builds its own HTTP client for each call and
    applies both fields.

If you turn off **Enabled** on a provider or a model, the app refuses each call through it. The app
refuses the call before any network traffic. This applies to each feature of the app at the same
time.

The app sends these values with each chat call: the **Default parameters** of the model, its
**Temperature**, and its **Num_predict**. **Default parameters** accepts generation parameters only.
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

After you change **Headers**, start one agent run or send one event. Open the LLM Usage list.
Read the record of that call. If the record shows a session error, use the fixed value again.

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

The service layer has one setting. Set it under the `llm` key in the settings of this app:

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
| `usage_retention_days` | `90` | The service layer deletes each usage record that is older than this number of days. It does this at most one time each day for each process. |

Providers, models, and credentials are not settings. They are registry objects in the AI Models app
(see [Register a provider](#registering-a-provider)). The API key does not appear in
`PLUGINS_CONFIG` or in a log line.

## Turn on event triage {#enabling-event-triage}

After you register a provider and a model, the event consumer can use the model to triage events.
The consumer triages each event that the [pre-filter](ingestion.md) accepts. The model chooses one
of four actions: open a ticket, attach the event to an open ticket, suppress the event, or drop the
event. Set the triage settings in the `ingestion` block:

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

The consumer refuses to start in these cases:

- The named provider or model does not exist, or it is disabled.
- You installed the app without the `llm` extra.

The consumer reports these faults in one list with all other configuration faults. A `--dry-run`
skips the triage checks, because a dry run makes no model call.

`model_cache_seconds` sets how long a running consumer keeps its copy of the model row. The
consumer reads the row one time and keeps it for this period. It does not join the tables before
each event. A running consumer can take up to this period to use a change to **Enabled** or to a
price. To apply the change sooner, decrease the value. On a very busy consumer,
increase the value.

Triage sends a limit of output tokens with each call. The app finds the limit in two steps:

1. The app reads **Num_predict** of the AI Model. If this field is empty, the app reads
   **Default num_predict** of the AI Provider.
2. If the value is 1 or more, the app uses it as the limit. If the value is empty, 0, or -1, the
   app uses `max_output_tokens` from the `triage` settings.

A value on the AI Model takes priority over **Default num_predict** of the AI Provider. For
example, if the model has -1, the limit is `max_output_tokens`, also when the provider has a
default. A value of -1 does not remove the limit for triage.

A reasoning model uses tokens from this limit before it writes the answer. A small limit can give
an empty answer. The default of 1024 leaves room for the reasoning. A model that does not reason
writes one small JSON object, and the limit does not change its cost. To give one reasoning model
more room, set **Num_predict** on its AI Model. A running consumer uses the new value within
`model_cache_seconds`.

The agent uses the same **Num_predict** value. If triage and the agent use the same AI Model, one
value limits both. To use different limits, register the model on a second AI Provider. The second
AI Provider can use the same External Integration. The two rows then have their own prices,
**Enabled** checkboxes, and **Default parameters**. The LLM Usage list shows them as two models.

`timeout_seconds` is the second limit. A slow model can reach the timeout before it reaches the
token limit. If you increase the token limit for a reasoning model, also check `timeout_seconds`.
When a call reaches one of the two limits, triage accepts the event.

A triage call makes one attempt. The app does not send the call again.

- Before, the SDK sent a call again after a timeout, a connection error, or an HTTP 408, 409, 429, or 5xx answer. On an
  OpenAI or OpenAI-compatible provider, one call took about three times `timeout_seconds`.
- Now such a fault fails the call at once. Triage accepts the event with no answer from the model, and `triage_errors`
  counts it. While the fault lasts, the pre-filter (the dedup window and `rate_limit`) is the only filter. Tickets
  can open for noise.

Read these points before you turn on triage:

- **Cost and privacy.** Each event that the pre-filter accepts makes one model call. The prompt of
  the call holds the payload of the event, up to `max_context_chars`. If the payloads of a topic
  must not leave your network, set `triage: False` on that topic. You can also point the provider
  at a self-hosted endpoint. A repeated event costs nothing: an event with a dedup key that matches
  an open ticket joins that ticket with no model call.

- **Triage is a filter, not a control.** The prompt holds text that the source of the events
  controls. An event can try to argue for its own result. The app limits the shape of the answer
  to four actions and to tickets that the app itself listed. The app does not limit the reasoning
  of the model. A triage `drop` means only that the model judged the event as noise, not that the
  event was harmless.

- **The app limits the parameters of a model on purpose.** **Default parameters** on an AI Model
  accepts generation parameters only: `temperature`, `top_p`, `top_k`, `frequency_penalty`,
  `presence_penalty`, `logit_bias`, `n`, `reasoning_effort`, `seed`, `stop`, `extra_body`, and
  `timeout`. The AI Models app refuses a parameter that chooses who answers, such as an endpoint,
  a key, or a header. Those values come from the External Integration of the provider and from no
  other place. If a fixture or a data migration puts a parameter outside the list in the row, the
  app removes the parameter before the call. The app names the removed parameter in a log line.

    For an OpenAI-compatible provider, the app sends `reasoning_effort` as written. The endpoint
    decides if it accepts the value.

- **Failure is safe.** If a call times out, the provider returns an error, or the answer is
  unusable, triage accepts the event. The result can be one ticket too many, but the app does not
  lose an event. An answer is unusable in these cases:

    - The answer is empty.
    - The answer is not JSON.
    - The action is unknown.
    - An `attach` does not name a ticket from the list that the app offered.

    Each such event adds 1 to the `triage_errors` counter. A failed call shows as failed on its
    usage record, and an empty answer is a failed call. Other unusable answers show there as a
    successful call. The consumer does not exit because of a model fault.

- **Latency.** The consumer handles one message at a time, so a slow model limits the throughput.
  The pre-filter and the dedup check keep the number of calls to events that are new.

- **Attribution.** The update trail of a ticket shows each action of the model, for example a
  suppression or an attach. The entry has the source **AI** and no user. The message of the entry
  holds the reason of the model. Triage needs no Nautobot account, because it runs inside the
  consumer process. See the note **The agent needs no account** in
  [Install and Configure](install.md).

- **A dry run is free.** `--dry-run` does not use the model. Its output says
  `(triage skipped: dry run)`.

To see the results of triage, open **Apps > Event Tracker > Ingestion Stats**. The list shows the
counters `triaged`, `triage_attached`, and `triage_errors`. To see the triage drops, click the
**Window** of a row. The **Drops by reason** panel of the row shows them under `llm_triage`. The
panel shows only when the row has drops.

## Monitor the cost {#watching-the-spend}

The **LLM Usage** list shows each model call with its tokens, cost and result. The image shows the triage calls only.

![The LLM Usage list, with the triage calls and the tokens, cost and result of each call](../media/ss_llm_usage_light.png#only-light)
![The LLM Usage list, with the triage calls and the tokens, cost and result of each call](../media/ss_llm_usage_dark.png#only-dark)

The app writes a usage record for each model call that it sends. This includes a failed call. The
record holds the token counts, the computed cost, and the latency of the call.

Open **AI Tools > Event Tracker AI > LLM Usage** to read the records. Filter the list by model,
purpose, or success. You can also query the same records through REST and GraphQL. The ticket page
has an **LLM Usage** panel with the calls about that ticket.

A reasoning model can use all of its output tokens before it writes an answer. The app writes one
warning to the log for an empty answer. The app also writes one warning for an answer that reached
the limit of output tokens (`finish_reason: length`). Each warning gives the finish reason and the
prompt and completion token counts. It also gives the reasoning token count when the provider
reports it.

An empty answer is also a failed call. The **Error** field of its usage record holds the same
detail.

If you see these warnings, increase **Num_predict** on the AI Model. For triage, you can also
increase `max_output_tokens` in the `triage` settings. This works only when no **Num_predict**
value of 1 or more applies to the AI Model. Some providers also accept a parameter that decreases
the reasoning of the model. Set this parameter in **Default parameters** of the AI Model.

The usage list is read-only in the UI and in the API. A write permission on usage records gives no
access to change them.
