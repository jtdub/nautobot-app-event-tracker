# 0007 — MCP tools over streamable HTTP with a default-deny allowlist

**Status:** Accepted
**Phase:** 4

## Context

Phase 4 agents need tools: query a device, look up a circuit, open a change record. MCP is the interface for exposing those tools, and it defines several transports. The choice of transport and the choice of what an agent may reach are both security decisions, made once here rather than per deployment.

The stdio transport requires the agent process to spawn a subprocess. That means arbitrary local process execution driven by configuration, inside a Nautobot deployment, and it does not work at all when the MCP server lives on another host — which, in a network operations context, is the normal case.

The reachability question is sharper. An agent working a ticket about a degraded optic has no business calling a tool that reloads a chassis. Tools that mutate network state are exactly the tools an LLM should be furthest from, and the failure mode of getting this wrong is an outage.

## Decision

**Streamable HTTP is the only supported MCP transport.** No stdio, no subprocess spawning. Servers are network endpoints, authenticated, reachable across hosts.

**Server connection details are `ExternalIntegration` objects**, following ADR 0006 — same pattern, same secrets handling.

**Tool access is default-deny.** An `MCPServer` record makes a server known; it does not make its tools callable. Each tool must be individually registered as an `MCPTool` and explicitly enabled. A server that advertises forty tools grants access to none of them until an operator enables them one at a time. New tools appearing on an already-approved server are discovered as disabled and stay that way until someone acts.

**Tools are classified read-only or mutating**, and mutating tools pass an approval gate: the agent proposes the call, a human approves it, and only then does it execute. The proposal, the decision, the approver, and the result are all recorded on the ticket through the same append-only update trail as everything else (ADR 0001).

## Consequences

**Good.** The blast radius of a confused or manipulated agent is bounded by an explicit, auditable list. Adding a capability is a deliberate operator action with a name attached. Remote MCP servers work naturally, and no configuration value can cause process execution.

**Bad.** Onboarding a server is tedious in proportion to its tool count. This is the intended trade and the UI should make bulk review pleasant, but it is friction and operators will feel it.

**Bad.** stdio-only MCP servers — of which there are many in the wider ecosystem — cannot be used without putting an HTTP bridge in front of them.

**Bad.** Approval gates make agents interactive. An agent that stops for approval cannot run unattended overnight, which limits automation to read-only work unless an operator is present.

## Alternatives considered

**Allow stdio for local servers.** Rejected: subprocess execution driven by database rows is a privilege escalation path, and the convenience does not pay for it.

**Server-level rather than tool-level approval.** Rejected: it makes the security boundary the server author's tool list, which can change under you without notice.

**Trust the model to avoid dangerous tools.** Rejected: prompt-level restrictions are guidance, not a control, and they are defeated by exactly the adversarial input a ticket about a security incident is likely to contain.
