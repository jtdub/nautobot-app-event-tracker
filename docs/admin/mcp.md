# Registering MCP Servers and Tools

An MCP server is somewhere Event Tracker can go and ask a question — a device inventory, a
monitoring system, a change-management API. This page is how you register one and decide what, if
anything, this deployment may call on it.

The registry itself belongs to the [AI Models app](https://github.com/jtdub/nautobot-app-ai-models),
which this app depends on ([ADR 0010](../decisions/0010-ai-registry-lives-in-nautobot-ai-models.md)).
Servers and tools are managed under **AI Tools → MCP Models**, and discovery is that app's Job. What
this app contributes is everything that happens after: the agent that asks for a tool, the approval
gate a person answers, and the record of every call.

!!! warning "Set the two registry policy settings first"
    The AI Models app is a catalogue, and its defaults are a catalogue's: a newly discovered tool
    arrives **enabled**, and a tool whose definition changes stays enabled. That is the wrong
    posture for a registry an agent calls from, and
    [ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) requires the other
    one. Set both before you register anything:

    ```python
    PLUGINS_CONFIG = {
        "nautobot_ai_models": {
            # A newly advertised tool arrives disabled. A server offering forty tools grants
            # access to none of them until a person says otherwise.
            "new_tools_enabled": False,
            # A tool whose description or argument schema moved is taken back out of service.
            # Those two together are what a reviewer read.
            "disable_on_definition_change": True,
        },
        "nautobot_event_tracker": {...},
    }
    ```

    Without them the registry still works and the approval gate still holds — a writable tool
    needs a human decision per call, and a call whose tool changed after it was proposed is
    refused. What you lose is the part that stops an unreviewed tool being offered to a model at
    all.

## Before you start

Install this app with the `mcp` extra, or every tool call refuses with a line telling you so:

```shell
pip install nautobot-event-tracker[mcp]
```

The AI Models app needs its own `discovery` extra to read a server's tool list; it is installed
with this app's dependency already, so there is nothing extra to do.

The server must speak **streamable HTTP**. The registry records `sse` and `stdio` as transports too,
so a server this app cannot reach can still be catalogued — but a call against either is refused
before any network I/O. stdio servers cannot be used without an HTTP bridge in front of them. That
is a deliberate refusal, not an omission: ADR 0007 says why.

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

Nothing key-shaped is stored on a registry record or in `PLUGINS_CONFIG`.

## 2. Register the server

**AI Tools → MCP Models → MCP Servers → Add.** Name it, point it at the integration, leave the
transport at *Streamable HTTP*, leave it enabled.

At this point the server is *known*. Nothing on it is callable.

## 3. Discover its tools

Run the **MCP Server Discovery** Job — from **Jobs**, or from the *Run Discovery* button on the
server's own page, which pre-selects it. Leave `remove_stale` unticked unless you want the table to
mirror the server exactly.

Discovery reads the advertised tool list and writes one **MCP Tool** record per tool. With
`new_tools_enabled: False` set, every new tool arrives **disabled** and marked **writable** —
whatever the server says about it. A server advertising forty tools has granted access to none of
them, and cannot classify any of them for you.

## 4. Review, which is the actual work

Go through the tool list and make two decisions per tool:

- **Is it writable?** Does calling it change something — on a device, in another system, anywhere?
  If it only reads, untick **Writable**. If you are not sure, leave it ticked. The **Advertised
  Read Only** column shows what the server says about itself; it is a place to start looking, not
  an answer. A server that could set this field could hand you a config-push tool filed under
  "safe", and the MCP specification says in as many words that a client must not decide from it.
- **Should this deployment be able to call it at all?** If yes, tick **Enabled**.

A read-only tool is not automatically a harmless tool. It is a decision about what the author of an
event — who may not be someone you trust — can eventually cause to be read.

Filter the tool list by server, sort by **Writable**, and use bulk edit. Reviewing forty tools one
row at a time is how people end up enabling all forty to be done with it, and ADR 0007 admits this
friction rather than pretending it away.

## Re-discovery

Run the Job again whenever the server changes, or on a schedule. Three things can happen:

- **A new tool appears.** With `new_tools_enabled: False` it arrives disabled. It is named in the
  Job result so you can go and look.
- **A tool's definition changed** — its title, its description, or either of its schemas. With
  `disable_on_definition_change: True` an enabled tool is **disabled again** and named. Those
  fields together are what you reviewed; if any has changed, the thing you allowed is not the thing
  now on offer. The description counts because it is what tells an agent what the tool is for.
- **A tool is no longer advertised.** It is disabled and reported, keeping its row and its review.
  A server having a bad minute must not silently undo your decisions.

Nothing discovery does ever enables a tool or sets **Writable** on a tool the server still offers.

Independently of all of the above, this app refuses at execution any approved call whose tool has
changed since the call was proposed — its definition, its name, or the endpoint it is reached at.
That check is unconditional and does not depend on the two settings.

## The off switches

| Switch | Effect |
| --- | --- |
| **Enabled** on a tool | That tool becomes uncallable. |
| **Enabled** on a server | Every tool on it becomes uncallable at once, whatever the tools say. |
| A transport other than *Streamable HTTP* | Every tool on that server is refused before any network I/O. |
| Uninstalling the `mcp` extra | Nothing can be called, with a message naming the extra. |

## Permissions

The registry moved, and so did its permissions. Discovery writes tool records, so the Job needs
`nautobot_ai_models.add_mcptool` — not merely `view_mcpserver`. Enabling a tool is
`nautobot_ai_models.change_mcptool`. Treat both as privileged: between them they decide what this
deployment can be made to do.

Approving a proposed call is this app's own `nautobot_event_tracker.approve_agenttoolcall`, and it
is a separate right from either of the above.

!!! note "Upgrading from a release before ADR 0010"
    Roles that carried `nautobot_event_tracker.change_mcptool` or `add_mcptool` need the
    `nautobot_ai_models` equivalents instead. A migration cannot do this for you: object
    permissions are deployment data.

## Further reading

- [ADR 0010](../decisions/0010-ai-registry-lives-in-nautobot-ai-models.md) — why the registry lives elsewhere
- [ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) — transport and default-deny, and its amendment
- [Running the Agent](agents.md) — the thing that calls what you enable here
- [ADR 0009](../decisions/0009-agent-runs-are-jobs-that-end-at-the-gate.md) — how the approval gate works
