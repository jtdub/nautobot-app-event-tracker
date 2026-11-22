# Getting Started with the App

This page shows the first steps with a ticket. It also lists the next steps.

## Install the App

To install the App, follow the [Installation Guide](../admin/install.md).

## First steps with the App

These steps use the Nautobot web interface. You need the permission to add and to transition a ticket.

1. Open **Apps > Event Tracker > Event Types**. The app adds a set of event types when you install it.
2. Open **Apps > Event Tracker > Tickets**.
3. Click **Add Event Ticket**.
4. Enter a **Title**.
5. Choose an **Event Type**.
6. Choose a **Severity**.
7. Click **Create**.
8. Read the new ticket page. The ticket has the status **New**. The **Update Trail** panel shows one entry that says the ticket opened.
9. Click **Transition**.
10. Choose **Triaged**.
11. Enter an optional **Note**.
12. Click **Update**.
13. Read the **Update Trail** panel again. It has a new entry for the transition.

![The Update Trail panel of a ticket page, with one row for each change to the ticket](../media/ss_ticket_detail_light.png#only-light)
![The Update Trail panel of a ticket page, with one row for each change to the ticket](../media/ss_ticket_detail_dark.png#only-dark)

The **Transition** menu shows only the transitions that the workflow allows from the current status. The edit form has no status field. See [Using the App](app_use_cases.md) for the reasons.

To link a device or an interface to the ticket, click **Attach Object**.

The web interface has no button for a comment. To add a comment, use the REST API action `tickets/<id>/comment/`. See [External Interactions](external_interactions.md).

## What are the next steps?

Use these guides to go further:

- [Using the App](app_use_cases.md) tells about the ticket workflow, the update trail, and the repeat events.
- [Running the event consumer](../admin/ingestion.md) tells how to open tickets from a broker.
- [Configuring LLM Providers](../admin/llm.md) tells how to turn on event triage.
- [Running the Agent](../admin/agents.md) tells how to let an agent investigate a ticket.
- [Retrieval over Closed Tickets](../admin/rag.md) tells how to show similar closed tickets on a ticket page.
- [The Analytics Dashboard](../admin/dashboard.md) tells about ticket flow, ingestion health, and model cost.
- [Frequently Asked Questions](faq.md) answers common questions.
