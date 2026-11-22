# App Overview

This page describes what Event Tracker does and who uses it. It also lists the parts of Nautobot that the app uses.

## Description

Event Tracker turns network events into tickets. A person, a script, or the event consumer opens a ticket. Each ticket keeps an update trail that nobody can edit or delete.

The app can also use a language model to help with a ticket. It can decide if an event is worth a ticket. An agent can investigate a ticket with read-only tools. A person must approve each tool call that can change the network.

The ticket list shows each ticket with its status and severity.

![The Event Tickets list of Event Tracker, with the status and severity of each ticket](../media/ss_ticket_list_light.png#only-light)
![The Event Tickets list of Event Tracker, with the status and severity of each ticket](../media/ss_ticket_list_dark.png#only-dark)

## Audience

These people use the app:

- **Network operations engineers** work the tickets. They triage, comment, transition, and resolve.
- **Nautobot administrators** install the app. They configure the consumer, the language models, the agent, and the permissions.
- **Automation developers** use the REST API and GraphQL to open tickets and to read the update trail.

## Authors and Maintainers

James Williams ([jtdub](https://github.com/jtdub)) maintains the app.

## Nautobot Features Used

The **Installed Apps** page of Nautobot lists the app as **Event Tracker**. The app adds the features that follow.

### Menu items

**Apps > Event Tracker** has these items:

- **Tickets**
- **Ingestion Stats**
- **Event Types**
- **Analytics**, only when the dashboard is on

**AI Tools > Event Tracker AI** has these items:

- **LLM Usage**
- **Agent Runs**
- **Agent Tool Calls**
- **Ticket Embeddings**

The **AI Tools** tab also holds the AI providers, the AI models, and the MCP servers. The AI Models app owns them.

### Jobs

The app adds two Jobs. Find them under **Jobs > Jobs**.

| Job | Use |
| --- | --- |
| **Investigate an Event Ticket** | Runs the agent over one ticket. See [Running the Agent](../admin/agents.md). |
| **Index a Closed Event Ticket** | A Job Hook receiver. It indexes a ticket when the ticket closes. See [Retrieval over Closed Tickets](../admin/rag.md). |

### Management commands

| Command | Use |
| --- | --- |
| `nautobot-server eventconsumer` | Reads events from a broker and opens tickets. See [Running the event consumer](../admin/ingestion.md). |
| `nautobot-server indexclosedtickets` | Indexes the closed tickets that the app did not index yet. See [Retrieval over Closed Tickets](../admin/rag.md). |

### Permissions

The app adds two custom permissions:

| Permission | Allows |
| --- | --- |
| **Can transition event ticket status** (`transition_eventticket`) | Make a transition of a ticket from one status to another. |
| **Can approve or deny a proposed agent tool call** (`approve_agenttoolcall`) | Decide a tool call that the agent proposed. |

### API

The app adds a REST API at `/api/plugins/event-tracker/` and GraphQL types for its models. See [External Interactions](external_interactions.md).

### Extras

The app adds no custom field.
