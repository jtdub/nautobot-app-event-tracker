"""The MCP service layer: the one module in this app that speaks MCP.

Everything an agent reaches outside Nautobot goes through here, so that the transport decision and
the allowlist are made once rather than at each call site (ADR 0007).

Rules implemented here, referenced by number from the Phase 4B spec:

* **M1** - the MCP client library is imported only in this module, lazily, behind the optional
  `mcp` extra. A deployment without it gets a plain `ImproperlyConfigured` naming the extra.
* **M2** - streamable HTTP and no other transport. There is no setting for this, because a setting
  is how stdio comes back; nothing in this app imports `subprocess`, and a guard says so.
* **M3** - the endpoint, its headers, its TLS settings and its timeout come from the server's
  ExternalIntegration at call time, and the credential from that integration's secrets group.
  Nothing key-shaped lives in settings, on a model, or in a log line.
* **M4** - a tool that is not enabled, on a server that is not enabled, is refused before any
  network I/O, whatever the prompt, the model or the caller said.
* **M5** - discovery never grants. New tools arrive disabled and mutating, whatever the server
  claims about them; anything the server advertises differently under an enabled tool disables it
  and reports it.
* **M6** - a mutating tool runs only from an approved `AgentToolCall`, and only against the tool
  definition that call was approved against.
* **M7** - every call is on the record - arguments, result, latency, error - before its caller
  sees any of it. Rule L1's promise, made about the other kind of call this app makes.
* **M8** - every call is bounded in both directions: a timeout going out, and a cap on the result
  coming back.
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, replace

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from nautobot.apps.choices import SecretsGroupSecretTypeChoices

from nautobot_event_tracker.choices import AgentToolCallStatusChoices
from nautobot_event_tracker.models import MCPTool
from nautobot_event_tracker.secrets import read_secret
from nautobot_event_tracker.services.exceptions import MCPCallError, MCPConfigurationError

logger = logging.getLogger(__name__)

#: Applied when the integration says nothing. Discovery and tool calls both want a bound; this is
#: the one they get when nobody chose (M8).
DEFAULT_TIMEOUT_SECONDS = 30

#: How long the read side of a session may wait. The transport keeps a server-sent-event stream
#: open, so the read deadline is not the request deadline and must not be set from it. Matches what
#: the SDK's own HTTP client factory uses.
SSE_READ_TIMEOUT_SECONDS = 300

#: A ceiling on tool-list pages, so a server offering a cursor that never ends cannot hold a web
#: request open forever. Far above any real tool list.
MAX_TOOL_PAGES = 50

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

    def __repr__(self):
        """Say everything except the header values, one of which is usually the credential.

        A dataclass renders its whole contents by default, and this one is passed around, held in
        tracebacks and - with `DEBUG` on - rendered onto an error page. The header *names* are
        worth seeing when something is misconfigured; their values never are.
        """
        headers = ", ".join(sorted(self.headers))
        return f"MCPConnection(url={self.url!r}, headers=[{headers}], verify={self.verify!r}, timeout={self.timeout!r})"


@dataclass(frozen=True)
class ToolDefinition:
    """One tool as a server advertised it, before this app has any opinion about it."""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    #: The server's own claim that this tool only reads. Recorded and shown; never acted on.
    #: The MCP specification says a client must not make tool-use decisions from annotations it
    #: received from the very server they describe, and the approval gate is such a decision.
    read_only_hint: bool = None


@dataclass(frozen=True)
class DiscoveryReport:
    """What one discovery pass changed, in the terms an operator needs to act on."""

    added: tuple = ()
    updated: tuple = ()
    definition_changed: tuple = ()
    missing: tuple = ()

    @property
    def needs_attention(self):
        """The tools somebody has to look at: newly offered, or changed under an approval."""
        return tuple(self.added) + tuple(self.definition_changed)

    def summary(self):
        """One line for a log, a Job result or a UI message."""
        return (
            f"{len(self.added)} new, {len(self.updated)} updated, "
            f"{len(self.definition_changed)} disabled by a changed definition, "
            f"{len(self.missing)} no longer offered"
        )


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


def discover(server, *, client=None):
    """Read a server's tool list and reconcile the registry with it, granting nothing (M5).

    `client` is the test seam - an object with `list_tools(connection)` - and nothing outside a
    test supplies one. No test opens a socket.

    Returns a `DiscoveryReport`. Raises `MCPConfigurationError` when the server is disabled or
    unreachable by configuration, and `MCPCallError` when the server was reached and would not
    answer.
    """
    if not server.enabled:
        # Reading a disabled server's tool list is harmless, and refusing it is still right: the
        # only thing discovery does is write rows about tools nothing may call, and an operator
        # who disabled a server should not find its registry quietly changing underneath them.
        raise MCPConfigurationError(f"MCP server '{server}' is disabled.")

    connection = connection_for(server)
    caller = client if client is not None else _default_client()

    try:
        advertised = tuple(caller.list_tools(connection))
    except Exception as error:  # pylint: disable=broad-except
        # Whatever the client raised, the caller sees one family (the L4 rule, applied here).
        raise MCPCallError(f"Could not list the tools on '{server}': {error}") from error

    report = _reconcile(server, advertised)
    server.last_discovered_at = timezone.now()
    server.validated_save()
    logger.info("Discovered tools on MCP server %s: %s", server, report.summary())
    return report


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
    when it left the process and did not come back usable.
    """
    cap = int(max_result_chars) if max_result_chars else DEFAULT_MAX_RESULT_CHARS

    # Re-read rather than trusting the instance the caller is holding. Between a proposal and its
    # execution a person disables the tool, an operator switches the server off, or discovery
    # rewrites the definition - and those three are precisely the events the checks below exist to
    # notice. A cached row would notice none of them.
    tool = MCPTool.objects.select_related("server__external_integration__secrets_group").get(pk=tool_call.tool_id)
    _check_callable(tool_call, tool)

    connection = connection_for(tool.server)
    if timeout:
        connection = replace(connection, timeout=float(timeout))
    caller = client if client is not None else _default_client()

    started = time.monotonic()
    try:
        raw = caller.call_tool(connection, tool.name, dict(tool_call.arguments or {}))
    except Exception as error:  # pylint: disable=broad-except
        # M7 - on the record first, then the caller hears about it. Whatever the client raised,
        # the caller sees one family (rule L4's arrangement, applied to the other kind of call).
        _finish(tool_call, latency_ms=_elapsed_ms(started), error=f"The call failed: {error}")
        raise MCPCallError(f"Calling '{tool}' failed: {error}") from error

    result, truncated = _capped_result(raw, cap)
    if truncated:
        logger.info("Truncated the result of %s to %d characters", tool, cap)

    error = None
    if result.get("is_error"):
        error = result.get("text") or "The server reported an error and said nothing about it."
    _finish(tool_call, latency_ms=_elapsed_ms(started), result=result, error=error)
    return tool_call


def _check_callable(tool_call, tool):
    """M4 and M6, in that order, before any network I/O and whatever anything else said.

    The order matters to the message an operator reads: a disabled tool is the answer even when
    the call is also unapproved, because enabling it is what they have to do first.
    """
    if not tool.enabled:
        _refuse(tool_call, f"Tool '{tool}' is not enabled.")
    if not tool.server.enabled:
        _refuse(tool_call, f"MCP server '{tool.server}' is disabled.")

    # A mutating tool runs from an approved row and from nothing else. A read-only one runs from
    # its own proposal - it needs no decision (13.4) - and neither runs twice: an executed, failed
    # or denied row is a decision that has already happened, and re-running it would be a second
    # call nobody asked for.
    allowed = (
        (AgentToolCallStatusChoices.APPROVED,)
        if tool.mutating
        else (AgentToolCallStatusChoices.PROPOSED, AgentToolCallStatusChoices.APPROVED)
    )
    if tool_call.status not in allowed:
        _refuse(
            tool_call,
            f"Tool '{tool}' is mutating and this call is '{tool_call.status}', not approved."
            if tool.mutating
            else f"This call is '{tool_call.status}' and has already been decided.",
        )

    # M6's second half. M5 disables a tool whose definition changed, which covers most of this and
    # not all of it: an operator may review the new definition and re-enable the tool while a
    # proposal written against the old one is still waiting. What was approved was a call on the
    # tool as it read then.
    if tool_call.tool_fingerprint and tool_call.tool_fingerprint != tool.definition_fingerprint:
        _refuse(
            tool_call,
            f"'{tool}' has been re-advertised since this call was proposed. "
            "Approving approves the tool as it read then; propose it again against the new definition.",
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


def _capped_result(raw, cap):
    """M8 - what came back, rendered as plain JSON and bounded. True when something was dropped.

    Rendered rather than stored: the SDK's content blocks are pydantic models, which a JSONField
    cannot hold and a prompt cannot use. Text blocks become text and everything else becomes a
    marker naming its type, so a model reading the transcript can tell an image it cannot see from
    an answer that was empty.

    The text is capped first, because it is what a model and a person both read. Structured content
    is dropped only when capping the text was not enough, which is the case where a tool answered
    with a megabyte of JSON and the alternative to dropping it is storing it.
    """
    blocks = []
    for block in getattr(raw, "content", None) or []:
        text = getattr(block, "text", None)
        blocks.append(text if isinstance(text, str) else f"<{getattr(block, 'type', None) or 'content'}>")
    text = "\n".join(blocks)

    result = {"is_error": bool(getattr(raw, "is_error", False)), "text": text}
    structured = getattr(raw, "structured_content", None)
    if structured is not None:
        result["structured_content"] = structured

    if len(json.dumps(result, default=str)) <= cap:
        return result, False

    result["text"] = text[:cap]
    result["truncated"] = True
    if len(json.dumps(result, default=str)) > cap:
        result.pop("structured_content", None)
    return result, True


def _elapsed_ms(started):
    """Whole milliseconds since `started`."""
    return int((time.monotonic() - started) * 1000)


def _reconcile(server, advertised):
    """Write what was advertised onto the registry, and say what changed.

    One transaction: a server that advertises the same tool twice, or a name longer than the
    column, must not leave half a registry behind and half a discovery reported. Model validation
    is what catches both, and it raises `ValidationError`, which is outside the family every caller
    of this module handles - so it is translated here rather than escaping as a 500.
    """
    added, updated, definition_changed = [], [], []
    now = timezone.now()

    try:
        with transaction.atomic():
            existing = {tool.name: tool for tool in server.tools.all()}

            for definition in advertised:
                fingerprint = definition_fingerprint(definition)
                tool = existing.get(definition.name)

                if tool is None:
                    tool = _create(server, definition, fingerprint, now)
                    # Recorded immediately, so a server advertising one name twice updates its own
                    # first row rather than colliding with it on the unique constraint.
                    existing[tool.name] = tool
                    added.append(tool)
                    continue

                withdrawn = _update(tool, definition, fingerprint, now)
                (definition_changed if withdrawn else updated).append(tool)
    except (ValidationError, IntegrityError) as error:
        raise MCPCallError(f"'{server}' advertised a tool this registry cannot hold: {error}") from error

    advertised_names = {definition.name for definition in advertised}
    missing = tuple(tool for name, tool in sorted(existing.items()) if name not in advertised_names)

    return DiscoveryReport(
        added=tuple(added),
        updated=tuple(updated),
        definition_changed=tuple(definition_changed),
        # Reported, never disabled: a server having a bad minute must not silently undo an
        # operator's decisions, and a tool that is really gone fails its next call anyway.
        missing=missing,
    )


def _create(server, definition, fingerprint, now):
    """Write a newly advertised tool: disabled, mutating, and believed about nothing else.

    `mutating=True` unconditionally. The server's `readOnlyHint` is recorded beside it and decides
    nothing: it is written by the party the gate exists to constrain, and a client that let it
    through would let a hostile server file `push_config` under "safe to enable in bulk". The MCP
    specification states this directly, and rule M5 says the same thing in this app's own words.
    """
    tool = MCPTool(
        server=server,
        name=definition.name,
        description=definition.description or "",
        input_schema=definition.input_schema or {},
        mutating=True,
        advertised_read_only=definition.read_only_hint,
        enabled=False,
        definition_fingerprint=fingerprint,
        last_seen_at=now,
    )
    tool.validated_save()
    return tool


def _update(tool, definition, fingerprint, now):
    """Refresh what the server says about an existing tool. True when an approval was withdrawn.

    `mutating` and `enabled` are the operator's two columns and are never written from a server's
    answer - except to take `enabled` away, which is M5's whole point: what was allowed is not
    what is now being offered.
    """
    changed = tool.definition_fingerprint != fingerprint
    withdraw = changed and tool.enabled

    if not changed and tool.last_seen_at is not None:
        # Nothing to write but the timestamp, and this is a change-logged model: a nightly run
        # against a forty-tool server would otherwise file forty ObjectChange rows a night
        # recording that nothing happened. The stamp is worth less than the change log is.
        return False

    if withdraw:
        tool.enabled = False

    tool.description = definition.description or ""
    tool.input_schema = definition.input_schema or {}
    tool.advertised_read_only = definition.read_only_hint
    tool.definition_fingerprint = fingerprint
    tool.last_seen_at = now
    tool.validated_save()
    return withdraw


def definition_fingerprint(definition):
    """A stable digest of everything a server said about one tool, so "did this change" is one test.

    The description is in it as well as the schema. It is half of what a reviewer read when they
    decided whether the tool mutates - a schema rarely says that on its own - and in an agent's
    prompt it *is* the tool's semantics, which makes it the sentence a compromised server would
    rewrite while leaving the arguments alone.

    Sorted keys, because two servers - or two versions of one - may serialize the same schema in
    different orders, and a tool disabled for a key ordering is a tool an operator stops trusting
    the alarm on.
    """
    canonical = json.dumps(
        {
            "description": definition.description or "",
            "input_schema": definition.input_schema or {},
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
    """The real client: one MCP session per operation, over streamable HTTP and nothing else (M2).

    A session per operation rather than a pooled one. Discovery happens from a web request and a
    tool call from a Job, neither of which outlives its own operation, and a cached session would
    have to be invalidated on every registry edit to keep rule M4 honest.

    The SDK is asynchronous and every caller here is not, so each operation is one `asyncio.run`.
    That is correct in a WSGI request and in a Celery worker, and it is wrong inside a running
    event loop - which this app has none of, and which the error below explains if that changes.
    """

    def __init__(self, session_class, transport, http_client_class, timeout_class):
        """Hold the four pieces of the SDK and its HTTP client that this module uses."""
        self._session_class = session_class
        self._transport = transport
        self._http_client_class = http_client_class
        self._timeout_class = timeout_class

    def list_tools(self, connection):
        """Every tool the server advertises, following the pagination it uses.

        `tools/list` is a paginated request: a server may answer with a page and a cursor. Reading
        one page would leave the rest unregistered - so unreviewable, so uncallable - and would
        also report every tool from page two onwards as "no longer offered" on each run, which is
        the one signal an operator is meant to act on.
        """
        definitions = []
        for page in self._run(connection, self._pages):
            definitions.extend(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.input_schema or {},
                    read_only_hint=getattr(getattr(tool, "annotations", None), "read_only_hint", None),
                )
                for tool in page.tools
            )
        return tuple(definitions)

    def call_tool(self, connection, name, arguments):
        """Call one tool and hand back what the server said, unread.

        Nothing is interpreted here: the refusals happened before this was reached, and the
        rendering happens after it. This is the wire and nothing else.
        """

        async def _call(session):
            return await session.call_tool(name, arguments, read_timeout_seconds=connection.timeout)

        return self._run(connection, _call)

    async def _pages(self, session):
        """Every page of the tool list, in order.

        Bounded: a server that answered with a cursor pointing at itself would otherwise be an
        infinite loop inside a web request. The cap is far above any real tool list.
        """
        from mcp import types  # pylint: disable=import-outside-toplevel

        pages = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            pages.append(page)
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                return pages
        logger.warning("Stopped reading tool pages after %s; the server kept offering a cursor", MAX_TOOL_PAGES)
        return pages

    def _run(self, connection, operation):
        """Open a session, do one thing, close it."""

        async def _once():
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
                    async with self._session_class(read, write, read_timeout_seconds=connection.timeout) as session:
                        await session.initialize()
                        return await operation(session)

        try:
            return asyncio.run(_once())
        except RuntimeError as error:
            if "running event loop" not in str(error):
                raise
            raise MCPCallError(
                "MCP calls are made from synchronous code and cannot run inside an event loop."
            ) from error


def _default_client():
    """The one place the MCP client library exists (M1). Imported lazily, like litellm."""
    try:
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
    return _StreamableHTTPClient(ClientSession, streamable_http_client, httpx2.AsyncClient, httpx2.Timeout)
