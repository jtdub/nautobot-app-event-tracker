# Agent Tool Call

An Agent Tool Call is one tool call that an agent asked for, and what happened to the call. The service layer writes it as part of an [Agent Run](agentrun.md). No form creates one.

## Call statuses {#the-difference-between-the-two-kinds-of-call-in-one-column}

| Status | How a call gets this status |
| --- | --- |
| **Proposed** | The agent asked for the call. The app makes a read-only call at once. A writable call stops here and waits for a person. |
| **Approved** | A person approved the call. The next run makes it. |
| **Denied** | A person denied the call. The run ends. |
| **Executing** | The app makes the call now. |
| **Executed** | The call ran and the server answered. |
| **Failed** | The call did not finish, the server reported an error, or the app refused the call. The **Error** field says why. |

A read-only call goes to **Executed** or **Failed**. It never has a decider. This is the only difference between a read-only call and a call that changes something. The **Writable** field of the MCP Tool that the call names decides it. The AI Models app owns that field.

## Fields

The detail page of a call shows the bold labels below. The list shows **Proposed** for `proposed_at`. It shows **Decided at**, **Latency ms** and **Called at**, with a small letter on the second word.

| Field | Description |
| --- | --- |
| **Proposed At** (`proposed_at`) | The time when the model asked for the call. |
| **Run** (`run`) | The run that asked for the call. |
| **Tool** (`tool`) | The MCP Tool of the AI Models app. The registry refuses to delete a tool that has a call, so the record of a call stays. |
| **Status** (`status`) | What happened to the call. See the table above. |
| **Decided By** (`decided_by`) | The person who approved or denied the call. The field is empty for a read-only call. |
| **Decided At** (`decided_at`) | The time when the person decided. |
| **Latency Ms** (`latency_ms`) | The time that the call took, in milliseconds. |
| **Called At** (`called_at`) | The time when the call reached the server. |
| **Tool Fingerprint** (`tool_fingerprint`) | A digest of the definition, the name and the endpoint of the tool when the agent proposed the call. |
| **Error** (`error`) | Why the call failed, if it did. The app cuts a long text. See the texts below. |
| **Arguments** (`arguments`) | What the model asked for, exactly as it asked. The arguments do not change after the proposal. The detail page shows them in a panel. |
| **Result** (`result`) | What came back, cut at a limit. The detail page shows it in a panel. |

In **Result**, `truncated` shows that the app cut the text at the limit. `separators_shortened` shows that the app made long rows of separator characters or table spaces short. Then the text is not the exact text of the device.

A call that a stop of the Job interrupted has one of these error texts:

- "The Job reached its soft time limit before this call finished." The call is read-only.
- "The process that ran this agent stopped before this call finished." The call is read-only.
- "The call was interrupted before the app got an answer. This tool changes the network, so the change might be in effect. Check the device before you approve the same change again." The call is writable.

## Approve or deny a call {#approving-and-denying}

Both actions need the permission "Can approve or deny a proposed agent tool call". The `change` permission does not include it. Approval of a call against the network is not the same right as the edit of a row.

No Nautobot page shows this permission name. In **Admin > Permissions**, an operator selects the object type `nautobot_event_tracker > Agent Tool Call` and enters `approve` in **Additional actions**. See [The approval gate](../admin/agents.md#the-approval-gate) for the full steps.

Click **Approve Tool Call** or **Deny Tool Call** on the ticket page. You can also click **Approve** or **Deny** on the page of the call. Both routes accept `POST` only. The REST API has `POST /api/plugins/event-tracker/agent-tool-calls/{id}/approve/` and the same path with `deny/`.

A decision obeys three rules:

- **A decision needs a person.** This is not a policy. The trail entry has the source `human`. The service layer refuses that entry without a user. An AI cannot approve its own proposal, because the function that it must call refuses it.
- **A person decides a call one time only.** The update trail shows what happened.
- **The arguments do not change.** An approval approves the arguments exactly as the agent proposed them. To use different arguments, deny the call. Write the reason in a ticket comment. Then run the agent again.

A denial ends the run. The agent does not answer a denial with a slightly different call. This rule stops a negotiation between the approver and a model.

## What the app checks again when it makes the call {#what-is-re-checked-at-execution-time}

An approval is not a token that stays valid. When the resumption run makes the call, `services/mcp.py` checks these items before it sends any network traffic:

- The tool is enabled, on a server that is enabled.
- The server uses the *Streamable HTTP* transport.
- The call is still **Approved**, and it did not run before.
- The fingerprint of the definition, the name and the endpoint of the tool is the same as the **Tool Fingerprint** of this call.

The last check covers a rare case. A rename of the tool or a change of its endpoint also stops the call. Discovery can find that the description or the schema of a tool changed. The AI Models app can then disable the tool, and an operator can review the new definition and enable the tool again. The proposal that an approver sees can still name the old definition. The approval covers the call on the tool as it was when the agent proposed the call.

## Elsewhere

The REST API serves calls at `/api/plugins/event-tracker/agent-tool-calls/`. GraphQL also serves them. Both are read-only, except for the two decision actions of the REST API.
