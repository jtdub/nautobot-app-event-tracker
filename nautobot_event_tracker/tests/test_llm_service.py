"""Tests for the LLM service layer, against rules L1-L8 from the Phase 3 spec.

Every test injects a fake client at the `client` seam of `complete()` rather than mocking litellm
internals, so everything up to the wire is exercised for real and no test opens a network
connection.
"""

import os
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.test import override_settings
from django.utils import timezone
from nautobot.apps.choices import SecretsGroupAccessTypeChoices, SecretsGroupSecretTypeChoices
from nautobot.apps.testing import TestCase
from nautobot.extras.models import Secret, SecretsGroup, SecretsGroupAssociation

from nautobot_event_tracker.choices import LLMProviderTypeChoices, LLMPurposeChoices
from nautobot_event_tracker.models import LLMModel, LLMUsageRecord
from nautobot_event_tracker.services import llm as llm_service
from nautobot_event_tracker.services.exceptions import LLMCallError, LLMConfigurationError, LLMResponseError
from nautobot_event_tracker.tests import fixtures
from nautobot_event_tracker.tests.fixtures import FakeLLMClient, FakeLLMResponse

MESSAGES = [{"role": "user", "content": "hello"}]


def call(model, client, **kwargs):
    """One service call with the boilerplate filled in."""
    kwargs.setdefault("messages", MESSAGES)
    kwargs.setdefault("purpose", LLMPurposeChoices.TRIAGE)
    return llm_service.complete(model=model, client=client, **kwargs)


class TestRecording(TestCase):
    """L1 - every call writes exactly one record, success or failure."""

    def setUp(self):
        """One model to call."""
        super().setUp()
        self.model = fixtures.create_llmmodel()

    def test_a_successful_call_writes_one_record(self):
        """The record carries what the call was and what it cost."""
        response = call(self.model, FakeLLMClient(FakeLLMResponse("answer", prompt_tokens=100, completion_tokens=50)))

        self.assertEqual(LLMUsageRecord.objects.count(), 1)
        record = LLMUsageRecord.objects.get()
        self.assertEqual(response.record, record)
        self.assertEqual(record.model, self.model)
        self.assertEqual(record.purpose, LLMPurposeChoices.TRIAGE)
        self.assertEqual(record.prompt_tokens, 100)
        self.assertEqual(record.completion_tokens, 50)
        self.assertTrue(record.success)
        self.assertEqual(record.error, "")
        self.assertEqual(record.request_id, "req-1")
        self.assertEqual(response.text, "answer")

    def test_a_failed_call_writes_one_record_and_raises_the_family(self):
        """L4 - the caller sees LLMCallError carrying the record, never the client's own type."""
        with self.assertRaises(LLMCallError) as raised:
            call(self.model, FakeLLMClient(error=RuntimeError("connection refused")))

        record = LLMUsageRecord.objects.get()
        self.assertFalse(record.success)
        self.assertIn("connection refused", record.error)
        self.assertEqual(raised.exception.record, record)

    def test_error_text_is_capped(self):
        """A stack-trace-sized error is truncated, not archived."""
        with self.assertRaises(LLMCallError):
            call(self.model, FakeLLMClient(error=RuntimeError("x" * 5000)))

        record = LLMUsageRecord.objects.get()
        self.assertEqual(len(record.error), llm_service.ERROR_TEXT_CAP)

    def test_a_contentless_response_is_recorded_and_refused(self):
        """The call happened and is on the record, but the caller gets LLMResponseError."""
        with self.assertRaises(LLMResponseError) as raised:
            call(self.model, FakeLLMClient(FakeLLMResponse(content=None)))

        record = LLMUsageRecord.objects.get()
        self.assertFalse(record.success)
        self.assertEqual(raised.exception.record, record)

    def test_a_ticket_is_linked_when_given(self):
        """A call made about a ticket says so."""
        ticket = fixtures.create_ticket()
        response = call(self.model, FakeLLMClient(), ticket=ticket)
        self.assertEqual(response.record.ticket, ticket)

    def test_link_usage_records_points_records_at_a_later_ticket(self):
        """A triage call precedes its ticket; the link is applied after the fact."""
        response = call(self.model, FakeLLMClient())
        ticket = fixtures.create_ticket()

        llm_service.link_usage_records([response.record.pk], ticket)

        response.record.refresh_from_db()
        self.assertEqual(response.record.ticket, ticket)


class TestCost(TestCase):
    """L5 - cost comes from the registry's prices and the provider's reported usage."""

    def test_cost_arithmetic(self):
        """1M input at $1 plus 0.5M output at $2 is $2."""
        model = fixtures.create_llmmodel()
        response = call(model, FakeLLMClient(FakeLLMResponse(prompt_tokens=1_000_000, completion_tokens=500_000)))
        self.assertEqual(response.cost, Decimal("2"))
        self.assertEqual(response.record.cost, Decimal("2"))

    def test_no_usage_block_records_zeros(self):
        """Zeros, never a guess."""
        model = fixtures.create_llmmodel()
        response = call(model, FakeLLMClient(FakeLLMResponse(usage=False)))
        self.assertEqual(response.prompt_tokens, 0)
        self.assertEqual(response.completion_tokens, 0)
        self.assertEqual(response.cost, Decimal("0"))


class TestCredentials(TestCase):
    """L3 - the endpoint and key resolve from Nautobot at call time."""

    ENV_VAR = "EVENT_TRACKER_TEST_LLM_KEY"

    def _secrets_group(self, secret_type):
        """A secrets group carrying one environment-variable secret under this type."""
        secret, _ = Secret.objects.get_or_create(
            name=f"llm test key {secret_type}",
            defaults={"provider": "environment-variable", "parameters": {"variable": self.ENV_VAR}},
        )
        group, _ = SecretsGroup.objects.get_or_create(name=f"llm test group {secret_type}")
        SecretsGroupAssociation.objects.get_or_create(
            secrets_group=group,
            secret=secret,
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=secret_type,
        )
        return group

    def _set_key(self, value):
        """Put the key in the environment for the duration of the test."""
        os.environ[self.ENV_VAR] = value
        self.addCleanup(os.environ.pop, self.ENV_VAR, None)

    def test_endpoint_and_token_reach_the_client(self):
        """The integration's URL becomes api_base and its token secret becomes api_key."""
        self._set_key("sk-test-token")
        integration = fixtures.create_external_integration()
        integration.secrets_group = self._secrets_group(SecretsGroupSecretTypeChoices.TYPE_TOKEN)
        integration.save()
        model = fixtures.create_llmmodel()

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["api_base"], "http://llm.example.test/v1")
        self.assertEqual(client.calls[0]["api_key"], "sk-test-token")
        self.assertEqual(client.calls[0]["model_string"], "openai/test-model")

    def test_the_secret_type_falls_back_to_secret(self):
        """An operator who modeled the key as a plain secret is not wrong."""
        self._set_key("sk-plain-secret")
        integration = fixtures.create_external_integration(name="Fallback Endpoint")
        integration.secrets_group = self._secrets_group(SecretsGroupSecretTypeChoices.TYPE_SECRET)
        integration.save()
        provider = fixtures.create_llmprovider(name="Fallback Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["api_key"], "sk-plain-secret")

    def test_a_keyless_openai_compatible_endpoint_gets_a_placeholder(self):
        """ADR 0006 makes an unauthenticated on-premises endpoint first-class; this is the cost.

        litellm builds an OpenAI client for the `openai/` prefix, and that client raises "Missing
        credentials" locally - before any request leaves the process - when it has no key at all.
        So "this endpoint needs no key" has to be spelled as a value rather than as an absence, or
        the deployment ADR 0006 exists to support cannot make a single call.
        """
        model = fixtures.create_llmmodel()

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["api_key"], llm_service.NO_CREDENTIAL_PLACEHOLDER)

    def test_ollama_routes_to_its_own_litellm_prefix(self):
        """Not `openai/`, and the difference is the whole reason this provider type exists.

        Ollama's OpenAI-compatibility layer does not return tool calls in the `tool_calls` field -
        a model asked for a tool answers with the JSON written into the message content, where
        nothing may act on it. Its native API does, and litellm reaches that through `ollama/`.
        On the compatible path an Ollama-backed agent cannot call a tool at all.
        """
        provider = fixtures.create_llmprovider(name="Local Ollama", provider_type=LLMProviderTypeChoices.OLLAMA)
        model = fixtures.create_llmmodel(provider=provider, name="qwen2.5-coder:7b")

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["model_string"], "ollama/qwen2.5-coder:7b")

    def test_ollama_needs_no_placeholder_key(self):
        """Its litellm path builds no OpenAI client, so there is no constructor to get past."""
        provider = fixtures.create_llmprovider(name="Keyless Ollama", provider_type=LLMProviderTypeChoices.OLLAMA)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertNotIn("api_key", client.calls[0])

    def test_a_self_hosted_provider_with_no_url_is_refused(self):
        """Both self-hosted types, because litellm's fallback for each goes somewhere wrong.

        `openai/` with no api_base sends this deployment's key and its event payload to
        api.openai.com; `ollama/` quietly tries a loopback address that means nothing inside a
        container. `clean()` demands the URL at save time, and an integration is a shared object
        that can be blanked afterwards without revalidating what points at it.
        """
        for provider_type in (LLMProviderTypeChoices.OPENAI_COMPATIBLE, LLMProviderTypeChoices.OLLAMA):
            with self.subTest(provider_type=provider_type):
                integration = fixtures.create_external_integration(name=f"Blank {provider_type}", remote_url="")
                provider = fixtures.create_llmprovider(
                    name=f"Blanked {provider_type}",
                    provider_type=provider_type,
                    external_integration=integration,
                )
                model = fixtures.create_llmmodel(provider=provider, name=f"model-{provider_type}")

                with self.assertRaises(LLMConfigurationError) as caught:
                    call(model, FakeLLMClient())

                self.assertIn("no remote URL", str(caught.exception))

    def test_a_keyless_first_party_provider_sends_no_key(self):
        """Only OpenAI-compatible gets the placeholder.

        A real OpenAI or Anthropic endpoint with no key configured is a misconfiguration, and it
        should fail with the provider's own message on the record - not with a placeholder this app
        invented, which would read as a rejected credential rather than as a missing one.
        """
        provider = fixtures.create_llmprovider(name="First Party", provider_type=LLMProviderTypeChoices.ANTHROPIC)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertNotIn("api_key", client.calls[0])

    def test_a_configured_secret_wins_over_the_placeholder(self):
        """The placeholder is what "nobody configured one" looks like, never an override."""
        self._set_key("sk-real-key")
        integration = fixtures.create_external_integration(name="Authenticated Endpoint")
        integration.secrets_group = self._secrets_group(SecretsGroupSecretTypeChoices.TYPE_TOKEN)
        integration.save()
        provider = fixtures.create_llmprovider(name="Authenticated Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["api_key"], "sk-real-key")

    def test_a_templated_remote_url_is_rendered(self):
        """Nautobot supports Jinja2 on remote_url; read raw it reaches litellm as a literal brace."""
        integration = fixtures.create_external_integration(
            name="Templated Endpoint", remote_url="http://{{ obj.name }}.example.test/v1"
        )
        provider = fixtures.create_llmprovider(name="Templated Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["api_base"], "http://Templated Provider.example.test/v1")

    def test_a_template_that_does_not_render_is_a_configuration_error(self):
        """Half a URL points the call somewhere nobody chose, so it is refused before the wire."""
        integration = fixtures.create_external_integration(
            name="Broken Template", remote_url="http://{{ oops.example.test/v1"
        )
        provider = fixtures.create_llmprovider(name="Broken Template Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        with self.assertRaises(LLMConfigurationError):
            call(model, client)
        self.assertEqual(client.calls, [], "the call left the process anyway")

    def test_the_integrations_headers_reach_the_client(self):
        """An operator who set a header on the integration meant it to be sent."""
        integration = fixtures.create_external_integration(name="Header Endpoint", headers={"X-Tenant": "network-ops"})
        provider = fixtures.create_llmprovider(name="Header Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["extra_headers"], {"X-Tenant": "network-ops"})

    def test_tls_settings_are_not_passed_as_call_keywords(self):
        """`ssl_verify` is not a litellm argument, and passing it was worse than useless.

        litellm sweeps an unknown keyword into `extra_body` and sends it to the provider in the
        request JSON. So the old code disabled no verification, loaded no CA bundle, and added a
        stray field to every request - while a test that asserted the keyword was passed went on
        agreeing that it worked. This asserts the outcome instead.
        """
        for name, overrides in (
            ("Private CA Endpoint", {"ca_file_path": "/etc/ssl/private-ca.pem"}),
            ("No Verify Endpoint", {"verify_ssl": False}),
        ):
            with self.subTest(name=name):
                integration = fixtures.create_external_integration(name=name, **overrides)
                provider = fixtures.create_llmprovider(name=f"{name} Provider", external_integration=integration)
                model = fixtures.create_llmmodel(provider=provider, name=f"model-{name}")

                client = FakeLLMClient()
                with self.assertLogs("nautobot_event_tracker.services.llm", level="WARNING"):
                    call(model, client)

                self.assertNotIn("ssl_verify", client.calls[0])
                self.assertNotIn("extra_body", client.calls[0])

    def test_the_warning_names_what_was_not_applied_and_what_to_do(self):
        """A setting the app cannot honour has to say so, or the UI is lying about it."""
        integration = fixtures.create_external_integration(
            name="Both TLS Fields", verify_ssl=False, ca_file_path="/etc/ssl/private-ca.pem"
        )
        provider = fixtures.create_llmprovider(name="Both TLS Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)

        with self.assertLogs("nautobot_event_tracker.services.llm", level="WARNING") as logs:
            call(model, FakeLLMClient())

        message = " ".join(logs.output)
        self.assertIn("Verify SSL", message)
        self.assertIn("/etc/ssl/private-ca.pem", message)
        self.assertIn("NOT being applied", message)
        # Names the additive remedy, and does not recommend the destructive one: turning
        # verification off in the environment turns it off for every provider in the process,
        # which is wider than the per-provider setting the app declines to apply.
        self.assertIn("SSL_CERT_FILE", message)
        self.assertNotIn("SSL_VERIFY=False", message)

    def test_an_integration_with_default_tls_says_nothing(self):
        """The warning fires for a setting that was made, never for one left alone."""
        model = fixtures.create_llmmodel()

        with self.assertNoLogs("nautobot_event_tracker.services.llm", level="WARNING"):
            call(model, FakeLLMClient())

    def test_extra_config_is_not_splatted_into_the_call(self):
        """Untyped operator JSON in the call kwargs would reopen the hole the allowlist closed."""
        integration = fixtures.create_external_integration(
            name="Extra Config Endpoint", extra_config={"base_url": "http://elsewhere.example.test"}
        )
        provider = fixtures.create_llmprovider(name="Extra Config Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertNotIn("base_url", client.calls[0])
        self.assertEqual(client.calls[0]["api_base"], "http://llm.example.test/v1")

    def test_provider_types_map_to_model_strings(self):
        """litellm routes on the prefix; the registry's type decides it."""
        integration = fixtures.create_external_integration(name="Anthropic Endpoint", remote_url="")
        provider = fixtures.create_llmprovider(
            name="Anthropic Test",
            provider_type=LLMProviderTypeChoices.ANTHROPIC,
            external_integration=integration,
        )
        model = fixtures.create_llmmodel(name="claude-test", provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["model_string"], "anthropic/claude-test")
        self.assertNotIn("api_base", client.calls[0])


class TestCallParameters(TestCase):
    """L6 and the parameter plumbing."""

    def test_a_timeout_is_always_passed(self):
        """The default applies when the caller sets none."""
        model = fixtures.create_llmmodel()
        client = FakeLLMClient()
        call(model, client)
        self.assertEqual(client.calls[0]["timeout"], llm_service.DEFAULT_TIMEOUT_SECONDS)

    def test_the_callers_timeout_wins(self):
        """A caller with a tighter budget gets it."""
        model = fixtures.create_llmmodel()
        client = FakeLLMClient()
        call(model, client, timeout=5)
        self.assertEqual(client.calls[0]["timeout"], 5)

    def test_max_tokens_falls_back_to_the_models_cap(self):
        """The registry's cap applies when the caller sets none, and the caller's wins when set."""
        model = fixtures.create_llmmodel(name="capped-model", max_output_tokens=128)
        client = FakeLLMClient()
        call(model, client)
        self.assertEqual(client.calls[0]["max_tokens"], 128)

        client = FakeLLMClient()
        call(model, client, max_tokens=64)
        self.assertEqual(client.calls[0]["max_tokens"], 64)

    def test_default_parameters_pass_through_underneath(self):
        """The registry's parameters reach the call; the caller's own arguments beat them."""
        model = fixtures.create_llmmodel(name="tuned-model", default_parameters={"temperature": 0.1, "timeout": 999})
        client = FakeLLMClient()
        call(model, client)
        self.assertEqual(client.calls[0]["temperature"], 0.1)

        client = FakeLLMClient()
        call(model, client, timeout=5)
        self.assertEqual(client.calls[0]["timeout"], 5)

    def test_the_registrys_timeout_applies_when_the_caller_states_none(self):
        """Otherwise a slow self-hosted model has no configuration escape hatch at all."""
        model = fixtures.create_llmmodel(name="slow-model", default_parameters={"timeout": 120})
        client = FakeLLMClient()
        call(model, client)
        self.assertEqual(client.calls[0]["timeout"], 120)

    def test_response_format_passes_through(self):
        """Structured-output requests reach the client untouched."""
        model = fixtures.create_llmmodel()
        client = FakeLLMClient()
        call(model, client, response_format={"type": "json_object"})
        self.assertEqual(client.calls[0]["response_format"], {"type": "json_object"})

    def test_a_parameter_outside_the_allowlist_never_reaches_the_client(self):
        """The second layer, and the one that matters: `clean()` is not on this path.

        The row is written with `update()`, which is how a fixture, a data migration or an
        operator at `nbshell` would write it - none of them runs model validation. If the filter
        lived only in `clean()`, this call would carry `base_url` and the provider's key to
        whatever address the row named.
        """
        model = fixtures.create_llmmodel(name="smuggled-model")
        LLMModel.objects.filter(pk=model.pk).update(
            default_parameters={"base_url": "https://somewhere.example/v1", "temperature": 0.2}
        )
        model.refresh_from_db()

        client = FakeLLMClient()
        call(model, client)

        self.assertNotIn("base_url", client.calls[0])
        # The legitimate parameter beside it still arrives, so this filters rather than discards.
        self.assertEqual(client.calls[0]["temperature"], 0.2)

    def test_a_dropped_parameter_is_logged(self):
        """Silently dropping it is how an operator's parameter stops applying with no signal."""
        model = fixtures.create_llmmodel(name="noisy-model")
        LLMModel.objects.filter(pk=model.pk).update(default_parameters={"base_url": "https://elsewhere.example"})
        model.refresh_from_db()

        with self.assertLogs("nautobot_event_tracker.services.llm", level="WARNING") as logged:
            call(model, FakeLLMClient())

        self.assertIn("base_url", "\n".join(logged.output))

    def test_the_integration_timeout_applies_when_nothing_else_states_one(self):
        """A slow on-premises endpoint is exactly what the integration's Timeout field is for."""
        integration = fixtures.create_external_integration(name="Slow Endpoint", timeout=120)
        provider = fixtures.create_llmprovider(name="Slow Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["timeout"], 120)

    def test_the_registrys_timeout_beats_the_integrations(self):
        """Most specific wins: the call, then the model row, then the integration, then the default."""
        integration = fixtures.create_external_integration(name="Slow Endpoint 2", timeout=120)
        provider = fixtures.create_llmprovider(name="Slow Provider 2", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider, default_parameters={"timeout": 7})

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["timeout"], 7)

    def test_a_null_timeout_in_the_registry_is_not_an_answer(self):
        """L6 says every call carries a timeout, and `setdefault` used to read null as one.

        Written with `update()`, the way `clean()`'s new positive-number check cannot be reached.
        """
        model = fixtures.create_llmmodel(name="null-timeout-model")
        LLMModel.objects.filter(pk=model.pk).update(default_parameters={"timeout": None})
        model.refresh_from_db()

        client = FakeLLMClient()
        call(model, client)

        self.assertEqual(client.calls[0]["timeout"], llm_service.DEFAULT_TIMEOUT_SECONDS)


class TestRefusals(TestCase):
    """L8 - disabled means disabled, before any network traffic and with no record."""

    def test_a_disabled_provider_refuses(self):
        """The provider's switch covers every model on it."""
        provider = fixtures.create_llmprovider(name="Disabled Provider", enabled=False)
        model = fixtures.create_llmmodel(provider=provider)
        client = FakeLLMClient()

        with self.assertRaises(LLMConfigurationError):
            call(model, client)

        self.assertEqual(client.calls, [])
        self.assertEqual(LLMUsageRecord.objects.count(), 0)

    def test_a_disabled_model_refuses(self):
        """The model's own switch works too."""
        model = fixtures.create_llmmodel(name="disabled-model", enabled=False)
        client = FakeLLMClient()

        with self.assertRaises(LLMConfigurationError):
            call(model, client)

        self.assertEqual(client.calls, [])

    def test_an_unmapped_provider_type_is_refused_as_configuration(self):
        """L4 - a routing gap must not escape the family; triage's fail-open catches only that."""
        model = fixtures.create_llmmodel()
        model.provider.provider_type = "some-new-service"
        client = FakeLLMClient()

        with self.assertRaises(LLMConfigurationError):
            call(model, client)

        self.assertEqual(client.calls, [])
        # A refusal is not a call, so it leaves no charge behind (L1 covers calls only).
        self.assertEqual(LLMUsageRecord.objects.count(), 0)

    def test_an_openai_compatible_provider_without_a_url_is_refused(self):
        """With no api_base, litellm's openai prefix would post this deployment's key elsewhere."""
        integration = fixtures.create_external_integration(name="Blanked", remote_url="")
        provider = fixtures.create_llmprovider(name="Blanked Provider", external_integration=integration)
        model = fixtures.create_llmmodel(provider=provider)
        client = FakeLLMClient()

        with self.assertRaises(LLMConfigurationError):
            call(model, client)

        self.assertEqual(client.calls, [])

    def test_get_model_resolves_and_refuses(self):
        """Resolution by names, and the same refusals for missing or disabled entries."""
        model = fixtures.create_llmmodel()
        self.assertEqual(llm_service.get_model("Test Provider", "test-model"), model)

        with self.assertRaises(LLMConfigurationError):
            llm_service.get_model("Test Provider", "no-such-model")

        fixtures.create_llmmodel(name="switched-off", enabled=False)
        with self.assertRaises(LLMConfigurationError):
            llm_service.get_model("Test Provider", "switched-off")

    def test_missing_litellm_names_the_extra(self):
        """The real client without the package installed points at the fix."""
        import sys  # pylint: disable=import-outside-toplevel

        model = fixtures.create_llmmodel()
        already_present = sys.modules.get("litellm")
        sys.modules["litellm"] = None
        try:
            with self.assertRaises(ImproperlyConfigured) as raised:
                call(model, client=None)
        finally:
            if already_present is None:
                sys.modules.pop("litellm", None)
            else:
                sys.modules["litellm"] = already_present

        self.assertIn("llm", str(raised.exception))
        # The refusal happened at import time, before any call was attempted, so nothing is on
        # the record: the call never left the process.
        self.assertEqual(LLMUsageRecord.objects.count(), 0)


class TestRetention(TestCase):
    """L7 - old records are pruned by the service itself, once a day."""

    def setUp(self):
        """Reset the once-a-day memo other tests may have set."""
        super().setUp()
        llm_service._last_pruned_on = None  # pylint: disable=protected-access
        llm_service._prune_failures = 0  # pylint: disable=protected-access
        self.addCleanup(setattr, llm_service, "_last_pruned_on", None)
        self.addCleanup(setattr, llm_service, "_prune_failures", 0)
        self.model = fixtures.create_llmmodel()

    def _age_record(self, record, days):
        """Backdate a record, bypassing the sole-writer path only in the queryset way tests may."""
        LLMUsageRecord.objects.filter(pk=record.pk).update(called_at=timezone.now() - timedelta(days=days))

    def test_records_past_retention_are_deleted(self):
        """Ninety days is the default window."""
        old = call(self.model, FakeLLMClient()).record
        self._age_record(old, days=91)
        llm_service._last_pruned_on = None  # pylint: disable=protected-access

        kept = call(self.model, FakeLLMClient()).record

        remaining = set(LLMUsageRecord.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {kept.pk})

    def test_pruning_runs_at_most_once_per_day(self):
        """The second call on the same day does not pay for a second DELETE."""
        call(self.model, FakeLLMClient())
        old = call(self.model, FakeLLMClient()).record
        self._age_record(old, days=91)

        # The memo was set by the first call, so this call must leave the old row alone.
        call(self.model, FakeLLMClient())
        self.assertIn(old.pk, set(LLMUsageRecord.objects.values_list("pk", flat=True)))

    def test_get_settings_applies_defaults_per_key(self):
        """An absent block gets the defaults; a partial block keeps them for unstated keys."""
        self.assertEqual(llm_service.get_settings()["usage_retention_days"], 90)

    def test_a_retention_window_of_zero_is_refused(self):
        """Read as "keep nothing", zero would empty the table the accounting lives in.

        `app-config-schema.json` says `minimum: 1`, but Nautobot does not enforce that schema
        against PLUGINS_CONFIG, so the check has to be in the code that reads the value.
        """
        for value in (0, -1, "ninety", True):
            with self.subTest(value=value):
                with override_settings(
                    PLUGINS_CONFIG={"nautobot_event_tracker": {"llm": {"usage_retention_days": value}}}
                ):
                    with self.assertRaises(ImproperlyConfigured) as raised:
                        llm_service.get_settings()
                    self.assertIn("usage_retention_days", str(raised.exception))

    def test_a_refused_retention_window_deletes_nothing(self):
        """A settings fault must not become a data-loss event, and must not break the call it rode in on."""
        old = call(self.model, FakeLLMClient()).record
        self._age_record(old, days=91)
        llm_service._last_pruned_on = None  # pylint: disable=protected-access

        with override_settings(PLUGINS_CONFIG={"nautobot_event_tracker": {"llm": {"usage_retention_days": 0}}}):
            kept = call(self.model, FakeLLMClient()).record

        self.assertIn(old.pk, set(LLMUsageRecord.objects.values_list("pk", flat=True)))
        self.assertIn(kept.pk, set(LLMUsageRecord.objects.values_list("pk", flat=True)))

    def test_a_failed_prune_is_retried_rather_than_blocked_for_the_day(self):
        """A lock or a statement timeout on a first prune of a large table is transient.

        Marking the day before the DELETE would let the table grow for 24 hours on every such
        failure, which is the opposite of what the retention window is for.
        """
        old = call(self.model, FakeLLMClient()).record
        self._age_record(old, days=91)
        llm_service._last_pruned_on = None  # pylint: disable=protected-access

        with mock.patch("django.db.models.query.QuerySet.delete", side_effect=DatabaseError("deadlock detected")):
            llm_service._maybe_prune()  # pylint: disable=protected-access
        self.assertIsNone(llm_service._last_pruned_on)  # pylint: disable=protected-access

        # The next call, with the database well again, prunes as it should have.
        call(self.model, FakeLLMClient())
        self.assertNotIn(old.pk, set(LLMUsageRecord.objects.values_list("pk", flat=True)))

    def test_a_persistent_failure_stops_retrying_for_the_day(self):
        """A missing privilege fails identically every time, and the retry is on the hot path.

        Unbounded, it means a whole-table DELETE attempt and a traceback for every event the
        consumer triages, forever. Bounded, the operator still gets the log lines and the table
        still waits only until tomorrow.
        """
        with mock.patch("django.db.models.query.QuerySet.delete", side_effect=DatabaseError("permission denied")):
            for _ in range(llm_service.MAX_PRUNE_FAILURES):
                llm_service._maybe_prune()  # pylint: disable=protected-access

        self.assertIsNotNone(llm_service._last_pruned_on)  # pylint: disable=protected-access

        # And with the day marked, the next call does not try again.
        with mock.patch("django.db.models.query.QuerySet.delete", side_effect=AssertionError("tried again")):
            llm_service._maybe_prune()  # pylint: disable=protected-access


class TestToolCalls(TestCase):
    """Section 8 - the tool-calling extension. One new field, two new arguments, no new rule."""

    def setUp(self):
        """One registered model to call."""
        self.model = fixtures.create_llmmodel()

    def complete(self, response, **kwargs):
        """One call through the service with this canned response."""
        client = FakeLLMClient(response)
        result = llm_service.complete(
            model=self.model,
            messages=[{"role": "user", "content": "hello"}],
            purpose=LLMPurposeChoices.AGENT,
            client=client,
            **kwargs,
        )
        self.client = client  # pylint: disable=attribute-defined-outside-init
        return result

    def test_a_plain_completion_asks_for_no_tools(self):
        """A call made without `tools` is exactly the call it was before this phase."""
        response = self.complete(FakeLLMResponse("hello"))

        self.assertEqual(response.tool_calls, ())
        self.assertNotIn("tools", self.client.calls[0])

    def test_tool_definitions_reach_the_client(self):
        """litellm translates them per provider; the app builds them from the registry."""
        definitions = [{"type": "function", "function": {"name": "look", "parameters": {}}}]

        self.complete(FakeLLMResponse("hello"), tools=definitions, tool_choice="auto")

        self.assertEqual(self.client.calls[0]["tools"], definitions)
        self.assertEqual(self.client.calls[0]["tool_choice"], "auto")

    def test_the_calls_a_model_asked_for_are_parsed(self):
        """Arguments arrive as a JSON string and are handed back as a dictionary."""
        raw = FakeLLMResponse(None, tool_calls=[fixtures.fake_tool_call("look", {"device": "leaf-01"})])

        response = self.complete(raw)

        self.assertEqual(len(response.tool_calls), 1)
        parsed = response.tool_calls[0]
        self.assertEqual((parsed.identifier, parsed.name), ("call-1", "look"))
        self.assertEqual(parsed.arguments, {"device": "leaf-01"})

    def test_a_tool_call_with_no_content_is_a_complete_answer(self):
        """A model that asked for a tool sends no message text, and that is not an empty answer."""
        raw = FakeLLMResponse(None, tool_calls=[fixtures.fake_tool_call("look")])

        response = self.complete(raw)

        self.assertEqual(response.text, "")
        self.assertTrue(response.record.success)

    def test_an_argument_less_tool_is_ordinary(self):
        """Providers spell "no arguments" three different ways, and all three mean this."""
        for arguments in ("", "{}", None):
            with self.subTest(arguments=arguments):
                raw = FakeLLMResponse(None, tool_calls=[fixtures.fake_tool_call("look", arguments)])

                response = self.complete(raw)

                self.assertEqual(response.tool_calls[0].arguments, {})

    def test_unparsable_arguments_are_a_response_error(self):
        """A call the app cannot read is not a call it should guess at."""
        raw = FakeLLMResponse(None, tool_calls=[fixtures.fake_tool_call("look", "{not json")])

        with self.assertRaises(LLMResponseError) as caught:
            self.complete(raw)

        self.assertIn("not valid JSON", str(caught.exception))

    def test_an_unusable_tool_call_is_still_on_the_record(self):
        """L1 - the call was made and paid for, whatever came back."""
        raw = FakeLLMResponse(None, tool_calls=[fixtures.fake_tool_call("look", "{not json")])

        with self.assertRaises(LLMResponseError) as caught:
            self.complete(raw)

        record = caught.exception.record
        self.assertFalse(record.success)
        self.assertIn("not valid JSON", record.error)
        self.assertEqual(LLMUsageRecord.objects.count(), 1)

    def test_a_tool_call_with_no_name_is_a_response_error(self):
        """There is nothing to look up, and guessing is how a call reaches the wrong tool."""
        raw = FakeLLMResponse(None, tool_calls=[fixtures.fake_tool_call("")])

        with self.assertRaises(LLMResponseError):
            self.complete(raw)

    def test_an_empty_answer_is_still_an_error(self):
        """Neither text nor tool calls is the case the old message was written for."""
        with self.assertRaises(LLMResponseError):
            self.complete(FakeLLMResponse(None))

    def test_a_tool_calling_response_is_priced_like_any_other(self):
        """L5 is untouched: a call that asked for tools costs what its tokens cost."""
        raw = FakeLLMResponse(None, tool_calls=[fixtures.fake_tool_call("look")])

        response = self.complete(raw)

        self.assertEqual(response.prompt_tokens, 10)
        self.assertEqual(response.completion_tokens, 5)


class TestTheClientIsResolvable(TestCase):
    """What `require_client()` says when the dependency will not import."""

    def test_the_reason_is_in_the_message(self):
        """ "Not installed" is one explanation of an ImportError, and not always the right one.

        litellm 1.98.0 imported `typing.NotRequired` while declaring support for Python 3.10, so it
        installed there and then could not be imported. The message said to install it, which was
        already done - and because the cause was chained rather than reported, a CI log showed only
        the wrong advice. This is that hour, spent once.
        """
        with mock.patch.dict("sys.modules", {"litellm": None}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                llm_service.require_client()

        message = str(caught.exception)
        self.assertIn("could not be imported", message)
        # The underlying failure's own type and text, which is the half that was missing.
        self.assertIn("ModuleNotFoundError", message)
        self.assertIn("litellm", message)
        # And still the advice, for the case where it really is not installed.
        self.assertIn("'llm' extra", message)
