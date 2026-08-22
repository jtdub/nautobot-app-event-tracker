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
* **M5** - discovery never grants. New tools arrive disabled and mutating; a schema that changed
  under an enabled tool disables it and reports it.
* **M8** - every call is bounded by a timeout.

Rules M4, M6 and M7 - the default-deny check, the approval check and the call record - land with
`call_tool()` in PR B, which is the PR that introduces the `AgentToolCall` row all three are
written on. Nothing in this module calls a tool.
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from nautobot.apps.choices import SecretsGroupSecretTypeChoices

from nautobot_event_tracker.models import MCPTool
from nautobot_event_tracker.secrets import read_secret
from nautobot_event_tracker.services.exceptions import MCPCallError, MCPConfigurationError

logger = logging.getLogger(__name__)

#: Applied when the integration says nothing. Discovery and tool calls both want a bound; this is
#: the one they get when nobody chose (M8).
DEFAULT_TIMEOUT_SECONDS = 30

#: How the credential is presented when the integration's own headers do not present it. Bearer is
#: what MCP servers over HTTP overwhelmingly expect; an operator who needs something else writes
#: that header on the integration, and this defers to them.
AUTHORIZATION_HEADER = "Authorization"


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


@dataclass(frozen=True)
class ToolDefinition:
    """One tool as a server advertised it, before this app has any opinion about it."""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    #: The server author's claim that the tool only reads. Read as a suggestion for a tool nobody
    #: has classified yet, and never as an answer: the boundary it would be deciding is ours.
    read_only_hint: bool = None


@dataclass(frozen=True)
class DiscoveryReport:
    """What one discovery pass changed, in the terms an operator needs to act on."""

    added: tuple = ()
    updated: tuple = ()
    schema_changed: tuple = ()
    missing: tuple = ()

    @property
    def needs_attention(self):
        """The tools somebody has to look at: newly offered, or changed under an approval."""
        return tuple(self.added) + tuple(self.schema_changed)

    def summary(self):
        """One line for a log, a Job result or a UI message."""
        return (
            f"{len(self.added)} new, {len(self.updated)} updated, "
            f"{len(self.schema_changed)} disabled by a schema change, {len(self.missing)} no longer offered"
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


def _reconcile(server, advertised):
    """Write what was advertised onto the registry, and say what changed."""
    added, updated, schema_changed = [], [], []
    now = timezone.now()
    existing = {tool.name: tool for tool in server.tools.all()}

    for definition in advertised:
        fingerprint = schema_fingerprint(definition.input_schema)
        tool = existing.get(definition.name)

        if tool is None:
            tool = MCPTool(
                server=server,
                name=definition.name,
                description=definition.description or "",
                input_schema=definition.input_schema or {},
                # The hint pre-fills a tool nobody has classified. Absent or false, it stays
                # mutating - which is also what an unhinted tool gets, deliberately.
                mutating=not definition.read_only_hint,
                enabled=False,
                schema_fingerprint=fingerprint,
                last_seen_at=now,
            )
            tool.validated_save()
            added.append(tool)
            continue

        was_enabled = tool.enabled
        changed = tool.schema_fingerprint != fingerprint
        if changed and was_enabled:
            # M5 - what the operator allowed is not what the server is now offering. Disabling is
            # the only honest answer: the arguments a tool takes are the thing that was reviewed.
            tool.enabled = False

        tool.description = definition.description or ""
        tool.input_schema = definition.input_schema or {}
        tool.schema_fingerprint = fingerprint
        tool.last_seen_at = now
        # `mutating` is deliberately not touched: it is a person's classification, and a server
        # must not be able to reclassify its own tool by changing a hint.
        tool.validated_save()

        if changed and was_enabled:
            schema_changed.append(tool)
        else:
            updated.append(tool)

    advertised_names = {definition.name for definition in advertised}
    missing = tuple(tool for name, tool in sorted(existing.items()) if name not in advertised_names)

    return DiscoveryReport(
        added=tuple(added),
        updated=tuple(updated),
        schema_changed=tuple(schema_changed),
        # Reported, never disabled: a server having a bad minute must not silently undo an
        # operator's decisions, and a tool that is really gone fails its next call anyway.
        missing=missing,
    )


def schema_fingerprint(schema):
    """A stable digest of an advertised schema, so "did this change" is one comparison.

    Sorted keys, because two servers - or two versions of one - may serialize the same schema in
    different orders, and a tool disabled for a key ordering is a tool an operator stops trusting
    the alarm on.
    """
    canonical = json.dumps(schema or {}, sort_keys=True, separators=(",", ":"))
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

    def __init__(self, session_class, transport, http_client_class):
        """Hold the three pieces of the SDK this module uses."""
        self._session_class = session_class
        self._transport = transport
        self._http_client_class = http_client_class

    def list_tools(self, connection):
        """Every tool the server advertises, as this module's own type."""
        result = self._run(connection, lambda session: session.list_tools())
        return tuple(
            ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema or {},
                read_only_hint=getattr(getattr(tool, "annotations", None), "read_only_hint", None),
            )
            for tool in result.tools
        )

    def _run(self, connection, operation):
        """Open a session, do one thing, close it."""

        async def _once():
            async with self._http_client_class(
                headers=connection.headers,
                verify=connection.verify,
                timeout=connection.timeout,
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
        raise ImproperlyConfigured(
            "The MCP client is not installed. Install the app with the 'mcp' extra: nautobot-event-tracker[mcp]."
        ) from error
    return _StreamableHTTPClient(ClientSession, streamable_http_client, httpx2.AsyncClient)
