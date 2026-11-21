"""The MCP service layer: what it reads off an integration, and what it refuses to call.

Phase 4B rules M1, M3, M4, M6, M7 and M8. Discovery is not tested here because it is not here:
the registry app owns it (ADR 0010), and rule M5's registry half is that app's configuration. M5's
call half - a tool that moved since a call was proposed against it - is tested with the other
refusals below.

Nothing here opens a socket: the client seam is the boundary, and no test mocks the SDK's
internals.
"""

import json
import pathlib
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from nautobot_ai_models.choices import MCPTransportChoices
from nautobot_ai_models.models import MCPTool

from nautobot_event_tracker.choices import AgentToolCallStatusChoices
from nautobot_event_tracker.models import AgentToolCall
from nautobot_event_tracker.services import mcp as mcp_service
from nautobot_event_tracker.services.exceptions import MCPCallError, MCPConfigurationError
from nautobot_event_tracker.tests import fixtures


def _recorded(name):
    """Return one recorded SR Linux tool result from the data directory."""
    return (pathlib.Path(__file__).parent / "data" / name).read_text(encoding="utf-8")


class MCPTestCase(TestCase):
    """A registered server for the connection tests to resolve."""

    def setUp(self):
        """Build the server every test here reaches."""
        self.server = fixtures.create_mcpserver()


class TestTheConnection(MCPTestCase):
    """M3: everything about reaching a server comes off its integration, at call time."""

    def test_the_remote_url_is_where_the_call_goes(self):
        """The endpoint is the integration's, never a setting."""
        connection = mcp_service.connection_for(self.server)
        self.assertEqual(connection.url, "https://mcp.example.test/mcp")

    def test_a_templated_url_is_rendered(self):
        """Nautobot supports Jinja2 there, so a raw read would send a literal `{{ ... }}`."""
        self.server.external_integration.remote_url = "https://{{ obj.name|lower|replace(' ', '-') }}.example.test/mcp"
        self.server.external_integration.save()

        self.assertEqual(
            mcp_service.connection_for(self.server).url,
            "https://test-mcp-server.example.test/mcp",
        )

    def test_a_template_that_does_not_render_is_a_configuration_error(self):
        """Half-rendering it would point the call at an address nobody chose."""
        self.server.external_integration.remote_url = "https://{{ unclosed.example.test/mcp"
        self.server.external_integration.save()

        with self.assertRaises(MCPConfigurationError):
            mcp_service.connection_for(self.server)

    def test_an_integration_with_no_url_is_refused(self):
        """`clean()` demands one, but a shared integration can be blanked afterwards."""
        self.server.external_integration.remote_url = ""
        self.server.external_integration.save()

        with self.assertRaises(MCPConfigurationError):
            mcp_service.connection_for(self.server)

    def test_the_integrations_headers_come_along(self):
        """An operator who set a header expects it sent."""
        self.server.external_integration.headers = {"X-Tenant": "noc"}
        self.server.external_integration.save()

        self.assertEqual(mcp_service.connection_for(self.server).headers["X-Tenant"], "noc")

    def test_a_secret_becomes_a_bearer_token(self):
        """The common case, and the one an operator should not have to write a header for."""
        with mock.patch("nautobot_event_tracker.services.mcp.read_secret", return_value="s3cret"):
            headers = mcp_service.connection_for(self.server).headers

        self.assertEqual(headers["Authorization"], "Bearer s3cret")

    def test_an_explicit_authorization_header_wins_over_the_secret(self):
        """An operator who wrote that header has said how this server is authenticated."""
        self.server.external_integration.headers = {"authorization": "ApiKey abc"}
        self.server.external_integration.save()

        with mock.patch("nautobot_event_tracker.services.mcp.read_secret", return_value="s3cret"):
            headers = mcp_service.connection_for(self.server).headers

        self.assertEqual(headers["authorization"], "ApiKey abc")
        self.assertNotIn("Authorization", headers)

    def test_unticking_verify_ssl_wins_over_a_ca_path(self):
        """Somebody who did both said not to verify, and quietly verifying anyway is the surprise."""
        self.server.external_integration.verify_ssl = False
        self.server.external_integration.ca_file_path = "/etc/ssl/private-ca.pem"
        self.server.external_integration.save()

        self.assertIs(mcp_service.connection_for(self.server).verify, False)

    def test_a_ca_path_is_used_when_verification_is_on(self):
        """An internal endpoint behind a private CA is the ordinary case, not an exotic one."""
        self.server.external_integration.ca_file_path = "/etc/ssl/private-ca.pem"
        self.server.external_integration.save()

        self.assertEqual(mcp_service.connection_for(self.server).verify, "/etc/ssl/private-ca.pem")

    def test_the_integration_timeout_applies(self):
        """M8 - there is no unbounded wait, and the integration is where the number lives."""
        self.server.external_integration.timeout = 45
        self.server.external_integration.save()

        self.assertEqual(mcp_service.connection_for(self.server).timeout, 45)

    def test_a_useless_timeout_falls_back_to_the_default(self):
        """A stored zero is not somebody asking for no limit."""
        self.server.external_integration.timeout = 0
        self.server.external_integration.save()

        self.assertEqual(mcp_service.connection_for(self.server).timeout, mcp_service.DEFAULT_TIMEOUT_SECONDS)


class TestTheClientSeam(TestCase):
    """M1: one import site, and a missing extra that says so."""

    def test_a_missing_extra_names_the_extra(self):
        """A deployment fault should read as one, not as an ImportError at startup."""
        with mock.patch.dict("sys.modules", {"mcp": None, "mcp.client.streamable_http": None}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                mcp_service.require_client()

        self.assertIn("mcp", str(caught.exception).lower())


class CallToolTestCase(TestCase):
    """A proposed call on an enabled tool, which is the state every rule here starts from."""

    def setUp(self):
        """One server, one enabled read-only tool, and one call asking for it."""
        self.server = fixtures.create_mcpserver()
        self.tool = fixtures.create_mcptool(server=self.server, enabled=True, writable=False)
        self.run = fixtures.create_agentrun()
        self.call = fixtures.create_agenttoolcall(run=self.run, tool=self.tool, arguments={"device": "leaf-01"})

    def call_tool(self, client=None, **kwargs):
        """Call the tool through the service with a fake wire underneath."""
        caller = client if client is not None else fixtures.FakeMCPCaller()
        record = mcp_service.call_tool(tool_call=self.call, client=caller, **kwargs)
        self.caller = caller  # pylint: disable=attribute-defined-outside-init
        return record


class TestCallingATool(CallToolTestCase):
    """M7 and M8: what reaches the wire, and what comes back onto the row."""

    def test_the_tool_name_and_arguments_go_over_the_wire(self):
        """The server's own name for the tool, not the name a model was offered."""
        self.call_tool()

        self.assertEqual(self.caller.calls[0]["name"], self.tool.name)
        self.assertEqual(self.caller.calls[0]["arguments"], {"device": "leaf-01"})

    def test_the_result_is_recorded_before_the_caller_sees_it(self):
        """M7 - the row carries the answer, and the row is what is handed back."""
        record = self.call_tool(client=fixtures.FakeMCPCaller(fixtures.FakeCallToolResult("ethernet-1/1 is down")))

        self.assertEqual(record.status, AgentToolCallStatusChoices.EXECUTED)
        self.assertEqual(record.result["text"], "ethernet-1/1 is down")
        self.assertEqual(record.error, "")
        self.assertIsNotNone(record.called_at)

    def test_structured_content_is_kept_when_a_server_sends_it(self):
        """A tool that answers in JSON is answering in JSON, and that is worth storing."""
        result = fixtures.FakeCallToolResult("ok", structured_content={"admin": "up"})

        record = self.call_tool(client=fixtures.FakeMCPCaller(result))

        self.assertEqual(record.result["structured_content"], {"admin": "up"})

    def test_a_server_reported_error_is_a_failed_row_rather_than_an_exception(self):
        """7.4 - the server was reached and had something to say, and it is the model's to read."""
        result = fixtures.FakeCallToolResult("no such interface", is_error=True)

        record = self.call_tool(client=fixtures.FakeMCPCaller(result))

        self.assertEqual(record.status, AgentToolCallStatusChoices.FAILED)
        self.assertIn("no such interface", record.error)

    def test_a_transport_failure_is_recorded_and_raised(self):
        """M7 first, then the exception: the row exists whatever the caller does next."""
        with self.assertRaises(MCPCallError):
            self.call_tool(client=fixtures.FakeMCPCaller(error=OSError("connection refused")))

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.FAILED)
        self.assertIn("connection refused", self.call.error)

    def test_no_sdk_exception_escapes(self):
        """The L4 arrangement, applied to the other kind of call this app makes."""
        with self.assertRaises(MCPCallError):
            self.call_tool(client=fixtures.FakeMCPCaller(error=ValueError("something SDK-shaped")))

    def test_a_huge_answer_is_capped(self):
        """M8 - forty megabytes of counters must not become a prompt, or a row."""
        result = fixtures.FakeCallToolResult("x" * 50_000)

        record = self.call_tool(client=fixtures.FakeMCPCaller(result), max_result_chars=100)

        self.assertTrue(record.result["truncated"])
        self.assertEqual(len(record.result["text"]), 100)

    def test_structured_content_is_dropped_only_when_capping_the_text_was_not_enough(self):
        """The text is what a model and a person both read, so it is capped first."""
        result = fixtures.FakeCallToolResult("short", structured_content={"blob": "y" * 50_000})

        record = self.call_tool(client=fixtures.FakeMCPCaller(result), max_result_chars=100)

        self.assertNotIn("structured_content", record.result)

    def test_a_non_text_content_block_is_named_rather_than_dropped(self):
        """A model reading the transcript can tell an image it cannot see from an empty answer."""
        result = fixtures.FakeCallToolResult(None)
        result.content = [object()]

        record = self.call_tool(client=fixtures.FakeMCPCaller(result))

        self.assertEqual(record.result["text"], "<content>")

    def test_the_timeout_reaches_the_connection(self):
        """M8 - there is no unbounded wait on this path either."""
        self.call_tool(timeout=7)

        self.assertEqual(self.caller.calls[0]["connection"].timeout, 7)

    def test_the_integration_s_timeout_applies_when_the_caller_names_none(self):
        """The operator's number, where the operator set it."""
        self.call_tool()

        self.assertEqual(self.caller.calls[0]["connection"].timeout, self.server.external_integration.timeout)


class TestShorteningAResult(CallToolTestCase):
    """M8: separator runs and the cell spaces of `|` rows are made short before the cap."""

    def stored(self, text, **kwargs):
        """Return the stored result for a call that answers with this text."""
        structured = kwargs.pop("structured_content", None)
        result = fixtures.FakeCallToolResult(text, structured_content=structured)
        return self.call_tool(client=fixtures.FakeMCPCaller(result), **kwargs).result

    def test_a_long_separator_run_becomes_three_characters(self):
        """A run of ten or more of one separator character shrinks and a shorter run does not."""
        lines = ["=" * 10, "-" * 10, "+" * 10, "-" * 9, "-----BEGIN CERTIFICATE-----", "--flag"]

        result = self.stored("\n".join(lines))

        self.assertEqual(
            result["text"].split("\n"),
            ["===", "---", "+++", "-" * 9, "-----BEGIN CERTIFICATE-----", "--flag"],
        )

    def test_the_cell_spaces_of_a_bar_row_become_one_space(self):
        """Spaces after text in a row that starts with a bar shrink. The indent and other lines stay."""
        row = "  | a" + " " * 6 + "|" + " " * 10 + "| c" + " " * 4 + "|" + " " * 3
        other = "Gi0/1" + " " * 8 + "unassigned"

        result = self.stored(f"{row}\n{other}")

        self.assertEqual(result["text"], f"  | a | | c | \n{other}")

    def test_the_recorded_table_shows_more_rows_inside_a_small_limit(self):
        """With a limit of 1,000 the shortened table reaches a row that the raw text never reaches."""
        raw = _recorded("srlinux_interface_table.txt")
        self.assertEqual(len(raw), 8000)

        result = self.stored(raw, max_result_chars=1000)

        self.assertNotIn("truncated", result)
        self.assertEqual(len(result["text"]), 688)
        self.assertIn("ethernet-1/13", result["text"])
        self.assertNotIn("ethernet-1/1", raw[:1000])

    def test_the_recorded_detail_fits_the_default_limit_after_shortening(self):
        """The first 8,000 characters of an interface detail shrink to 1,727."""
        raw = _recorded("srlinux_interface_detail.txt")
        self.assertEqual(len(raw), 8000)

        result = self.stored(raw)

        self.assertEqual(len(result["text"]), 1727)
        self.assertNotIn("truncated", result)
        self.assertTrue(result["separators_shortened"])

    def test_a_result_still_over_the_limit_is_cut_from_the_shortened_text(self):
        """The cap applies to the shortened text and the result says that the app cut it."""
        raw = _recorded("srlinux_interface_detail.txt")

        result = self.stored(raw, max_result_chars=500)

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), 500)
        self.assertEqual(result["text"], mcp_service._shortened(raw)[:500])  # pylint: disable=protected-access
        self.assertTrue(result["text"].startswith("===\nInterface: ethernet-1/2"))

    def test_the_marker_is_set_when_the_text_changed(self):
        """Changed text carries the key."""
        self.assertTrue(self.stored("x" + "-" * 10)["separators_shortened"])

    def test_the_marker_is_absent_when_the_text_did_not_change(self):
        """Unchanged text has no key."""
        self.assertNotIn("separators_shortened", self.stored("ok"))

    def test_the_marker_is_absent_for_a_non_text_block(self):
        """The app does not change a named content block."""
        result = fixtures.FakeCallToolResult(None)
        result.content = [object()]

        stored = self.call_tool(client=fixtures.FakeMCPCaller(result)).result

        self.assertEqual(stored["text"], "<content>")
        self.assertNotIn("separators_shortened", stored)

    def test_the_marker_is_absent_for_an_empty_text(self):
        """The app does not change an empty text."""
        self.assertNotIn("separators_shortened", self.stored(""))

    def test_the_marker_does_not_cause_a_cut(self):
        """A result that fits only after shortening has no truncated key, even with the marker added."""
        limit = len(json.dumps({"is_error": False, "text": "a---"}))

        result = self.stored("a" + "-" * 10, max_result_chars=limit)

        self.assertNotIn("truncated", result)
        self.assertEqual(result["text"], "a---")
        self.assertTrue(result["separators_shortened"])

    def test_the_marker_does_not_drop_structured_content(self):
        """The marker key is outside the size check that drops structured content."""
        limit = len(json.dumps({"is_error": False, "text": "a---", "structured_content": {"k": "v"}}))

        result = self.stored("a" + "-" * 10, structured_content={"k": "v"}, max_result_chars=limit)

        self.assertNotIn("truncated", result)
        self.assertEqual(result["structured_content"], {"k": "v"})
        self.assertTrue(result["separators_shortened"])

    def test_an_error_text_is_shortened_too(self):
        """The text of a server error gets the same rules."""
        raw = fixtures.FakeCallToolResult("bad " + "-" * 40, is_error=True)

        record = self.call_tool(client=fixtures.FakeMCPCaller(raw))

        self.assertEqual(record.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(record.error, "bad ---")

    def test_structured_content_is_not_shortened(self):
        """Structured content is data and keeps its text."""
        structured = {"line": "-" * 50}

        result = self.stored("ok", structured_content=structured)

        self.assertEqual(result["structured_content"], structured)
        self.assertNotIn("separators_shortened", result)

    def test_windows_line_ends_stay(self):
        """Each carriage return stays on its line."""
        raw = "| a" + " " * 4 + "| b" + " " * 4 + "|\r\n" + "-" * 12 + "\r\n"

        result = self.stored(raw)

        self.assertEqual(result["text"], "| a | b |\r\n---\r\n")


class TestCallRefusals(CallToolTestCase):
    """M4 and M6: everything refused before any network I/O, and recorded when it is."""

    def assert_refused(self, fragment, client=None):
        """Assert the call was refused, said why, and never reached the wire."""
        caller = client if client is not None else fixtures.FakeMCPCaller()
        with self.assertRaises(MCPConfigurationError) as caught:
            mcp_service.call_tool(tool_call=self.call, client=caller)

        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(caller.calls, [])
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.FAILED)
        self.assertIn(fragment, self.call.error)

    def test_a_disabled_tool_is_refused(self):
        """M4 - the allowlist is a table, and it is read here as well as when tools are offered."""
        self.tool.enabled = False
        self.tool.validated_save()

        self.assert_refused("is not enabled")

    def test_a_tool_on_a_disabled_server_is_refused(self):
        """The server-level off switch works everywhere at once."""
        self.server.enabled = False
        self.server.validated_save()

        self.assert_refused("is disabled")

    def test_a_server_on_another_transport_is_refused(self):
        """M2, checked rather than assumed.

        The registry accepts `sse` and `stdio` so a server this app cannot reach is still
        catalogued. Attempting one would fail somewhere inside the transport with a message about
        the protocol; this refuses first and names the field.
        """
        self.server.transport = MCPTransportChoices.TYPE_STDIO
        self.server.validated_save()

        self.assert_refused("streamable HTTP only")

    def test_an_unapproved_writable_tool_is_refused(self):
        """M6 - the one rule this whole phase is built around."""
        self.tool.writable = True
        self.tool.validated_save()

        self.assert_refused("not approved")

    def test_an_approved_writable_tool_runs(self):
        """The other half of M6, or the gate would just be a wall."""
        self.tool.writable = True
        self.tool.validated_save()
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, status=AgentToolCallStatusChoices.APPROVED
        )

        record = self.call_tool()

        self.assertEqual(record.status, AgentToolCallStatusChoices.EXECUTED)
        self.assertEqual(len(self.caller.calls), 1)

    def test_a_call_that_already_ran_is_not_run_again(self):
        """An executed row is a decision that has happened; re-running it is a second call."""
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, status=AgentToolCallStatusChoices.EXECUTED
        )

        self.assert_refused("already been decided")

    def test_a_denied_call_is_not_run(self):
        """13.8 - denying is an answer, not a pause."""
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, status=AgentToolCallStatusChoices.DENIED
        )

        self.assert_refused("already been decided")

    def test_a_tool_re_advertised_since_the_proposal_is_refused(self):
        """M6's second half: what was approved was a call on the tool as it read then."""
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, tool_fingerprint="the binding when this was proposed"
        )
        self.tool.definition_fingerprint = "a different digest"
        self.tool.validated_save()

        self.assert_refused("has changed since this call was proposed")

    def test_a_matching_fingerprint_passes(self):
        """The check is a comparison, not a bar on every call that carries a digest."""
        self.tool.definition_fingerprint = "same"
        self.tool.validated_save()
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, tool_fingerprint=mcp_service.call_binding(self.tool)
        )

        self.assertEqual(self.call_tool().status, AgentToolCallStatusChoices.EXECUTED)

    def test_a_call_is_claimed_so_two_callers_cannot_both_make_it(self):
        """One approval must not become two commands against a device.

        The check and the call were separate steps, so two resumptions opened together could both
        pass the status check before either wrote a result. The claim is a conditional UPDATE, so
        exactly one caller wins it.
        """
        # Read before the first call, so this instance genuinely holds the pre-call status - which
        # is exactly what a second resumption racing the first would be holding. Hand-setting the
        # field would also trip the status-assignment guard, and rightly.
        stale = AgentToolCall.objects.get(pk=self.call.pk)

        first = fixtures.FakeMCPCaller()
        mcp_service.call_tool(tool_call=self.call, client=first)

        second = fixtures.FakeMCPCaller()

        with self.assertRaises(MCPConfigurationError) as caught:
            mcp_service.call_tool(tool_call=stale, client=second)

        self.assertIn("already taken", str(caught.exception))
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(second.calls, [])

    def test_a_failure_resolving_the_connection_is_recorded(self):
        """M7 - every call is on the record, including the ones that never left the process.

        `connection_for()` and the client resolution sat outside the recorded region, so an
        integration blanked of its URL left the row untouched and the agent was told the call
        "returned nothing" rather than that the server was misconfigured.
        """
        integration = self.server.external_integration
        integration.remote_url = ""
        integration.save()

        with self.assertRaises(MCPConfigurationError):
            mcp_service.call_tool(tool_call=self.call, client=fixtures.FakeMCPCaller())

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.FAILED)
        self.assertIn("no remote URL", self.call.error)

    def test_renaming_a_tool_after_approval_is_refused(self):
        """The name is the string put on the wire, and `change_mcptool` is enough to edit it.

        A lower bar than `approve_agenttoolcall`: rename the row between approval and execution and
        the approved arguments would go to a different command, with the definition digest
        untouched because a rename does not re-run discovery.
        """
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, tool_fingerprint=mcp_service.call_binding(self.tool)
        )
        self.tool.name = "something_else"
        self.tool.validated_save()

        self.assert_refused("has changed since this call was proposed")

    def test_repointing_the_server_after_approval_is_refused(self):
        """The endpoint an approved call is dialled at is part of what was approved."""
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, tool_fingerprint=mcp_service.call_binding(self.tool)
        )
        self.server.external_integration = fixtures.create_external_integration(
            name="Somewhere Else", remote_url="https://elsewhere.example.test/mcp"
        )
        self.server.validated_save()

        self.assert_refused("has changed since this call was proposed")

    def test_a_tool_that_gains_a_fingerprint_after_the_proposal_is_refused(self):
        """The empty-digest case, which used to skip the check entirely.

        A tool created by hand rather than by discovery carries no fingerprint, so every proposal
        against it stored an empty one - and the guard `if tool_call.tool_fingerprint and ...`
        then skipped the comparison forever, including after discovery wrote a real digest and an
        operator re-enabled the tool. That is exactly the sequence M6 exists to catch.
        """
        # A row written before bindings were recorded, or by anything that skipped it.
        self.call = fixtures.create_agenttoolcall(run=self.run, tool=self.tool, tool_fingerprint="")
        self.tool.definition_fingerprint = "a digest discovery wrote afterwards"
        self.tool.validated_save()

        with self.assertRaises(MCPConfigurationError) as caught:
            mcp_service.call_tool(tool_call=self.call, client=fixtures.FakeMCPCaller())

        self.assertIn("has changed since this call was proposed", str(caught.exception))

    def test_the_tool_is_re_read_rather_than_trusted_as_handed_over(self):
        """The stale instance a caller holds would notice none of the events above."""
        MCPTool.objects.filter(pk=self.tool.pk).update(enabled=False)
        # `self.tool` still says enabled, and so does the instance the call points at.

        with self.assertRaises(MCPConfigurationError):
            mcp_service.call_tool(tool_call=self.call, client=fixtures.FakeMCPCaller())
