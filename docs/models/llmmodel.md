# LLM Model

An LLM Model is one model available from an [LLM Provider](llmprovider.md), together with the
parameters and prices the app should use when calling it. The name is what goes on the wire —
`gpt-4o-mini`, `llama-3.1-70b` — and is unique within its provider.

The costs you enter here are what the app uses to price each call on its
[usage records](llmusagerecord.md). They are your numbers, not a price list the app maintains:
for a hosted provider enter their published rates, for a self-hosted model enter whatever your
own accounting says a token costs you — including zero.

## Fields

| Field | Description |
| --- | --- |
| Provider | The provider this model is called through. |
| Name | The model identifier sent on the wire. |
| Description | Free text. |
| Enabled | Turn this off and every call to this model is refused before any network traffic. |
| Input Cost per Million | USD per one million prompt tokens. |
| Output Cost per Million | USD per one million completion tokens. |
| Max Output Tokens | A default cap on completion length, applied when a caller does not set its own. |
| Default Parameters | Extra request parameters passed through on every call. Only seven keys are accepted — `frequency_penalty`, `presence_penalty`, `seed`, `stop`, `temperature`, `timeout` and `top_p` — and anything else is refused, naming the key. A specific call's own arguments win over these, so a `timeout` here applies whenever the caller does not set one. The list is an allowlist rather than a list of forbidden keys because litellm accepts many names for the endpoint that answers a call, and one of them getting through would send the provider's credential somewhere else: what decides *who* answers belongs to the provider's external integration, and the model and the messages belong to the call. |

## Elsewhere

The model detail page lists the recent usage records for that model. Selecting which model event
triage uses is part of [Configuring LLM Providers](../admin/llm.md).
