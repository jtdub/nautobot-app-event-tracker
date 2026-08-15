# Ticket Update

A Ticket Update is one entry in a ticket's history. Every change to a ticket writes one: the ticket being opened, a comment, a status change, an assignment, a severity change, an object being attached or detached, and a repeat occurrence of the same event.

## Append-only

Updates cannot be edited or deleted. There is no edit form, no delete button, and the REST endpoint accepts no write method at all — `POST`, `PATCH`, `PUT` and `DELETE` all return `405 Method Not Allowed`. The model refuses in-place modification even from code.

If something in the trail is wrong, add a comment saying so. The record of what happened stays intact.

The one exception: deleting a ticket deletes its updates along with it, because the trail belongs to the ticket.

## Fields

| Field | Description |
| --- | --- |
| Ticket | The ticket this entry belongs to. |
| Update Type | What kind of change this was. |
| Source | Whether a person, AI, or automation made the change. |
| User | The person who acted. Empty for AI and automation entries — those are never attributed to a person. |
| Message | Human-readable description of the change. |
| From Status / To Status | Filled in for status changes only. |
| Related Object | Filled in for attach and detach entries only. |
| Created | When the entry was written. |

## Reading the trail

The trail appears at the bottom of every ticket, oldest first. It is also queryable through `/api/plugins/event-tracker/ticket-updates/` and through GraphQL, where it can be filtered by ticket, type, source, user, or date.
