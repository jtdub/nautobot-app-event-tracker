# Event Ticket

An Event Ticket is a unit of work raised for one or more network events. It holds the description of the problem, its severity, who is working it, and where it sits in the workflow — and it points at the Nautobot objects involved rather than copying them.

## Fields

| Field | Description |
| --- | --- |
| Title | Short summary of the problem. |
| Event Type | The kind of event that raised the ticket. |
| Status | Where the ticket sits in the workflow. **Not directly editable** — see below. |
| Severity | How serious the underlying event is, from Critical down to Info. |
| Source | Whether the ticket was opened by a person, by AI, or by automation. |
| Description | The detail of the problem. |
| Assigned To | The user working the ticket, if anyone. |
| Dedup Key | Optional idempotency key. A repeat event with the same key joins the open ticket instead of opening a second one. |
| Event Count | How many times this event has been seen. |
| First Seen / Last Seen | When the event was first and most recently observed. |
| Resolved At / Closed At | Set automatically when the ticket reaches those states. |
| Resolution | How the problem was fixed. Required to resolve a ticket. |
| Payload | Raw event data. Unused today; populated once event ingestion arrives. |

## Status is not an editable field

You will not find Status on the ticket edit form, and a REST `PATCH` carrying it is rejected rather than ignored. Status changes go through the **Transition** control on the ticket page, or `POST /api/plugins/event-tracker/tickets/{id}/transition/`.

This is deliberate. Only some status changes are legal, every one has to leave an audit entry, and some are forbidden to AI actors. Routing them through one path is what makes those guarantees hold. See [the ticketing workflow guide](../user/app_use_cases.md) for the full state diagram.

## Attached objects

Devices, interfaces, IP addresses, prefixes, cables, circuits and locations can be attached to a ticket with the **Attach Object** button. They appear on the ticket grouped by type.

Attachment is recorded in the update trail rather than in a table of its own, so a detached object leaves a record that it was once attached. Which object types may be attached is configurable — see [Install and Configure](../admin/install.md).

## Deletion

Deleting a ticket also deletes its update trail. An Event Type that has tickets cannot be deleted at all.
