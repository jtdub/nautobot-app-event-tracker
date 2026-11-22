# Registering MCP Servers and Tools

An MCP server is a system that the app can ask for data. Examples are a device inventory, a monitoring
system, and a change-management API. This page shows how to register a server and how to decide which of
its tools this deployment can call.

The registry belongs to the [AI Models app](https://github.com/jtdub/nautobot-app-ai-models). This app
depends on it ([ADR 0010](../decisions/0010-ai-registry-lives-in-nautobot-ai-models.md)). Manage servers
and tools under **AI Tools > MCP Models**. Discovery is a Job of the AI Models app. This app adds the
agent that asks for a tool and the approval gate that a person answers. It also adds the record of each call.

!!! warning "Set the two registry policy settings first"
    By default, the AI Models app enables each new tool and keeps a changed tool enabled. An agent can then
    offer a tool that no person reviewed. [ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md)
    requires the opposite. Set both settings before you register a server:

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

    Without these settings, the registry still works and the approval gate still works. A writable tool
    needs a decision from a person for each call. The app refuses a call when its tool changed after the
    agent proposed the call. But a model can get a tool that no person reviewed.

## What you need first {#before-you-start}

Install this app with the `mcp` extra. Without the extra, the app refuses each tool call. The message
names the extra:

```shell
pip install nautobot-event-tracker[mcp]
```

The AI Models app needs its own `discovery` extra to read the tool list of a server. This app depends on
that extra, so the install includes it. You do not need to do anything.

The server must use *Streamable HTTP*. The registry also records `sse` and `stdio` as transports. The
registry can list a server with one of these transports, but the app cannot call it. The app refuses each
call to it before any network request. To use a stdio server, put an HTTP bridge in front of it. ADR 0007 gives the reason for this refusal.

## 1. Create an External Integration

A Nautobot **External Integration** holds the address and the credentials of the server. The app uses the
same record type for all its other outbound connections. Set these fields:

| Field | Use |
| --- | --- |
| **Remote URL** | The streamable HTTP endpoint of the server. You can use Jinja2 templates in this field. |
| **Secrets group** | The credential. Use access type **Generic** and secret type **Token** (or **Secret**). The app sends it as `Authorization: Bearer …`. |
| **Headers** | Other headers that the server needs. A header `Authorization` that you write takes priority over the secret. Use it for a server with a different authentication method. |
| **Verify SSL** and **CA file path** | Discovery and each tool call use both. If you clear **Verify SSL**, the app does not verify the server, and it ignores the CA path. |
| **Timeout** | The number of seconds that discovery waits. A tool call from the agent uses the setting `tool_timeout_seconds` (see [Running the Agent](agents.md)). |

The app stores no key on a registry record or in `PLUGINS_CONFIG`.

## 2. Register the server

1. Go to **AI Tools > MCP Models > MCP Servers**.
2. Click **Add**.
3. Enter a name.
4. Select the **External Integration** from step 1.
5. Leave **Transport** at *Streamable HTTP*.
6. Leave **Enabled** selected.

The app now knows the server. The app can call no tool on it yet.

## 3. Discover its tools

Run the **MCP Server Discovery** Job. Use one of these ways:

- Go to **Jobs > Jobs** and run the Job.
- Click **Run Discovery** on the page of the server. This button selects the server in the Job form.

Leave **Remove stale tools** (`remove_stale`) cleared, unless you want the table to match the server
exactly.

Discovery reads the tool list that the server advertises. It writes one **MCP Tool** record for each tool.
With `new_tools_enabled: False`, each new tool starts disabled, with **Writable** selected. The server
cannot change this. A server that advertises forty tools gives access to none of them. It cannot classify
a tool for you.

## 4. Review the tools {#4-review-which-is-the-actual-work}

This step is the main work. Make two decisions for each tool in the list:

- **Is the tool writable?** Decide if a call changes something, on a device or in another system. If the
  tool only reads, clear **Writable**. If you are not sure, leave **Writable** selected.

    The **Advertised Read Only** column shows the claim of the server. Use it as a place to start. Do not
    use it as the answer. A server can mark a configuration-push tool as safe. The MCP specification says
    that a client must treat these annotations as untrusted.

- **Can this deployment call the tool at all?** If yes, select **Enabled**.

A read-only tool is not always a safe tool. The author of an event can be a person that you do not trust.
Your decision sets what that author can cause the app to read.

Filter the tool list by server. Sort it by **Writable**. Use bulk edit. A review of forty tools, one row
at a time, is slow. A person can then enable all forty only to finish. ADR 0007 accepts this problem.

## Re-discovery

Run the Job again when the server changes. You can also run it on a schedule. Three events can occur:

- **A new tool appears.** With `new_tools_enabled: False`, the tool starts disabled. The Job result names
  the tool, so you can review it.
- **The definition of a tool changes.** The definition is the title, the description, and the two schemas.
  With `disable_on_definition_change: True`, the Job disables an enabled tool and names it. You reviewed
  these parts together, so a change makes the tool different from the tool that you allowed. The
  description counts because it tells an agent what the tool is for.
- **The server no longer advertises a tool.** The Job disables the tool and reports it. The row and your
  review stay. If you select **Remove stale tools**, the Job deletes the tool instead. A short fault of the
  server must not undo your decisions.

With `new_tools_enabled: False`, discovery never enables a tool. Discovery never changes **Writable** on
a tool that is already in the registry.

The app also refuses a call when its tool changed after the agent proposed the call. The change can be in
the definition, the name, or the endpoint of the tool. This check always runs. It does not depend on the
two settings.

## The off switches

| Switch | Effect |
| --- | --- |
| Clear **Enabled** on a tool | The app cannot call that tool. |
| Clear **Enabled** on a server | The app cannot call any tool on that server, whatever the tools say. |
| A transport other than *Streamable HTTP* | The app refuses each call to a tool on that server before any network request. |
| Uninstall the `mcp` extra | The app cannot call any tool. The message names the extra. |

## Permissions

The AI Models app holds the registry, so it also holds the permissions. This list shows what each action
needs:

| Action | Permission |
| --- | --- |
| Run the **MCP Server Discovery** Job | `extras.run_job` on that Job |
| Show the **Run Discovery** button | `extras.run_job` and `nautobot_ai_models.change_mcptool` |
| Enable a tool | `nautobot_ai_models.change_mcptool` |

Nautobot installs a Job disabled. Select **Enabled** on the **MCP Server Discovery** Job once, in
**Jobs > Jobs**, before the first run. The **Run Discovery** button shows only when the Job is enabled.

The Job does not check `nautobot_ai_models.add_mcptool`. A user who can run the Job can add, disable, and
delete tool records. The Job writes them with the rights of the worker. With the default
`new_tools_enabled`, the Job also adds tools that are enabled. Treat `extras.run_job` on this Job as
privileged.

Treat `nautobot_ai_models.change_mcptool` as privileged too. Together, these permissions decide which tools
this deployment can call.

The permission to approve a proposed call is `nautobot_event_tracker.approve_agenttoolcall`. It is
separate from the permissions above.

!!! note "Upgrade from a release before ADR 0010"
    A role can carry `nautobot_event_tracker.change_mcptool` or `add_mcptool`. Replace each one with the
    `nautobot_ai_models` permission of the same name. A migration cannot do this, because object
    permissions are deployment data.

## Further reading

- [ADR 0010](../decisions/0010-ai-registry-lives-in-nautobot-ai-models.md): why the registry is in another app
- [ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md): the transport, the default-deny rule, and the amendment
- [Running the Agent](agents.md): the agent that calls the tools that you enable here
- [ADR 0009](../decisions/0009-agent-runs-are-jobs-that-end-at-the-gate.md): how the approval gate works
