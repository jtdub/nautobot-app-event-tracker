"""Create fixtures for tests.

Tickets are built **through the service layer**, never by direct ORM creation with an arbitrary
status. Where a test needs a ticket in a later state, it walks the workflow graph to get there.
This is slower than setting `status` directly, and it is deliberate: a fixture that assigned status
would be the first violation of the rule the whole app exists to enforce.
"""

import json
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from django.utils import timezone
from nautobot.dcim.models import Location, LocationType
from nautobot.extras.models import Status

from nautobot_event_tracker import dcim_fixtures
from nautobot_event_tracker.choices import (
    LLMProviderTypeChoices,
    LLMPurposeChoices,
    SeverityChoices,
    TicketSourceChoices,
)
from nautobot_event_tracker.ingestion.consumers import BrokerMessage, EventConsumer
from nautobot_event_tracker.models import EventType, IngestionStats, LLMModel, LLMProvider
from nautobot_event_tracker.services import llm as llm_service
from nautobot_event_tracker.services import tickets as ticket_service


def create_user(username="tester"):
    """Return a user to act as the human source."""
    user, _ = get_user_model().objects.get_or_create(username=username)
    return user


def create_event_types():
    """Create a small set of event types for tests."""
    return [
        EventType.objects.get_or_create(
            name="Test Interface Down",
            defaults={"default_severity": SeverityChoices.MAJOR},
        )[0],
        EventType.objects.get_or_create(
            name="Test Device Unreachable",
            defaults={"default_severity": SeverityChoices.CRITICAL},
        )[0],
        EventType.objects.get_or_create(
            name="Test Disabled Type",
            defaults={"default_severity": SeverityChoices.INFO, "enabled": False},
        )[0],
    ]


def create_location(name="Test Location"):
    """Create a Location, used as the attachable object in attachment tests."""
    location_status = Status.objects.get_for_model(Location).first()
    location_type, _ = LocationType.objects.get_or_create(name="Test Site")
    location_type.content_types.add(ContentType.objects.get_for_model(Location))
    location, _ = Location.objects.get_or_create(
        name=name,
        defaults={"location_type": location_type, "status": location_status},
    )
    return location


def create_device(name="somebody-elses-device"):
    """A device this app did not create, for the tests that must not touch one.

    Built with the same helpers the test data command uses, so that a change to what a Device
    requires is made in one place - but under different names, because the whole point of this
    fixture is to be somebody else's.
    """
    device, _ = dcim_fixtures.ensure_device(
        name,
        location=dcim_fixtures.ensure_location(location_type_name="Test Site", location_name="Test Device Location"),
        device_type=dcim_fixtures.ensure_device_type(manufacturer_name="Test Manufacturer", model_name="Test Model"),
        role=dcim_fixtures.ensure_role(role_name="Test Device Role"),
    )
    return device


def create_interface(device, name="ethernet-1/1"):
    """One interface on a device, for the enrichment resolver's scoped lookups."""
    return dcim_fixtures.ensure_interface(device=device, name=name)


#: Enrichment rules matching `event_payload()`: the device by the hostname it logged, and the
#: interface inside that device. The shape the lab uses, so the tests and the lab agree.
INGESTION_RESOLVE = [
    {"name": "device", "path": "host", "model": "dcim.device", "field": "name"},
    {
        "name": "interface",
        "path": "interface",
        "model": "dcim.interface",
        "field": "name",
        "scope": {"device": "device"},
    },
]


def create_ticket(user=None, event_type=None, **kwargs):
    """Create one ticket through the service layer."""
    if event_type is None:
        event_type = create_event_types()[0]
    if user is None:
        user = create_user()
    kwargs.setdefault("title", "Test ticket")
    return ticket_service.create_ticket(
        event_type=event_type,
        source=TicketSourceChoices.HUMAN,
        user=user,
        **kwargs,
    )


def create_ticket_in_status(status, user=None, **kwargs):
    """Create a ticket and walk the graph until it reaches `status`."""
    if user is None:
        user = create_user()
    ticket = create_ticket(user=user, **kwargs)
    ticket_service.walk_to_status(
        ticket=ticket,
        to_status=status,
        source=TicketSourceChoices.HUMAN,
        user=user,
        resolution="Fixed in tests.",
    )
    ticket.refresh_from_db()
    return ticket


def create_eventticket():
    """Create the standard three tickets used by the generic view and API test cases."""
    user = create_user()
    event_type = create_event_types()[0]
    return [
        create_ticket(user=user, event_type=event_type, title=title)
        for title in ("Ticket One", "Ticket Two", "Ticket Three")
    ]


class FakeEventConsumer(EventConsumer):
    """An in-memory broker, so no test in CI needs a real one.

    Records what was acknowledged, which is how the at-least-once tests check that a message whose
    ticket failed was left on the queue.
    """

    supports_replay = False

    def __init__(self, *, settings=None, topics=(), messages=()):
        """Queue these messages for delivery."""
        super().__init__(settings=settings or {}, topics=topics)
        self.messages = list(messages)
        self.acknowledged = []
        self.connected = False
        self.closed = False

    def connect(self):
        """Nothing to connect to, but the loop expects to be able to say so."""
        self.connected = True

    def poll(self, timeout):
        """Hand over the next queued message, or None once they run out."""
        if not self.messages:
            return None
        return self.messages.pop(0)

    def acknowledge(self, message):
        """Record the acknowledgement rather than sending one."""
        self.acknowledged.append(message)

    def close(self):
        """Note that the loop closed us, which the shutdown tests assert."""
        self.closed = True


def broker_message(payload, *, topic="network.events", **kwargs):
    """Build a BrokerMessage carrying this payload as JSON."""
    return BrokerMessage(topic=topic, value=json.dumps(payload).encode("utf-8"), **kwargs)


#: A topic configuration the ingestion tests share, so the pipeline, the command and the pre-filter
#: are all exercised against the same shape of payload.
INGESTION_TOPIC = {
    "field_map": {"event_type": "event.type", "title": "message", "severity": "event.severity"},
    "defaults": {"event_type": "Test Interface Down"},
    "dedup_key_template": "{event.type}:{host}",
}


def event_payload(**overrides):
    """A payload the pre-filter accepts and the pipeline turns into a ticket."""
    base = {
        "event": {"type": "Test Interface Down", "severity": SeverityChoices.MAJOR},
        "message": "Interface ethernet-1/1 is down",
        "host": "leaf-01",
    }
    base.update(overrides)
    return base


class FakeClock:
    """A monotonic clock a test moves by hand, for flush intervals and token buckets."""

    def __init__(self, start=0.0):
        """Start here."""
        self.now = start

    def __call__(self):
        """Read the clock, as `time.monotonic` would."""
        return self.now

    def advance(self, seconds):
        """Move time forward."""
        self.now += seconds


class FakeWallClock:
    """Wall time a test moves by hand, for the bucket a count lands in."""

    def __init__(self, start=datetime(2026, 8, 15, 3, 14, tzinfo=datetime_timezone.utc)):
        """Start at a fixed moment, so buckets are predictable."""
        self.now = start

    def __call__(self):
        """Read the clock, as `timezone.now` would."""
        return self.now

    def advance(self, **kwargs):
        """Move wall time forward."""
        self.now += timedelta(**kwargs)


def app_settings(**overrides):
    """A PLUGINS_CONFIG override that starts from the app's own defaults, as a deployment does.

    Nautobot fills a missing key from `default_settings` when it loads the app, so a real
    `PLUGINS_CONFIG` always carries `attachable_object_types` whether or not anyone wrote it down.
    `override_settings` does not, and a test that replaced the whole block would be testing an
    installation that cannot exist - one where nothing may be attached to a ticket at all.
    """
    from nautobot_event_tracker import EventTrackerConfig  # pylint: disable=import-outside-toplevel

    return override_settings(
        PLUGINS_CONFIG={"nautobot_event_tracker": {**EventTrackerConfig.default_settings, **overrides}}
    )


def ingestion_settings(**overrides):
    """A PLUGINS_CONFIG override carrying this ingestion block.

    One spelling of the app label and the `ingestion` key, so a test that moves between modules
    cannot find two same-named helpers meaning different things.
    """
    block = {"topics": {"network.events": INGESTION_TOPIC}, **overrides}
    return app_settings(ingestion=block)


def create_ingestionstats(**overrides):
    """One ingestion counter row."""
    defaults = {
        "consumer_name": "consumer-1",
        "topic": "network.events",
        "bucket_start": timezone.now().replace(second=0, microsecond=0),
    }
    return IngestionStats.objects.create(**{**defaults, **overrides})


def create_external_integration(name="Test LLM Endpoint", remote_url="http://llm.example.test/v1", **overrides):
    """An ExternalIntegration for an LLM provider to point at."""
    from nautobot.extras.models import ExternalIntegration  # pylint: disable=import-outside-toplevel

    integration, _ = ExternalIntegration.objects.get_or_create(
        name=name, defaults={"remote_url": remote_url, **overrides}
    )
    return integration


def create_llmprovider(name="Test Provider", **overrides):
    """One LLM provider, pointing at a test integration."""
    defaults = {
        "provider_type": LLMProviderTypeChoices.OPENAI_COMPATIBLE,
        "external_integration": create_external_integration(),
    }
    provider, _ = LLMProvider.objects.get_or_create(name=name, defaults={**defaults, **overrides})
    return provider


def create_llmmodel(name="test-model", provider=None, **overrides):
    """One LLM model on a provider."""
    if provider is None:
        provider = create_llmprovider()
    defaults = {
        # Decimal, not string: get_or_create leaves the given value on the in-memory instance,
        # and the service does arithmetic with it before any refresh from the database.
        "input_cost_per_million": Decimal("1.0000"),
        "output_cost_per_million": Decimal("2.0000"),
    }
    model, _ = LLMModel.objects.get_or_create(provider=provider, name=name, defaults={**defaults, **overrides})
    return model


def fake_tool_call(name="get_interface_status", arguments=None, identifier="call-1"):
    """One tool call in the shape a provider sends it: arguments as a JSON string.

    A string rather than a dictionary on purpose. That is what comes over the wire, and parsing it
    is the part of `complete()` these tests exist to exercise.
    """
    return SimpleNamespace(
        id=identifier,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
        ),
    )


class FakeLLMResponse:  # pylint: disable=too-few-public-methods
    """The shape `litellm.completion` returns, as far as the service reads it."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        content="ok",
        *,
        prompt_tokens=10,
        completion_tokens=5,
        request_id="req-1",
        usage=True,
        tool_calls=None,
    ):
        """A successful-looking response carrying this content, these tool calls and this usage."""
        self.id = request_id
        message = SimpleNamespace(content=content, tool_calls=list(tool_calls) if tool_calls else None)
        self.choices = [SimpleNamespace(message=message)] if content is not None or tool_calls else []
        self.usage = (
            SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens) if usage else None
        )


class FakeLLMClient:  # pylint: disable=too-few-public-methods
    """The `client` seam of `services.llm.complete()`: records calls, returns canned responses.

    Raises whatever `error` it was given instead, when the test wants a failing call. Tests inject
    this rather than mocking litellm internals, so they exercise everything up to the wire.
    """

    def __init__(self, response=None, *, error=None):
        """Answer every call with this response, or raise this error."""
        self.response = response if response is not None else FakeLLMResponse()
        self.error = error
        self.calls = []

    def __call__(self, model_string, messages, **kwargs):
        """Record the call, then answer or refuse."""
        self.calls.append({"model_string": model_string, "messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


#: The triage block a test that wants triage on needs, naming the model `create_llmmodel` registers.
TRIAGE_SETTINGS = {"enabled": True, "provider": "Test Provider", "model": "test-model"}

#: What the triage fake says when a test does not care what the model answered.
DEFAULT_TRIAGE_ANSWER = '{"action": "accept", "reason": "looks real"}'


class FakeComplete:  # pylint: disable=too-few-public-methods
    """The `complete` seam of `TriageFilter`: canned text, through the real service and a fake client.

    Routing through `services.llm.complete` keeps rule L1 honest in these tests: every triage
    decision leaves a real usage record behind, exactly as it would in production.
    """

    def __init__(self, text=None, *, error=None):
        """Answer every call with this text, or fail every call with this error."""
        self.text = DEFAULT_TRIAGE_ANSWER if text is None else text
        self.error = error
        self.calls = []

    def __call__(self, **kwargs):
        """Record the call, then answer through the real service."""
        self.calls.append(kwargs)
        client = FakeLLMClient(FakeLLMResponse(self.text), error=self.error)
        return llm_service.complete(**kwargs, client=client)


def create_llmusagerecord(model=None, ticket=None, **complete_kwargs):
    """One usage record, written the only way one may be: by the service making a call.

    The guard tests forbid constructing LLMUsageRecord anywhere else, fixtures included, so this
    goes through `complete()` with a fake client rather than the ORM.
    """
    if model is None:
        model = create_llmmodel()
    complete_kwargs.setdefault("client", FakeLLMClient())
    response = llm_service.complete(
        model=model,
        ticket=ticket,
        messages=[{"role": "user", "content": "fixture"}],
        purpose=LLMPurposeChoices.TRIAGE,
        **complete_kwargs,
    )
    return response.record


def create_mcpserver(name="Test MCP Server", **overrides):
    """One registered MCP server, pointing at an integration made for it.

    Reuses a server of this name when one already exists, so that `create_mcptool()` can fall back
    to a default server without every caller having to know whether somebody made it first.
    """
    from nautobot_event_tracker.models import MCPServer  # pylint: disable=import-outside-toplevel

    existing = MCPServer.objects.filter(name=name).first()
    if existing is not None:
        # Whatever the overrides say. Falling through with them would build a second server of the
        # same unique name and fail on save, which is a confusing way for a fixture to report
        # "somebody already made this one".
        return existing

    defaults = {
        "external_integration": create_external_integration(
            name=f"{name} Endpoint", remote_url="https://mcp.example.test/mcp"
        ),
    }
    server = MCPServer(name=name, **{**defaults, **overrides})
    server.validated_save()
    return server


def create_mcptool(server=None, name="get_interface_status", **overrides):
    """One tool on a server, disabled and mutating unless a test says otherwise.

    The defaults are the model's own, restated here so a test that wants a callable tool has to
    say so - which is the same thing an operator has to do.
    """
    from nautobot_event_tracker.models import MCPTool  # pylint: disable=import-outside-toplevel

    if server is None:
        server = create_mcpserver()
    tool = MCPTool(server=server, name=name, **overrides)
    tool.validated_save()
    return tool


class FakeMCPClient:  # pylint: disable=too-few-public-methods
    """The `client` seam of `services.mcp`: records connections, returns canned tool lists.

    Tests inject this rather than mocking the MCP SDK's internals, so they exercise everything up
    to the wire and no test opens a socket.
    """

    def __init__(self, tools=(), *, error=None):
        """Advertise these tools, or raise this error instead."""
        self.tools = tuple(tools)
        self.error = error
        self.connections = []

    def list_tools(self, connection):
        """Record the connection, then answer or refuse."""
        self.connections.append(connection)
        if self.error is not None:
            raise self.error
        return self.tools


def tool_definition(name="get_interface_status", **overrides):
    """One advertised tool, as `services.mcp` models what a server said."""
    from nautobot_event_tracker.services.mcp import ToolDefinition  # pylint: disable=import-outside-toplevel

    defaults = {
        "description": "Read an interface's operational state.",
        "input_schema": {"type": "object", "properties": {"device": {"type": "string"}}},
    }
    return ToolDefinition(name=name, **{**defaults, **overrides})


#: The agent block a test that wants agents on needs, naming the provider and model the LLM
#: fixtures register. Small bounds, so a test that means to hit one does not have to loop eight
#: times to get there.
AGENT_SETTINGS = {
    "enabled": True,
    "provider": "Test Provider",
    "model": "test-model",
    "max_iterations": 3,
    "max_tool_calls": 4,
}


def agent_settings(**overrides):
    """A PLUGINS_CONFIG override with the agent switched on and these keys changed."""
    return app_settings(agent={**AGENT_SETTINGS, **overrides})


class FakeAgentComplete:  # pylint: disable=too-few-public-methods
    """The `complete` seam of `services.agent`: a scripted sequence of model turns.

    Each entry is either a string, which becomes a plain answer that ends the run, or a list of
    fake tool calls, which becomes a turn that asks for them. The last entry repeats, so a test
    that wants a bound reached does not have to script every iteration.

    Answers through the real `services.llm.complete` with a fake client, which keeps rule L1 honest
    in these tests: every turn leaves a real usage record behind, exactly as it would in production.
    """

    def __init__(self, *turns):
        """Answer the run's turns with these, in order."""
        self.turns = list(turns) or ["Nothing to report."]
        self.calls = []

    def __call__(self, **kwargs):
        """Record the call, then answer through the real service."""
        self.calls.append(kwargs)
        turn = self.turns[min(len(self.calls) - 1, len(self.turns) - 1)]
        if isinstance(turn, str):
            response = FakeLLMResponse(turn)
        else:
            response = FakeLLMResponse(None, tool_calls=turn)
        return llm_service.complete(**kwargs, client=FakeLLMClient(response))

    @property
    def offered_tools(self):
        """The tool names offered on the most recent call, which is what rule M4 is visible as."""
        tools = self.calls[-1].get("tools") or []
        return [definition["function"]["name"] for definition in tools]


class FakeToolCaller:  # pylint: disable=too-few-public-methods
    """The `call_tool` seam of `services.agent`: records the calls, writes a plausible outcome.

    Writes the row the way `services.mcp.call_tool` does, because the agent reads it back: a test
    that faked the call without recording it would be testing a loop that never sees a result.
    """

    def __init__(self, text="all good", *, error=None):
        """Answer every call with this text, or refuse every call with this error."""
        self.text = text
        self.error = error
        self.calls = []

    def __call__(self, *, tool_call, timeout=None, max_result_chars=None):
        """Record the call, write the row, then answer or refuse.

        Written with `update()` rather than by assigning the fields, because the status-assignment
        guard forbids `<something>.status = ...` outside the service layer - and it is right to:
        the one place that may write a call's outcome is `services.mcp`, and a fixture is not it.
        """
        from nautobot_event_tracker.choices import (  # pylint: disable=import-outside-toplevel
            AgentToolCallStatusChoices,
        )
        from nautobot_event_tracker.models import AgentToolCall  # pylint: disable=import-outside-toplevel

        self.calls.append({"tool_call": tool_call, "timeout": timeout, "max_result_chars": max_result_chars})
        rows = AgentToolCall.objects.filter(pk=tool_call.pk)
        if self.error is not None:
            rows.update(status=AgentToolCallStatusChoices.FAILED, error=str(self.error))
            tool_call.refresh_from_db()
            raise self.error
        rows.update(
            status=AgentToolCallStatusChoices.EXECUTED,
            result={"is_error": False, "text": self.text},
        )
        tool_call.refresh_from_db()
        return tool_call


class FakeCallToolResult:  # pylint: disable=too-few-public-methods
    """What the MCP SDK hands back from `call_tool`, as far as `services.mcp` reads it."""

    def __init__(self, text="ok", *, is_error=False, structured_content=None):
        """One text block, optionally an error and optionally structured content."""
        self.content = [SimpleNamespace(type="text", text=text)] if text is not None else []
        self.is_error = is_error
        self.structured_content = structured_content


class FakeMCPCaller:  # pylint: disable=too-few-public-methods
    """The `client` seam of `services.mcp.call_tool`: records connections, returns canned results."""

    def __init__(self, result=None, *, error=None):
        """Answer every call with this result, or raise this error."""
        self.result = result if result is not None else FakeCallToolResult()
        self.error = error
        self.calls = []

    def call_tool(self, connection, name, arguments):
        """Record what would have gone over the wire, then answer or refuse."""
        self.calls.append({"connection": connection, "name": name, "arguments": arguments})
        if self.error is not None:
            raise self.error
        return self.result


def create_agentrun(ticket=None, **overrides):
    """One agent run, written directly because a test needs a run without a model behind it.

    The guards forbid this everywhere but here and the model tests, for the reason every other
    record model has the same exemption: a fixture that goes through the service would need a model
    call to produce a row.
    """
    from nautobot_event_tracker.models import AgentRun  # pylint: disable=import-outside-toplevel

    if ticket is None:
        ticket = create_ticket()
    run = AgentRun(ticket=ticket, **overrides)
    run.validated_save()
    return run


def create_agenttoolcall(run=None, tool=None, **overrides):
    """One tool call on a run, in `proposed` unless a test says otherwise.

    The binding is recorded by default, because `services.agent` records it on every call it
    writes: a row without one is a row that could not be executed, so a fixture that left it empty
    would be building a state production never produces. Pass `tool_fingerprint` explicitly to
    test a mismatch.
    """
    from nautobot_event_tracker.models import AgentToolCall  # pylint: disable=import-outside-toplevel
    from nautobot_event_tracker.services import mcp as mcp_service  # pylint: disable=import-outside-toplevel

    if run is None:
        run = create_agentrun()
    if tool is None:
        tool = create_mcptool()
    overrides.setdefault("tool_fingerprint", mcp_service.call_binding(tool))
    call = AgentToolCall(run=run, tool=tool, **overrides)
    call.validated_save()
    return call


class RefusalAssertions:  # pylint: disable=too-few-public-methods
    """Assert that something was refused, and that the message says why.

    Mixed into the tests for both halves of startup validation. The message is the whole point of
    the exercise - an operator reading it at 03:00 is the reason the validation exists - so every
    test asserts on it rather than on the exception's type alone.
    """

    def assert_names(self, message, fragments):
        """Assert the message names each of these faults."""
        for fragment in fragments:
            self.assertIn(fragment, message)  # pylint: disable=no-member
        return message
