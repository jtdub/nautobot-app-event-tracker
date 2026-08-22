# MCP Server

An MCP Server record makes a tool server *known* to Event Tracker. It does not make anything on
that server callable — see [MCP Tool](mcptool.md), and
[ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) for why the two are
separate records.

## What it holds

| Field | Description |
| --- | --- |
| Name | What this server is called here. Unique. |
| Description | Free text for whoever reads the list later. |
| External Integration | Where the server is and how to reach it: the endpoint URL, its headers, its TLS settings, its timeout, and the secrets group holding whatever authenticates to it. |
| Enabled | Unticking this makes every tool on the server uncallable at once, whatever the individual tools say. |
| Last Discovered At | When this server's tool list was last read. |

No credential is stored on this record. The integration's secrets group holds it, resolved at call
time and never logged — the same arrangement [LLM Providers](llmprovider.md) use.

## Transport

Streamable HTTP, and nothing else. There is no setting for the transport, because a setting is how
stdio — and with it arbitrary local process execution driven by database rows — comes back.

## Discovering tools

**Discover Tools** on the server's page reads the server's advertised tool list and reconciles this
app's registry with it. `nautobot-server discovermcptools` does the same thing from a shell, for a
deployment that would rather schedule it.

Discovery grants nothing. New tools arrive **disabled** and marked **mutating**; a tool whose
argument schema has changed since somebody enabled it is disabled again and named in the result.
A tool the server has stopped advertising is reported and left alone — a server having a bad
minute must not silently undo an operator's decisions.

## Elsewhere

`/api/plugins/event-tracker/mcp-servers/` and GraphQL. Configuring what agents do with these tools
is [Agents and MCP](../admin/mcp.md).
