# Event Type

An Event Type is a category of network event the system knows about — "Interface Down", "BGP Session Down", "Circuit Down". Every ticket belongs to exactly one.

## Fields

| Field | Description |
| --- | --- |
| Name | Unique name of the event type. |
| Description | What this kind of event means. |
| Default Severity | Severity applied to a new ticket of this type when the caller does not specify one. |
| Enabled | A disabled type cannot be used for new tickets. Existing tickets keep theirs. |

## Seeded types

Installing the app creates ten starter types:

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

Edit them freely — the seed only runs on install, and re-running it will not overwrite your changes. Add your own types as needed.

## Retiring a type

An Event Type with tickets cannot be deleted; the tickets reference it and the delete is refused. Clear the **Enabled** checkbox instead. Existing tickets keep working and reporting normally, but no new ticket can be opened against it.
