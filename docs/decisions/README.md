# Architecture Decision Records

Each record captures one decision, the pressure that forced it, and what it costs. Records are immutable once accepted: to change a decision, add a new record that supersedes the old one rather than editing history.

| ADR | Title | Status |
| --- | ----- | ------ |
| [0001](0001-ticket-service-layer-as-sole-mutation-path.md) | Ticket service layer as the sole mutation path | Accepted |
| [0002](0002-explicit-status-workflow-graph.md) | Explicit status workflow graph in code | Accepted |
| [0003](0003-postgresql-with-pgvector-only.md) | PostgreSQL with pgvector as the only database | Accepted |
| [0004](0004-pluggable-event-broker-consumers.md) | Pluggable event broker consumers | Accepted |
| [0005](0005-standalone-consumer-process.md) | Standalone consumer process | Accepted |
| [0006](0006-litellm-service-layer-and-credential-storage.md) | litellm service layer and credential storage | Accepted |
| [0007](0007-mcp-tools-streamable-http-and-default-deny.md) | MCP tools over streamable HTTP with a default-deny allowlist | Accepted |
| [0008](0008-ui-component-framework-only.md) | UI Component Framework only | Accepted |

ADRs 0004 through 0007 describe later phases. They are recorded now because Phase 1 has to leave room for them.
