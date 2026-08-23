# LLM Provider

An LLM Provider is an endpoint the app is allowed to send model calls to. You register one through
the UI; nothing in the app calls a model that is not behind a provider on this list.

A provider carries no credentials of its own. It points at an
[External Integration](https://docs.nautobot.com/projects/core/en/stable/user-guide/platform-functionality/externalintegration/),
which holds the endpoint URL and — through its Secrets Group — the API key. Rotate the key where
you rotate every other credential in your deployment; the app reads it at call time and never
stores it.

## Fields

| Field | Description |
| --- | --- |
| Name | A name of your choosing, unique. Configuration refers to the provider by this name. |
| Description | Free text. |
| Provider Type | The protocol the endpoint speaks: OpenAI, Anthropic, OpenAI-compatible for a self-hosted endpoint, or Ollama. The two self-hosted types require their integration to carry a remote URL, because litellm's fallback for each goes somewhere wrong — `api.openai.com` for one, a loopback address for the other. Ollama has a type of its own because its OpenAI-compatibility layer cannot return tool calls; see [Configuring LLM Providers](../admin/llm.md). |
| External Integration | Where the endpoint URL and credentials live. |
| Enabled | Turn this off and every call through this provider is refused before any network traffic — the off switch works everywhere at once. |

## Elsewhere

The models available from a provider are registered as [LLM Models](llmmodel.md). Every call made
through a provider is recorded as an [LLM Usage Record](llmusagerecord.md). Setup is walked
through in [Configuring LLM Providers](../admin/llm.md).
