# Ticket Update

A Ticket Update is one entry in the update trail of a ticket. The app writes one entry for each of these changes:

- The ticket opens.
- An actor adds a comment.
- A ticket transitions to a new status.
- The assigned user changes.
- The severity changes.
- An actor attaches or detaches an object.
- The same event occurs again.
- An agent proposes, decides or runs a tool call.

## Append-only

You cannot edit or delete an update. The web interface has no edit form and no delete button. The REST endpoint accepts no write method. `POST`, `PATCH`, `PUT` and `DELETE` all return `405 Method Not Allowed`. The model refuses to change a saved update, also from code.

If an entry is wrong, add a comment that says so. Use the REST API action `tickets/<id>/comment/`. The original entry stays.

One exception exists. When you delete a ticket, the app deletes its updates. The trail belongs to the ticket.

## Fields

The web interface has no page for one update. The **Update Trail** panel of the ticket page shows the updates in these columns.

| Field | Column | Description |
| --- | --- | --- |
| `ticket` | None | The ticket that this entry belongs to. |
| `update_type` | **Type** | The kind of change. The value is **Created**, **Comment**, **Status Change**, **Assignment**, **Severity Change**, **Object Attached**, **Object Detached**, **Recurrence**, **Tool Proposed**, **Tool Decided** or **Tool Executed**. |
| `source` | **Source** | The kind of actor that made the change: **Human**, **AI** or **System**. **System** means automation. |
| `user` | **User** | The person who acted. The field is empty for an **AI** entry and for a **System** entry. The app never shows a person as the actor of these entries. |
| `message` | **Message** | A text that describes the change. |
| `from_status`, `to_status` | None | The status before and after a transition. The app sets them on **Status Change** entries only. |
| `related_object` | **Object** | The object that the app attached or detached. The app sets it on **Object Attached** and **Object Detached** entries only. |
| `created` | **When** | The time when the app wrote the entry. |

## Read the trail {#reading-the-trail}

The **Update Trail** panel of the ticket page shows the entries, oldest first. The REST endpoint `/api/plugins/event-tracker/ticket-updates/` and GraphQL also serve them. You can filter them by ticket, type, source, user or date.

In GraphQL, the type of the attached object is a nested relation:

```graphql
{
  ticket_updates {
    update_type
    related_object_type { app_label model }
    related_object_id
  }
}
```

The REST API returns the same data as one flat string in the form `app_label.model`.
