# MCP Tool

One tool on one [MCP Server](mcpserver.md), and — the part that matters — whether anyone has
allowed it.

## The two decisions that are yours

Everything else on this record is what the server said about itself. These two are what this
deployment says about the server:

| Field | Default | What it means |
| --- | --- | --- |
| **Enabled** | **No** | Whether this tool may be called at all. A disabled tool is never offered to a model and would be refused if one named it anyway. |
| **Mutating** | **Yes** | Whether calling it changes something. A mutating tool never runs without a person approving that specific call. |

Both defaults are deliberately the unhelpful ones. A server advertising forty tools grants access
to none of them until somebody goes through the list. And a tool nobody has classified is treated
as though it changes the network, because getting that wrong in one direction costs a click and in
the other direction costs an outage.

MCP's own `readOnlyHint` annotation pre-fills **Mutating** for a tool nobody has classified yet. It
never overrules a person, and re-discovery will not use it to reclassify a tool you have already
decided about: the hint is written by the server's author, and the boundary it would be deciding is
yours.

## What the server said

| Field | Description |
| --- | --- |
| Server | The server offering it. Deleting the server deletes its tools. |
| Name | The tool name sent on the wire. Unique per server, not globally. |
| Description | As the server advertised it. Refreshed by discovery. |
| Input Schema | The JSON Schema for the tool's arguments. Refreshed by discovery. |
| Last Seen At | When discovery last saw this tool advertised. |

## When a schema changes

The argument schema is the thing that was reviewed when the tool was enabled. If discovery finds it
has changed under an **enabled** tool, that tool is **disabled** and named in the discovery result.
What was allowed is not what is now being offered, and somebody should look before it runs again.

A change to a tool that was already disabled is simply recorded — there is nothing to withdraw.

## Reviewing a new server in bulk

Enabling forty tools one row at a time is how an operator ends up enabling all forty to be done
with it. Filter the tool list by server, sort by **Mutating**, select what should be readable, and
use bulk edit. The friction is admitted in ADR 0007 and this is where it is answerable.

## Elsewhere

`/api/plugins/event-tracker/mcp-tools/` and GraphQL. The API can enable a tool, because that is an
operator decision an operator may reasonably script; no route anywhere calls one.
