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

## What the server claims, and why it decides nothing

MCP lets a server annotate its own tools with `readOnlyHint`. Event Tracker records that claim in
**Server Claims Read-Only** and acts on it in no way whatsoever. **Mutating** is set by a person
and by nobody else.

This is not caution for its own sake. `Mutating` is the only thing standing between a tool and an
agent calling it without asking anyone. If a server could set it, a compromised or hostile server
would advertise `push_config` as read-only, it would land in the harmless-looking half of the list,
and an operator working down that half in bulk would enable it. No Nautobot permission is needed
for that attack — only control of the server's own answer.

The MCP specification says the same thing in its own words: a client must never make tool-use
decisions from annotations received from the server those annotations describe.

What the claim is good for is review. A tool where the server says *read-only* and this deployment
still says *mutating* is a row worth a second look — either the server is right and somebody should
untick Mutating, or it is not, and that is worth knowing about the server.

| Field | Description |
| --- | --- |
| Server Claims Read-Only | The server's `readOnlyHint`, or unset when it makes no claim. Refreshed by discovery, read by nobody. |

## What the server said

| Field | Description |
| --- | --- |
| Server | The server offering it. Deleting the server deletes its tools. |
| Name | The tool name sent on the wire. Unique per server, not globally. |
| Description | As the server advertised it. Refreshed by discovery. |
| Input Schema | The JSON Schema for the tool's arguments. Refreshed by discovery. |
| Last Seen At | When discovery last saw this tool advertised. |

## When the definition changes

The description and the argument schema together are what was reviewed when the tool was enabled.
If discovery finds either has changed under an **enabled** tool, that tool is **disabled** and
named in the discovery result. What was allowed is not what is now being offered, and somebody
should look before it runs again.

The description counts, not just the schema. It is half of what you read when you decided whether
the tool mutates — a schema of `{"device": "string"}` rarely says — and it is the sentence that
tells an agent what the tool is *for*. A server that wanted to change a tool's meaning without
tripping the alarm would leave the arguments alone and rewrite that sentence.

A change to a tool that was already disabled is simply recorded — there is nothing to withdraw.

## Reviewing a new server in bulk

Enabling forty tools one row at a time is how an operator ends up enabling all forty to be done
with it. Filter the tool list by server, sort by **Mutating**, select what should be readable, and
use bulk edit. The friction is admitted in ADR 0007 and this is where it is answerable.

## Elsewhere

`/api/plugins/event-tracker/mcp-tools/` and GraphQL. The API can enable a tool, because that is an
operator decision an operator may reasonably script; no route anywhere calls one.
