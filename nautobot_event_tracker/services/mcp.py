"""The MCP service layer: the one module in this app that speaks MCP.

Everything an agent reaches outside Nautobot goes through here, so that the transport decision and
the allowlist are made once rather than at each call site (ADR 0007).

Rules implemented here, referenced by number from the Phase 4B spec:

* **M1** - the MCP client library is imported only in this module, lazily, behind the optional
  `mcp` extra. A deployment without it gets a plain `ImproperlyConfigured` naming the extra.
* **M2** - streamable HTTP and no other transport. There is no setting for this, because a setting
  is how stdio comes back; nothing in this app imports `subprocess`, and a guard says so. The AI
  Models app records `sse` and `stdio` as transports a server may be registered with; a call
  against either is refused here rather than attempted.
* **M3** - the endpoint, its headers, its TLS settings and its timeout come from the server's
  ExternalIntegration at call time, and the credential from that integration's secrets group.
  Nothing key-shaped lives in settings, on a model, or in a log line.
* **M4** - a tool that is not enabled, on a server that is not enabled, is refused before any
  network I/O, whatever the prompt, the model or the caller said.
* **M5** - discovery never grants, and discovery is no longer this app's (ADR 0010). The rule now
  has two halves in two places. The registry half is configuration on the AI Models app:
  `new_tools_enabled: False` so a newly advertised tool arrives disabled, and
  `disable_on_definition_change: True` so one whose definition moved is taken back out of service.
  Both are documented in `docs/admin/mcp.md` and neither is this app's to enforce. The call half
  is here and is unconditional: a tool whose definition, name or endpoint has moved since a call
  was proposed against it is refused, whatever the registry currently says.
* **M6** - a writable tool runs only from an approved `AgentToolCall`, only against the tool
  definition that call was approved against, and only once: the row is claimed atomically before
  the call, so two callers cannot both make it.
* **M7** - every call is on the record - arguments, result, latency, error - before its caller
  sees any of it. Rule L1's promise, made about the other kind of call this app makes. A call that
  the soft time limit of the Job interrupts is recorded as failed before the error goes on. A call
  of a writable tool that was sent and has no answer says that the change might be in effect.
* **M8** - every call is bounded in both directions. Each step has a timeout, the call has one total
  deadline of two timeouts, and the result has a cap. Long separator runs and the cell spaces of `|`
  rows are made short before the cap.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.utils import timezone
from nautobot.apps.choices import SecretsGroupSecretTypeChoices
from nautobot_ai_models.choices import MCPTransportChoices
from nautobot_ai_models.models import MCPTool

from nautobot_event_tracker.choices import AgentToolCallStatusChoices
from nautobot_event_tracker.models import AgentToolCall
from nautobot_event_tracker.secrets import read_secret
from nautobot_event_tracker.services.exceptions import (
    MCPCallError,
    MCPConfigurationError,
    MCPDeadlineError,
    MCPError,
    is_soft_time_limit,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
"""The timeout of a call when the integration gives none (M8)."""

SOFT_TIME_LIMIT_CALL_ERROR = "The Job reached its soft time limit before this call finished."
"""The error of a read-only call that the soft time limit of the Job interrupted."""

CALL_DEADLINE_TIMEOUTS = 2
"""Timeouts that one call can take in total (M8)."""

DEADLINE_CALL_ERROR = "The call did not finish in {seconds} seconds."
"""The error of a call that its total deadline stopped."""

CHANGE_MIGHT_BE_IN_EFFECT = (
    "This tool changes the network, so the change might be in effect. "
    "Check the device before you approve the same change again."
)
"""The warning of a call of a writable tool that was sent and has no answer."""

CUT_CHANGE_ERROR = f"The call was interrupted before the app got an answer. {CHANGE_MIGHT_BE_IN_EFFECT}"
"""The error of a call of a writable tool that a stop interrupted."""


def call_deadline(timeout):
    """Return the total deadline, in seconds, of a call with this timeout."""
    return CALL_DEADLINE_TIMEOUTS * timeout

DEAD_PROCESS_CALL_ERROR = "The process that ran this agent stopped before this call finished."
"""The error of a read-only call that a dead process left unfinished."""

SEPARATOR_RUN = re.compile(r"([=+-])\1{9,}")
"""Ten or more of one separator character in a row (M8)."""

SHORT_SEPARATOR_LENGTH = 3

TABLE_ROW_SPACE_RUN = re.compile(r"(?<=\S)[ \t]{2,}")
"""Two or more spaces or tabs after a character that is not whitespace (M8)."""

SSE_READ_TIMEOUT_SECONDS = 300
"""The read timeout of the HTTP client. The stream stays open, so it is not the timeout of a request."""

CALLABLE_TRANSPORTS = (MCPTransportChoices.TYPE_STREAMABLE_HTTP,)
"""The transports that this app calls (M2).

The AI Models app also records `sse` and `stdio`; this app refuses a call to them.
"""

#: How the credential is presented when the integration's own headers do not present it. Bearer is
#: what MCP servers over HTTP overwhelmingly expect; an operator who needs something else writes
#: that header on the integration, and this defers to them.
AUTHORIZATION_HEADER = "Authorization"

#: How much of a tool's answer is kept, when the caller does not say (M8). The agent passes its own
#: `max_tool_result_chars`; this is what everything else gets, and what stops a tool that returns
#: forty megabytes of interface counters becoming a row as well as a prompt.
DEFAULT_MAX_RESULT_CHARS = 8000

#: Error text longer than this is truncated before it is recorded, exactly as the LLM service caps
#: its own. The row exists to show that and why a call failed, not to archive a stack trace.
ERROR_TEXT_CAP = 1000


@dataclass(frozen=True)
class MCPConnection:
    """Everything needed to reach one server, resolved from its integration (M3).

    `verify` carries httpx's own convention rather than a pair of fields: True, False, or the path
    to a CA bundle. The broker layer's `BrokerConnection` keeps the two apart because librdkafka
    and redis-py each want them differently; here there is one client and one shape.
    """

    url: str
    headers: dict = field(default_factory=dict)
    verify: object = True
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def deadline(self):
        """Return the total deadline of one call, in seconds."""
        return call_deadline(self.timeout)

    def __repr__(self):
        """Say everything except the header values, one of which is usually the credential.

        A dataclass renders its whole contents by default, and this one is passed around, held in
        tracebacks and - with `DEBUG` on - rendered onto an error page. The header *names* are
        worth seeing when something is misconfigured; their values never are.
        """
        headers = ", ".join(sorted(self.headers))
        return f"MCPConnection(url={self.url!r}, headers=[{headers}], verify={self.verify!r}, timeout={self.timeout!r})"


def require_client():
    """Resolve the client now, so a missing `mcp` extra is a refusal rather than a crash later.

    The same reason `services.llm.require_client()` exists: the missing-extra failure is an
    `ImproperlyConfigured`, deliberately outside the `MCPError` family that callers handle, so
    nothing on an agent's path would catch it.
    """
    _default_client()


def connection_for(server):
    """M3 - everything the server's ExternalIntegration says about reaching it, read now.

    The credential becomes an `Authorization: Bearer` header unless the integration's own headers
    already carry an Authorization, in which case the operator has said how this server is
    authenticated and this defers to them.
    """
    integration = server.external_integration

    url = _rendered(integration, "render_remote_url", server)
    if not url:
        # `clean()` demands a URL at save time, but an integration is a shared object and can be
        # blanked afterwards without revalidating what points at it.
        raise MCPConfigurationError(f"MCP server '{server}' has an external integration with no remote URL.")

    headers = dict(_rendered(integration, "render_headers", server) or {})
    if not any(key.lower() == AUTHORIZATION_HEADER.lower() for key in headers):
        for secret_type in (SecretsGroupSecretTypeChoices.TYPE_TOKEN, SecretsGroupSecretTypeChoices.TYPE_SECRET):
            token = read_secret(integration, secret_type)
            if token:
                headers[AUTHORIZATION_HEADER] = f"Bearer {token}"
                break

    # Unticking *Verify SSL* wins over a CA path, the same way it does for a model call: an
    # operator who has done both has said not to verify, and quietly verifying anyway is the
    # surprise this rule exists to prevent.
    if not integration.verify_ssl:
        verify = False
    elif integration.ca_file_path:
        verify = integration.ca_file_path
    else:
        verify = True

    timeout = getattr(integration, "timeout", None)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS

    return MCPConnection(url=url, headers=headers, verify=verify, timeout=timeout)


def call_tool(*, tool_call, timeout=None, max_result_chars=None, client=None):
    """Call one tool for one `AgentToolCall`, refusing everything the allowlist and the gate refuse.

    The row is the argument rather than a tool and a dictionary, because every rule this function
    enforces is written on the row: M4 reads the tool it points at, M6 reads its status and the
    definition it was approved against, and M7 writes the outcome back onto it before the caller
    sees anything at all.

    `client` is the test seam - an object with `call_tool(connection, name, arguments)` - and
    nothing outside a test supplies one. No test opens a socket.

    Returns the updated `AgentToolCall`, `executed` or `failed`. A server that answers with an
    error of its own is a `failed` row and not an exception: the server was reached and had
    something to say, and what it said is the model's to read (section 7.4). Raises
    `MCPConfigurationError` when the call is refused before any network I/O, and `MCPCallError`
    when it left the process and did not come back usable. A call that does not finish before its total
    deadline is recorded as failed, then raised as `MCPDeadlineError`. The soft time limit of the Job is raised
    again as itself, after the row is written (M7).
    """
    cap = int(max_result_chars) if max_result_chars else DEFAULT_MAX_RESULT_CHARS

    # Re-read rather than trusting the instance the caller is holding. Between a proposal and its
    # execution a person disables the tool, an operator switches the server off, or discovery
    # rewrites the definition - and those three are precisely the events the checks below exist to
    # notice. A cached row would notice none of them.
    tool = MCPTool.objects.select_related("mcp_server__external_integration__secrets_group").get(pk=tool_call.tool_id)
    _check_callable(tool_call, tool)
    _claim(tool_call, tool)

    started = time.monotonic()
    try:
        # Inside the recorded region, all of it. Resolving the connection can fail on its own -
        # an integration blanked of its URL, a Jinja2 field that will not render - and so can
        # resolving the client, when the `mcp` extra is not installed. Left outside, those two
        # returned to the caller with nothing written on the row: M7 broken, the row stranded in
        # whatever state the claim left it, and the agent told "the call returned nothing" when
        # the truth was "this server is misconfigured".
        connection = connection_for(tool.mcp_server)
        if timeout:
            connection = replace(connection, timeout=float(timeout))
        caller = client if client is not None else _default_client()
        raw = caller.call_tool(connection, tool.name, dict(tool_call.arguments or {}))
    except Exception as error:  # pylint: disable=broad-except
        if is_soft_time_limit(error):
            _finish(
                tool_call,
                latency_ms=_elapsed_ms(started),
                error=CUT_CHANGE_ERROR if tool.writable else SOFT_TIME_LIMIT_CALL_ERROR,
            )
            raise
        # M7 - on the record first, then the caller hears about it.
        _finish(tool_call, latency_ms=_elapsed_ms(started), error=_failure_text(error, tool))
        # The family the caller expects is preserved: a configuration fault stays one, and a
        # missing extra stays the deployment fault it is (deliberately outside `MCPError`, so
        # nothing on the agent's path swallows it as a bad tool call).
        if isinstance(error, (MCPError, ImproperlyConfigured)):
            raise
        raise MCPCallError(f"Calling '{tool}' failed: {error}") from error

    result, truncated = _capped_result(raw, cap)
    if truncated:
        logger.info("Truncated the result of %s to %d characters", tool, cap)

    error = None
    if result.get("is_error"):
        error = result.get("text") or "The server reported an error and said nothing about it."
    _finish(tool_call, latency_ms=_elapsed_ms(started), result=result, error=error)
    return tool_call


def _failure_text(error, tool):
    """Return the row text of a failed call, with the warning when a writable tool was sent and has no answer."""
    text = str(error) if isinstance(error, MCPDeadlineError) else f"The call failed: {error}"
    if tool.writable and getattr(error, "call_sent", False):
        room = ERROR_TEXT_CAP - len(CHANGE_MIGHT_BE_IN_EFFECT) - 1
        text = f"{text[:room]} {CHANGE_MIGHT_BE_IN_EFFECT}"
    return text


def fail_unfinished_calls(run_ids, reason):
    """Mark the unfinished calls of these runs as failed, and return the rows that changed.

    A call is unfinished when it is `executing`, or when it is `proposed` for a tool that is not
    writable. A call of a writable tool gets `CUT_CHANGE_ERROR`; the others get `reason`. A
    `proposed` call of a writable tool is a proposal for a person, so it stays.
    """
    unfinished = AgentToolCall.objects.filter(run_id__in=run_ids).filter(
        Q(status=AgentToolCallStatusChoices.EXECUTING)
        | Q(status=AgentToolCallStatusChoices.PROPOSED, tool__writable=False)
    )
    changed = list(unfinished.values_list("pk", flat=True))
    now = timezone.now()
    for writable, error in ((True, CUT_CHANGE_ERROR), (False, reason)):
        AgentToolCall.objects.filter(pk__in=changed, tool__writable=writable).update(
            status=AgentToolCallStatusChoices.FAILED, error=str(error)[:ERROR_TEXT_CAP], called_at=now
        )
    return list(AgentToolCall.objects.filter(pk__in=changed).select_related("tool__mcp_server"))


def _claim(tool_call, tool):
    """Take this call, atomically, so that exactly one caller can make it.

    `_check_callable` reads; this writes, and the two have to be one step. Checking and then
    calling leaves a window in which two callers both pass the check and both reach the server -
    and for a writable tool that means a change a person approved once being applied twice, which
    is not the same thing as applying it once. `services/agent.py` serializes launches per ticket
    as well; this is the check in the layer that owns the rule, and it is the one that holds if
    anything ever calls `call_tool()` from somewhere else.

    A conditional UPDATE rather than a lock held across the call: the call is network I/O and may
    take a tool timeout to return, and no row lock should be open that long. The status is the
    claim, so a row left in `executing` is a process that died mid-call.
    """
    claimed = (
        AgentToolCall.objects.filter(pk=tool_call.pk, status=tool_call.status)
        .exclude(status=AgentToolCallStatusChoices.EXECUTING)
        .update(status=AgentToolCallStatusChoices.EXECUTING)
    )
    if not claimed:
        # Somebody else moved the row between the check above and this line. Deliberately not
        # written to the row: whoever holds the claim owns its outcome, and recording a refusal
        # here would overwrite the result of the call that is actually running.
        raise MCPConfigurationError(f"This call on '{tool}' was already taken by another caller and is not run twice.")
    tool_call.status = AgentToolCallStatusChoices.EXECUTING


def _check_callable(tool_call, tool):
    """M4 and M6, in that order, before any network I/O and whatever anything else said.

    The order matters to the message an operator reads: a disabled tool is the answer even when
    the call is also unapproved, because enabling it is what they have to do first. A writable tool runs from an
    approved call only, and a call that has a decision does not run again (13.4). The fingerprint check of M6 has no
    condition, because the registry policy that disables a changed tool is an optional setting of another app.
    """
    if not tool.enabled:
        _refuse(tool_call, f"Tool '{tool}' is not enabled.")
    if not tool.mcp_server.enabled:
        _refuse(tool_call, f"MCP server '{tool.mcp_server}' is disabled.")

    if tool.mcp_server.transport not in CALLABLE_TRANSPORTS:
        _refuse(
            tool_call,
            f"MCP server '{tool.mcp_server}' uses the '{tool.mcp_server.transport}' transport. "
            "This app calls over streamable HTTP only (ADR 0007).",
        )

    allowed = (
        (AgentToolCallStatusChoices.APPROVED,)
        if tool.writable
        else (AgentToolCallStatusChoices.PROPOSED, AgentToolCallStatusChoices.APPROVED)
    )
    if tool_call.status not in allowed:
        _refuse(
            tool_call,
            f"Tool '{tool}' is writable and this call is '{tool_call.status}', not approved."
            if tool.writable
            else f"This call is '{tool_call.status}' and has already been decided.",
        )

    # Compared unconditionally. Guarding on `tool_call.tool_fingerprint` being truthy skipped the
    # check entirely for a tool created by hand through the form or the API, which carries no
    # fingerprint - and kept skipping it after discovery later wrote a real one, which is exactly
    # the drift this check exists to catch.
    if tool_call.tool_fingerprint != call_binding(tool):
        _refuse(
            tool_call,
            f"'{tool}' has changed since this call was proposed - its definition, its name or the "
            "endpoint it is reached at. Approving approves the tool as it read then; propose it "
            "again against what it is now.",
        )


def _refuse(tool_call, message):
    """Record a refusal on the row and raise it. Nothing has left the process."""
    _finish(tool_call, latency_ms=0, error=message)
    raise MCPConfigurationError(message)


def _finish(tool_call, *, latency_ms, result=None, error=None):
    """M7 - write the outcome onto the row. The one place a call's result is recorded.

    No `validated_save()`: every value here is service-constructed or capped on the line above,
    and the alternative costs two FK queries on a path that has just made a network call.
    """
    tool_call.status = AgentToolCallStatusChoices.FAILED if error is not None else AgentToolCallStatusChoices.EXECUTED
    tool_call.result = result or {}
    tool_call.error = str(error)[:ERROR_TEXT_CAP] if error is not None else ""
    tool_call.latency_ms = latency_ms
    tool_call.called_at = timezone.now()
    tool_call.save()
    return tool_call


def _shortened(text):
    """Return the text with long separator runs and the cell spaces of `|` rows made short (M8)."""
    text = SEPARATOR_RUN.sub(lambda run: run.group(1) * SHORT_SEPARATOR_LENGTH, text)
    return "\n".join(
        TABLE_ROW_SPACE_RUN.sub(" ", line) if line.lstrip().startswith("|") else line for line in text.split("\n")
    )


def _capped_result(raw, cap):
    """M8 - what came back, rendered as plain JSON and bounded. True when something was dropped.

    Rendered rather than stored: the SDK's content blocks are pydantic models, which a JSONField
    cannot hold and a prompt cannot use. Text blocks become text and everything else becomes a
    marker naming its type, so a model reading the transcript can tell an image it cannot see from
    an answer that was empty.

    The text is capped first, because it is what a model and a person both read. Structured content
    is dropped only when capping the text was not enough, which is the case where a tool answered
    with a megabyte of JSON and the alternative to dropping it is storing it.

    The function shortens the text before it applies the cap. It sets the key `separators_shortened`
    after every size check, so the key never causes a cut.
    """
    blocks = []
    for block in getattr(raw, "content", None) or []:
        text = getattr(block, "text", None)
        blocks.append(text if isinstance(text, str) else f"<{getattr(block, 'type', None) or 'content'}>")
    original = "\n".join(blocks)
    text = _shortened(original)

    result = {"is_error": bool(getattr(raw, "is_error", False)), "text": text}
    structured = getattr(raw, "structured_content", None)
    if structured is not None:
        result["structured_content"] = structured

    shortened = text != original

    if len(json.dumps(result, default=str)) <= cap:
        if shortened:
            result["separators_shortened"] = True
        return result, False

    result["text"] = text[:cap]
    result["truncated"] = True
    if len(json.dumps(result, default=str)) > cap:
        result.pop("structured_content", None)
    if shortened:
        result["separators_shortened"] = True
    return result, True


def _elapsed_ms(started):
    """Whole milliseconds since `started`."""
    return int((time.monotonic() - started) * 1000)


def call_binding(tool):
    """What an approval is bound to: the definition, the name, and the endpoint.

    `definition_fingerprint` answers "did the server change what this tool is", which is discovery's
    question. This answers the approver's: "will the call I approved go where I thought it would".
    Three things decide that, and the digest alone covers one.

    The name is the string put on the wire, and the AI Models app's own form lets anyone with
    `nautobot_ai_models.change_mcptool` edit it - a lower bar than `approve_agenttoolcall`, and now
    in a different app's permission set. Rename the row between approval and execution and the
    approved arguments go to a different command with the digest untouched, because a rename does
    not re-run discovery. The server's integration decides which host is dialled, and repointing it
    sends an approved call somewhere else entirely.

    Recomputed from the tool's current state at execution and compared with what was stored at
    proposal, so any of the three changing is a refusal.
    """
    canonical = json.dumps(
        {
            "definition": tool.definition_fingerprint,
            "name": tool.name,
            "integration": str(tool.mcp_server.external_integration_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rendered(integration, method_name, server):
    """One of the integration's Jinja2-templated fields, rendered rather than read raw."""
    try:
        return getattr(integration, method_name)({"obj": server})
    except Exception as error:  # pylint: disable=broad-except
        raise MCPConfigurationError(
            f"External integration '{integration}' has a template that does not render: {error}"
        ) from error


class _StreamableHTTPClient:  # pylint: disable=too-few-public-methods
    """The real client: one MCP session per call, over streamable HTTP and nothing else (M2).

    A session per call rather than a pooled one. A tool call happens from a Job that does not
    outlive its own run, and a cached session would have to be invalidated on every registry edit
    to keep rule M4 honest.

    The SDK is asynchronous and every caller here is not, so each operation is one `asyncio.run`.
    That is correct in a WSGI request and in a Celery worker, and it is wrong inside a running
    event loop - which this app has none of, and which the error below explains if that changes.

    One cancel scope holds the whole session, so a slow step cannot take the call past its deadline.
    An answer that the app already has is kept when only the close is cut.
    """

    def __init__(
        self, session_class, transport, http_client_class, timeout_class, move_on_after
    ):  # pylint: disable=too-many-arguments,too-many-positional-arguments
        """Hold the pieces of the SDK, of its HTTP client and of anyio that this module uses."""
        self._session_class = session_class
        self._transport = transport
        self._http_client_class = http_client_class
        self._timeout_class = timeout_class
        self._move_on_after = move_on_after

    def call_tool(self, connection, name, arguments):
        """Call one tool and hand back what the server said, unread.

        Nothing is interpreted here: the refusals happened before this was reached, and the
        rendering happens after it. This is the wire and nothing else.
        """

        async def _call(session):
            return await session.call_tool(name, arguments, read_timeout_seconds=connection.timeout)

        return self._run(connection, _call)

    def _run(self, connection, operation):
        """Open a session, do one thing, close it, all inside the total deadline of the call."""
        sent = []

        async def _once():
            answer = []
            late_error = False
            with self._move_on_after(connection.deadline) as scope:
                try:
                    async with self._http_client_class(
                        headers=connection.headers,
                        verify=connection.verify,
                        # Not a flat timeout. The SDK's own client factory documents why: the read side of
                        # a streamable HTTP session is a long-lived SSE stream, and a 30-second read
                        # deadline cuts it mid-answer. The other three phases keep the integration's
                        # number, which is what an operator set it for.
                        timeout=self._timeout_class(
                            connect=connection.timeout,
                            write=connection.timeout,
                            pool=connection.timeout,
                            read=max(connection.timeout, SSE_READ_TIMEOUT_SECONDS),
                        ),
                        # An endpoint that redirects `/mcp` to `/mcp/` is ordinary, and without this the
                        # session simply fails. The SDK's factory sets it for the same reason.
                        follow_redirects=True,
                    ) as http_client:
                        async with self._transport(connection.url, http_client=http_client) as (read, write):
                            async with self._session_class(
                                read, write, read_timeout_seconds=connection.timeout
                            ) as session:
                                await session.initialize()
                                sent.append(True)
                                answer.append(await operation(session))
                except Exception as error:  # pylint: disable=broad-except
                    if not scope.cancel_called or is_soft_time_limit(error):
                        raise
                    late_error = True
                    logger.info("The MCP session at %s raised an error after its deadline: %s", connection.url, error)
            return answer, scope.cancelled_caught or late_error

        try:
            answer, cut = asyncio.run(_once())
        except Exception as error:  # pylint: disable=broad-except
            if isinstance(error, RuntimeError) and "running event loop" in str(error):
                raise MCPCallError(
                    "MCP calls are made from synchronous code and cannot run inside an event loop."
                ) from error
            if sent and not is_soft_time_limit(error):
                raise MCPCallError(str(error), call_sent=True) from error
            raise

        seconds = f"{connection.deadline:g}"
        if not answer:
            raise MCPDeadlineError(DEADLINE_CALL_ERROR.format(seconds=seconds), call_sent=bool(sent))
        if cut:
            logger.warning(
                "The MCP session at %s did not close in %s seconds. The app keeps the answer of the tool.",
                connection.url,
                seconds,
            )
        return answer[0]


def _default_client():
    """The one place the MCP client library exists (M1). Imported lazily, like litellm."""
    try:
        import anyio  # pylint: disable=import-outside-toplevel
        import httpx2  # pylint: disable=import-outside-toplevel
        from mcp import ClientSession  # pylint: disable=import-outside-toplevel
        from mcp.client.streamable_http import streamable_http_client  # pylint: disable=import-outside-toplevel
    except ImportError as error:
        # The cause in the message, for the reason `services.llm` gives: a dependency that is
        # installed and unimportable is not a missing extra, and telling somebody to install it
        # again sends them the wrong way.
        raise ImproperlyConfigured(
            "The MCP client could not be imported, so no tool can be called: "
            f"{type(error).__name__}: {error}. "
            "If it is not installed, install the app with the 'mcp' extra: nautobot-event-tracker[mcp]."
        ) from error
    return _StreamableHTTPClient(
        ClientSession, streamable_http_client, httpx2.AsyncClient, httpx2.Timeout, anyio.move_on_after
    )
