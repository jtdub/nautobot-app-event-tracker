"""Tests that run the real litellm with a patched HTTP transport and no network.

The Phase 3 spec (section 11) names this module as its one exception to the rule that no test goes
past the `client` seam. Only litellm can show which parameters it refuses, and a fake client cannot.
"""

import importlib
import importlib.util
import json
import os
import socket
import sys
import unittest
from unittest import mock

from nautobot.apps.testing import TestCase
from nautobot_ai_models.constants import ALLOWED_MODEL_PARAMETERS

from nautobot_event_tracker.choices import LLMPurposeChoices
from nautobot_event_tracker.models import LLMUsageRecord
from nautobot_event_tracker.services import llm as llm_service
from nautobot_event_tracker.tests import fixtures

COMPLETIONS_URL = "http://llm.example.test/v1/chat/completions"
MESSAGES = [{"role": "user", "content": "hello"}]
CANNED_ANSWER = {
    "id": "req-1",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
VALID_VALUES = {
    "extra_body": {"x": 1},
    "frequency_penalty": 0.5,
    "logit_bias": {"1": 1},
    "n": 1,
    "presence_penalty": 0.5,
    "reasoning_effort": "none",
    "seed": 1,
    "stop": ["x"],
    "temperature": 0.1,
    "timeout": 5,
    "top_k": 5,
    "top_p": 0.5,
}


@unittest.skipUnless(importlib.util.find_spec("litellm"), "litellm is not installed")
class TestTheRealClient(TestCase):
    """`complete()` with no `client` argument, against a patched transport."""

    def setUp(self):
        """Keep litellm off the network and catch each request that it sends."""
        super().setUp()
        import httpx  # pylint: disable=import-outside-toplevel

        self.httpx = httpx
        self.bodies = []

        def fake_send(_client, request, **_kwargs):
            self.assertEqual(str(request.url), COMPLETIONS_URL)
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, request=request, json=CANNED_ANSWER)

        environment = mock.patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"})
        environment.start()
        self.addCleanup(environment.stop)
        llm_service.require_client()
        transport = mock.patch.object(httpx.Client, "send", fake_send)
        transport.start()
        self.addCleanup(transport.stop)

    def test_importing_litellm_makes_no_network_request(self):
        """Import litellm again with sockets blocked, so an earlier import cannot hide a request.

        The test records each attempt, because litellm catches the error of a failed request. It sets the loaded
        litellm modules aside and restores them after the import. It proves the import
        with `LITELLM_LOCAL_MODEL_COST_MAP` set. It does not prove the later calls, which the patched transport
        covers.
        """

        attempts = []

        def refuse_network(*args, **_kwargs):
            attempts.append(args[:2])
            raise OSError("network blocked by the test")

        def refuse_request(_client, request, **_kwargs):
            attempts.append(str(request.url))
            raise OSError("network blocked by the test")

        loaded = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "litellm"}
        for name in loaded:
            del sys.modules[name]
        try:
            with (
                mock.patch.object(socket, "getaddrinfo", refuse_network),
                mock.patch.object(socket, "create_connection", refuse_network),
                mock.patch.object(socket.socket, "connect", refuse_network),
                mock.patch.object(self.httpx.Client, "send", refuse_request),
            ):
                importlib.import_module("litellm")
            self.assertEqual(attempts, [], "litellm tried to reach the network at import")
        finally:
            for name in [name for name in sys.modules if name.split(".")[0] == "litellm"]:
                del sys.modules[name]
            sys.modules.update(loaded)

    def complete(self, model):
        """One call through the service with the real client."""
        return llm_service.complete(model=model, messages=MESSAGES, purpose=LLMPurposeChoices.TRIAGE)

    def test_a_top_level_reasoning_effort_reaches_the_request_body(self):
        """litellm sends the key at the top level and keeps the allowance out of the body."""
        model = fixtures.create_llmmodel(name="real-reasoning-model", default_parameters={"reasoning_effort": "none"})

        response = self.complete(model)

        self.assertEqual(self.bodies[0]["reasoning_effort"], "none")
        self.assertNotIn("allowed_openai_params", self.bodies[0])
        self.assertEqual(response.text, "answer")
        self.assertTrue(LLMUsageRecord.objects.get().success)

    def test_the_extra_body_value_reaches_the_request_body(self):
        """A row with the two forms sends the `extra_body` value."""
        model = fixtures.create_llmmodel(
            name="real-both-forms-model",
            default_parameters={"reasoning_effort": "low", "extra_body": {"reasoning_effort": "none"}},
        )

        self.complete(model)

        self.assertEqual(self.bodies[0]["reasoning_effort"], "none")

    def test_litellm_accepts_each_allowed_parameter(self):
        """A new allowed key needs a value here, and a new refusal of litellm fails a subtest."""
        self.assertEqual(
            set(VALID_VALUES),
            set(ALLOWED_MODEL_PARAMETERS),
            "ALLOWED_MODEL_PARAMETERS changed. Add a valid value for the new key to VALID_VALUES.",
        )
        for key, value in VALID_VALUES.items():
            with self.subTest(key=key):
                model = fixtures.create_llmmodel(name=f"real-{key}-model", default_parameters={key: value})

                self.complete(model)

                self.assertTrue(LLMUsageRecord.objects.filter(model=model, success=True).exists())
