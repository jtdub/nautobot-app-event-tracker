# Event Ticket

An Event Ticket is a unit of work for one network event or more. It holds the description of the problem, its severity, the user who works on it, and its status in the workflow. It points to the Nautobot objects that the event involves. It does not copy them.

## Fields

The ticket page shows each name with a capital letter on each word, for example **Event Count**. The list and the forms show the same name with a capital letter on the first word only, for example **Event count**.

| Field | Description |
| --- | --- |
| **Title** (`title`) | A short summary of the problem. |
| **Event Type** (`event_type`) | The kind of event that opened the ticket. |
| **Status** (`status`) | The status of the ticket in the workflow. You cannot edit it. See [Status is not an editable field](#status-is-not-an-editable-field). |
| **Severity** (`severity`) | The seriousness of the event. The values are **Critical**, **Major**, **Minor**, **Warning** and **Info**. |
| **Source** (`source`) | The kind of actor that opened the ticket: **Human**, **AI** or **System**. **System** means automation. |
| **Description** (`description`) | The details of the problem. |
| **Assigned To** (`assigned_to`) | The user who works on the ticket. The field is empty if no user does. |
| **Dedup key** (`dedup_key`) | An optional key. The add form and the edit form show it. The ticket page does not show it. A new event with the same key joins the open ticket. The app does not open a second ticket. |
| **Event Count** (`event_count`) | The number of times that the app saw this event. |
| **First Seen** (`first_seen`) | The time of the first event. |
| **Last Seen** (`last_seen`) | The time of the most recent event. |
| **Resolved At** (`resolved_at`) | The app sets it when the ticket becomes resolved or closed. |
| **Closed At** (`closed_at`) | The app sets it when the ticket becomes closed. |
| **Resolution** (`resolution`) | How the problem ended. You must give it to resolve a ticket. |
| `payload` | The raw event data. The consumer copies the event data into this field when it opens a ticket. The REST API shows it. The ticket page does not show it. |

## Status is not an editable field

The ticket edit form has no **Status** field. A REST `PATCH` that includes `status` fails with an error. The app does not ignore it.

To transition a ticket, use the **Transition** menu on the ticket page. You can also send `POST /api/plugins/event-tracker/tickets/{id}/transition/`.

This is intentional. The workflow allows only some transitions. Each transition must add an entry to the update trail. The app does not let an AI actor change a resolved ticket or a closed ticket. The app can keep these rules only when each transition uses one path. For the diagram of the workflow, see [Using the App](../user/app_use_cases.md).

## Attached objects

You can attach these objects to a ticket: devices, interfaces, IP addresses, prefixes, cables, circuits and locations. Use the **Attach Object** button on the ticket page. The button shows only on an open ticket. The **Attached Objects** panel shows the objects in groups by type.

The app does not store an attachment in a table of its own. It writes an **Object Attached** entry or an **Object Detached** entry in the update trail. The **Object Attached** entry stays when an actor detaches the object.

An administrator sets the object types that you can attach with the `attachable_object_types` setting. See [Install and Configure](../admin/install.md).

## Deletion

When you delete a ticket, the app also deletes its update trail. You cannot delete an Event Type that has tickets.
