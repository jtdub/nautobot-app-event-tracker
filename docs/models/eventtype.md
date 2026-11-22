# Event Type

An Event Type is a category of network event that the app knows about, for example "Interface Down", "BGP Session Down" or "Circuit Down". Each ticket has exactly one Event Type.

## Fields

| Field | Description |
| --- | --- |
| **Name** (`name`) | The unique name of the Event Type. |
| **Description** (`description`) | What this kind of event means. |
| **Default Severity** (`default_severity`) | The severity of a new ticket of this type when the event or the request gives no severity. The list and the form show the name as **Default severity**. |
| **Enabled** (`enabled`) | You cannot use a disabled Event Type for a new ticket. Tickets that already exist keep their type. |

## Seeded types

When you install the app, it creates ten starter types:

| Name | Default severity |
| --- | --- |
| Device Unreachable | Critical |
| Circuit Down | Critical |
| Interface Down | Major |
| BGP Session Down | Major |
| Hardware Alarm | Major |
| Optical Degradation | Minor |
| High CPU Utilization | Minor |
| High Memory Utilization | Minor |
| Configuration Drift | Warning |
| Unclassified | Info |

You can edit the starter types. The seed runs one time, at install. A second run does not change a type that has the same name. Add your own types as you need them.

## Retire a type {#retiring-a-type}

You cannot delete an Event Type that has tickets. The tickets use the type, so the app refuses to delete it. To retire the type, clear the **Enabled** checkbox. This does not change tickets that already exist. The app opens no new ticket with this type.
