"""Tests that run the real litellm with a patched HTTP transport and no network.

The Phase 3 spec (section 11) names this module as its one exception to the rule that no test goes
past the `client` seam. Only litellm can show which parameters it refuses, and a fake client cannot.
"""

import atexit
import importlib
import importlib.util
import json
import logging
import os
import socket
import sys
import threading
import unittest
from unittest import mock

from nautobot.apps.testing import TestCase
from nautobot_ai_models.constants import ALLOWED_MODEL_PARAMETERS

from nautobot_event_tracker.choices import LLMPurposeChoices
from nautobot_event_tracker.models import LLMUsageRecord
from nautobot_event_tracker.services import llm as llm_service
from nautobot_event_tracker.services.exceptions import LLMCallError
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
        self.headers = []

        async def fake_send(_client, request, **_kwargs):
            self.assertEqual(str(request.url), COMPLETIONS_URL)
            self.bodies.append(json.loads(request.content))
            self.headers.append(request.headers)
            return httpx.Response(200, request=request, json=CANNED_ANSWER)

        environment = mock.patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"})
        environment.start()
        self.addCleanup(environment.stop)
        llm_service.require_client()
        transport = mock.patch.object(httpx.AsyncClient, "send", fake_send)
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

        async def refuse_async_request(_client, request, **_kwargs):
            attempts.append(str(request.url))
            raise OSError("network blocked by the test")

        def litellm_handlers():
            return {
                name: list(logging.getLogger(name).handlers)
                for name in list(logging.Logger.manager.loggerDict)
                if name.startswith("LiteLLM")
            }

        loaded = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "litellm"}
        handlers_before = litellm_handlers()
        for name in loaded:
            del sys.modules[name]
        try:
            with (
                mock.patch.object(atexit, "register") as register_callback,
                mock.patch.object(socket, "getaddrinfo", refuse_network),
                mock.patch.object(socket, "create_connection", refuse_network),
                mock.patch.object(socket.socket, "connect", refuse_network),
                mock.patch.object(self.httpx.Client, "send", refuse_request),
                mock.patch.object(self.httpx.AsyncClient, "send", refuse_async_request),
            ):
                importlib.import_module("litellm")
        finally:
            for name in [name for name in sys.modules if name.split(".")[0] == "litellm"]:
                del sys.modules[name]
            sys.modules.update(loaded)
            for name in list(logging.Logger.manager.loggerDict):
                if name.startswith("LiteLLM"):
                    logging.getLogger(name).handlers = handlers_before.get(name, [])

        self.assertEqual(attempts, [], "litellm tried to reach the network at import")
        self.assertEqual(litellm_handlers(), handlers_before, "the second import left a log handler behind")
        register_callback.assert_called()

    def complete(self, model, **kwargs):
        """One call through the service with the real client."""
        return llm_service.complete(model=model, messages=MESSAGES, purpose=LLMPurposeChoices.TRIAGE, **kwargs)

    def test_the_real_client_runs_in_a_worker_thread_inside_the_deadline(self):
        """AC-6 - the request leaves from a thread that is not the caller's, and the wrapper holds the call."""
        threads = []
        original_send = self.httpx.AsyncClient.send

        async def record_thread(client, request, **kwargs):
            threads.append(threading.current_thread() is threading.main_thread())
            return await original_send(client, request, **kwargs)

        model = fixtures.create_llmmodel(name="real-thread-model")
        with (
            mock.patch.object(llm_service, "_within_deadline", wraps=llm_service._within_deadline) as wrapper,
            mock.patch.object(self.httpx.AsyncClient, "send", record_thread),
        ):
            response = self.complete(model)

        self.assertEqual(response.text, "answer")
        self.assertEqual(wrapper.call_count, 1)
        self.assertEqual(threads, [False])

    def test_a_call_with_no_retry_sends_one_request_when_the_server_times_out(self):
        """AC-6 - `max_retries=0` is the only way to keep the SDK from sending the request again."""
        attempts = []

        async def time_out(_client, request, **_kwargs):
            attempts.append(str(request.url))
            raise self.httpx.ReadTimeout("no answer", request=request)

        model = fixtures.create_llmmodel(name="real-no-retry-model")

        with mock.patch.object(self.httpx.AsyncClient, "send", time_out):
            with self.assertRaises(LLMCallError):
                self.complete(model, max_retries=0)

        self.assertEqual(len(attempts), 1)

    def test_an_embedding_call_sends_one_request_when_the_server_times_out(self):
        """AC-3 - the call makes one attempt, though litellm would send the request twice more."""
        attempts = []

        async def time_out(_client, request, **_kwargs):
            attempts.append(str(request.url))
            raise self.httpx.ReadTimeout("no answer", request=request)

        model = fixtures.create_embedding_model(name="real-embedding-timeout-model")

        with (
            mock.patch("litellm.DEFAULT_MAX_RETRIES", 2),
            mock.patch.object(self.httpx.AsyncClient, "send", time_out),
        ):
            with self.assertRaises(LLMCallError):
                llm_service.embed(model=model, text="hello")

        self.assertEqual(len(attempts), 1)

    def test_an_embedding_call_sends_one_request_when_the_server_answers_429(self):
        """AC-4 - a rate limit answer does not start a second request."""
        attempts = []

        async def rate_limit(_client, request, **_kwargs):
            attempts.append(str(request.url))
            return self.httpx.Response(429, request=request, json={"error": {"message": "rate limit"}})

        model = fixtures.create_embedding_model(name="real-embedding-429-model")

        with (
            mock.patch("litellm.DEFAULT_MAX_RETRIES", 2),
            mock.patch.object(self.httpx.AsyncClient, "send", rate_limit),
        ):
            with self.assertRaises(LLMCallError):
                llm_service.embed(model=model, text="hello")

        self.assertEqual(len(attempts), 1)

    def test_a_call_with_no_retry_sends_one_request_when_the_server_answers_429(self):
        """AC-4 - `max_retries=0` also holds for a rate limit answer on a completion."""
        attempts = []

        async def rate_limit(_client, request, **_kwargs):
            attempts.append(str(request.url))
            return self.httpx.Response(429, request=request, json={"error": {"message": "rate limit"}})

        model = fixtures.create_llmmodel(name="real-no-retry-429-model")

        with mock.patch.object(self.httpx.AsyncClient, "send", rate_limit):
            with self.assertRaises(LLMCallError):
                self.complete(model, max_retries=0)

        self.assertEqual(len(attempts), 1)

    def test_the_session_id_reaches_the_request_headers(self):
        """AC-6 - the rendered Headers template is in the HTTP request, and each call has its own value."""
        integration = fixtures.create_external_integration(
            name="Session Header Endpoint", headers={"x-opencode-session": "{{ session_id }}"}
        )
        provider = fixtures.create_llmprovider(name="Session Header Provider", external_integration=integration)
        model = fixtures.create_llmmodel(name="real-session-model", provider=provider)

        self.complete(model, session_id="run-a")
        self.complete(model, session_id="run-b")

        self.assertEqual([headers["x-opencode-session"] for headers in self.headers], ["run-a", "run-b"])

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
