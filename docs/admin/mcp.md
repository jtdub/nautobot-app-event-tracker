# Registering MCP Servers and Tools

An MCP server is somewhere Event Tracker can go and ask a question — a device inventory, a
monitoring system, a change-management API. This page is how you register one and decide what, if
anything, this deployment may call on it.

!!! info "Registering a server grants nothing"
    Registering a server makes it *known*. Every tool on it arrives disabled and classified as
    changing the network, and stays that way until a person says otherwise. The
    [agent](agents.md) is the only thing that calls one, it is off by default, and it stops at
    every mutating tool to ask.

## Before you start

Install the app with the `mcp` extra, or discovery will refuse with a line telling you so:

```shell
pip install nautobot-event-tracker[mcp]
```

The server must speak **streamable HTTP**. stdio servers — of which there are many — cannot be used
without an HTTP bridge in front of them. That is a deliberate refusal, not an omission:
[ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) says why.

## 1. Create an External Integration

The server's address and credentials live in a Nautobot **External Integration**, the same as every
other outbound connection this app makes:

| Field | Use |
| --- | --- |
| Remote URL | The server's streamable HTTP endpoint. Jinja2 templating works here. |
| Secrets Group | The credential, under access type *generic*, secret type *token* (or *secret*). Sent as `Authorization: Bearer …`. |
| HTTP Headers | Anything else the server wants. An `Authorization` header you write yourself wins over the secret, for a server that authenticates some other way. |
| SSL Verification / CA File Path | Honoured. Unticking verification wins over a CA path. |
| Timeout | Applied to discovery and to every tool call. |

Nothing key-shaped is stored on the app's own records or in `PLUGINS_CONFIG`.

## 2. Register the server

**Apps → Event Tracker → MCP Servers → Add.** Name it, point it at the integration, leave it
enabled.

At this point the server is *known*. Nothing on it is callable.

## 3. Discover its tools

**Discover Tools** on the server's page, or:

```shell
nautobot-server discovermcptools --server "Device Inventory"
nautobot-server discovermcptools            # every enabled server
```

Discovery reads the advertised tool list and writes one **MCP Tool** record per tool. Every new
tool arrives **disabled** and marked **mutating** — whatever the server says about it. A server
advertising forty tools has granted access to none of them, and cannot classify any of them for
you.

## 4. Review, which is the actual work

Go through the tool list and make two decisions per tool:

- **Is it mutating?** Does calling it change something — on a device, in another system, anywhere?
  If it only reads, untick **Mutating**. If you are not sure, leave it ticked. The **Server Claims
  Read-Only** column shows what the server says about itself; it is a place to start looking, not
  an answer. A server that could set this field could hand you a config-push tool filed under
  "safe".
- **Should this deployment be able to call it at all?** If yes, tick **Enabled**.

A read-only tool is not automatically a harmless tool. It is a decision about what the author of an
event — who may not be someone you trust — can eventually cause to be read.

Filter the tool list by server, sort by **Mutating**, and use bulk edit. Reviewing forty tools one
row at a time is how people end up enabling all forty to be done with it, and ADR 0007 admits this
friction rather than pretending it away.

## Re-discovery

Run discovery again whenever the server changes, or on a schedule. Three things can happen:

- **A new tool appears.** It arrives disabled. It is named in the result so you can go and look.
- **A tool's definition changed** — its description or its argument schema. If that tool was
  enabled, it is **disabled again** and named. Those two together are what you reviewed; if either
  has changed, the thing you allowed is not the thing now on offer. The description counts because
  it is what tells an agent what the tool is for.
- **A tool is no longer advertised.** It is reported and otherwise left alone. A server having a
  bad minute must not silently undo your decisions.

Nothing discovery does ever enables a tool or sets **Mutating**, on a new tool or an old one.

## The off switches

| Switch | Effect |
| --- | --- |
| **Enabled** on a tool | That tool becomes uncallable. |
| **Enabled** on a server | Every tool on it becomes uncallable at once, whatever the tools say. |
| Uninstalling the `mcp` extra | Nothing can be called and discovery refuses with a message naming the extra. |

## Permissions

Discovery writes tool records, so it needs `add_mcptool` — not merely `view_mcpserver`. Enabling a
tool is `change_mcptool`. Treat both as privileged: between them they decide what this deployment
can be made to do.

## Further reading

- [MCP Server](../models/mcpserver.md) and [MCP Tool](../models/mcptool.md) — the records
- [ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) — transport and default-deny
- [Running the Agent](agents.md) — the thing that calls what you enable here
- [ADR 0009](../decisions/0009-agent-runs-are-jobs-that-end-at-the-gate.md) — how the approval gate works
