# Getting Started with the App

This page shows the first steps with a ticket. It also lists the next steps.

## Install the App

To install the App, follow the [Installation Guide](../admin/install.md).

## First steps with the App

These steps use the Nautobot web interface. You need the permission to add and to transition a ticket.

1. Open **Apps > Event Tracker > Event Types**. The app adds a set of event types when you install it.
2. Open **Apps > Event Tracker > Tickets**.
3. Click **Add Event Ticket**.
4. Enter a **Title**, choose an **Event Type** and a **Severity**, and click **Create**.
5. Read the new ticket page. The ticket has the status **New**. The **Update Trail** panel shows one entry that says the ticket opened.
6. Click **Transition** and choose **Triaged**. Enter an optional **Note**, and click **Update**.
7. Read the **Update Trail** panel again. It has a new entry for the status change.

![The Update Trail panel of a ticket page, with one row for each change to the ticket](../media/ss_ticket_detail_light.png#only-light)
![The Update Trail panel of a ticket page, with one row for each change to the ticket](../media/ss_ticket_detail_dark.png#only-dark)

The **Transition** menu shows only the moves that the workflow allows from the current status. The edit form has no status field. See [Using the App](app_use_cases.md) for the reasons.

To link a device or an interface to the ticket, click **Attach Object**.

The web interface has no button for a comment. To add a comment, use the REST API action `tickets/<id>/comment/`. See [External Interactions](external_interactions.md).

## What are the next steps?

Use these guides to go further:

- [Using the App](app_use_cases.md) explains the ticket workflow, the update trail, and the repeat events.
- [Running the event consumer](../admin/ingestion.md) opens tickets from a broker.
- [Configuring LLM Providers](../admin/llm.md) turns on event triage.
- [Running the Agent](../admin/agents.md) lets an agent investigate a ticket.
- [Retrieval over Closed Tickets](../admin/rag.md) shows similar closed tickets on a ticket page.
- [The Analytics Dashboard](../admin/dashboard.md) shows ticket flow, ingestion health, and model cost.
- [Frequently Asked Questions](faq.md) answers common questions.
