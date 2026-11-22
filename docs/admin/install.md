# Installing the App in Nautobot

Use these instructions to **install** and **configure** the app in your Nautobot environment.

## Prerequisites

- The app works with Nautobot 3.2.0 and later.
- The app needs Python 3.11 through 3.14.
- **Databases supported: PostgreSQL only.**
- The app needs the [AI Models app](https://pypi.org/project/nautobot-ai-models/), version 1.1 or
  later. `pip` installs it with the app.

!!! warning "PostgreSQL is required"
    Event Tracker does not support MySQL, unlike most Nautobot apps. When retrieval is on, the app
    indexes closed tickets as vector embeddings with the `pgvector` extension. MySQL has no equivalent that is
    worth a second code path. A Nautobot deployment on MySQL cannot install this app. For the
    reasons, see [ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md).

    The `pgvector` extension is **required** in the database. The first migration that needs it
    creates it when it is not there. On a managed PostgreSQL, the database user can fail to create
    it. In that case, ask a superuser to run `CREATE EXTENSION vector;` before you run the
    migrations. For the steps, see [Retrieval over Closed Tickets](rag.md).

!!! warning "The AI Models app is a hard dependency"
    `nautobot-ai-models` owns the AI provider, AI model, MCP server and MCP tool registries. The
    app reads these registries, and its models hold foreign keys into them
    ([ADR 0010](../decisions/0010-ai-registry-lives-in-nautobot-ai-models.md)). The dependency is
    not optional and is not behind an extra. A Nautobot that turns on this app without
    `nautobot_ai_models` does not start.

    `pip` installs the package automatically. You add it to `PLUGINS` yourself, as the install
    steps below show.

!!! note
    For a full compatibility matrix and the deprecation policy, see the
    [dedicated page](compatibility_matrix.md).

### Access Requirements

The app sends nothing outside Nautobot until you configure it to. After you configure it, each
endpoint and each credential comes from a Nautobot **External Integration** and its secrets group.
A registry object in the AI Models app names the External Integration. No endpoint and no
credential comes from `PLUGINS_CONFIG`. See [Configuring LLM Providers](llm.md) and
[Registering MCP Servers](mcp.md).

## Install Guide

!!! note
    You can install apps from the [Python Package Index](https://pypi.org/) or from a local source. For more details, see the [Nautobot documentation](https://docs.nautobot.com/projects/core/en/stable/user-guide/administration/installation/app-install/). The pip package name of this app is [`nautobot-event-tracker`](https://pypi.org/project/nautobot-event-tracker/).

The app is a Python package on PyPI. Install it with `pip`:

```shell
pip install nautobot-event-tracker
```

To install the app again at each future upgrade, add it to `local_requirements.txt`. This file is in the Nautobot root directory, next to `requirements.txt`. Make the file if it does not exist. Then list the `nautobot-event-tracker` package:

```shell
echo nautobot-event-tracker >> local_requirements.txt
```

After you install the app, turn it on in your Nautobot configuration. Make these changes in your `nautobot_config.py` file:

- Add both `"nautobot_ai_models"` and `"nautobot_event_tracker"` to the `PLUGINS` list.
- Add the `"nautobot_event_tracker"` dictionary to the `PLUGINS_CONFIG` dictionary. Override a default if you need to.

```python
# In your nautobot_config.py
PLUGINS = ["nautobot_ai_models", "nautobot_event_tracker"]

PLUGINS_CONFIG = {
    # The registry app needs no settings for the LLM half. These two are its MCP policy, and
    # they matter: without them a newly discovered tool is offered to an agent before anyone has
    # reviewed it. See "Registering MCP Servers and Tools".
    "nautobot_ai_models": {
        "new_tools_enabled": False,
        "disable_on_definition_change": True,
    },
    "nautobot_event_tracker": {
        # Object types that may be attached to a ticket. Anything not listed here is rejected.
        # Defaults to the list below; override it to widen or narrow what operators can attach.
        "attachable_object_types": [
            "dcim.device",
            "dcim.interface",
            "dcim.cable",
            "dcim.location",
            "ipam.ipaddress",
            "ipam.prefix",
            "circuits.circuit",
        ],
    }
}
```

### Settings

| Setting | Default | Description |
| --- | --- | --- |
| `attachable_object_types` | The seven DCIM, IPAM and Circuits models above | `app_label.model` strings. They name the object types that you can attach to a ticket. The app refuses any other type, and the object picker shows only the types on this list. |
| `llm` | `{}` | Settings for the LLM service layer. The only key is `usage_retention_days`. Providers, models and credentials are registry objects in the AI Models app, not settings. See [Configuring LLM Providers](llm.md). |

A wider list is a deliberate choice. The list decides which objects a ticket can point at. It also
decides which object types an event resolve rule can name. Do not add `extras.secret`.

After you update the Nautobot configuration, run the Post Upgrade command. The command runs the
migrations and clears the cache:

```shell
nautobot-server post_upgrade
```

Then restart the Nautobot services that need it. These can include:

- Nautobot
- Nautobot Workers
- Nautobot Scheduler

```shell
sudo systemctl restart nautobot nautobot-worker nautobot-scheduler
```

## Permissions

The app adds one permission to the four that Django makes for each model:

| Permission | Codename | Grants |
| --- | --- | --- |
| Can transition event ticket status | `nautobot_event_tracker.transition_eventticket` | The right to transition a ticket through the workflow |

This permission is separate from `change_eventticket`. Neither permission implies the other. You
can allow a first-line team to transition tickets without the right to rewrite ticket content. You
can do the reverse for a team that curates ticket detail but does not own the queue.

A typical operator role needs these permissions:

- `view` and `add` on **Event Type**, **Event Ticket** and **Ticket Update**.
- `change` on **Event Ticket**.
- `transition_eventticket`.

Two permissions are easy to forget. If one of these permissions is not there, a page shows less and does not block you:

- **`view_ticketupdate`**: without it, the ticket page opens, but it does not show the
  **Update Trail** panel. A user who can see tickets but not their update trail is usually not what you intend.
- **`view_eventtype`**: without it, the event type picker on the ticket form has no choices, so
  nobody can open a ticket.

### The AI actor, and why triage has no account

[Event triage](llm.md) runs inside the consumer process. That process already has database access.
Triage needs **no Nautobot user, no API token, and no permissions**, so you set up nothing. The app
attributes each action of triage by kind, not by account. Each update that triage writes has source
*AI* and no user. The domain rules require this combination, so triage cannot act through an
account, even if you make one.

Two guarantees hold in each configuration. The service layer enforces them, and permissions do not:

- An AI actor cannot change a resolved or closed ticket. This covers its status, its comments and
  its attachments. No permission can grant this.
- The app never records an AI action against the user name of a person. The app stores the actor
  kind on each update, and AI entries have no user.

!!! note "The agent needs no account"
    The [agent](agents.md) runs as a Nautobot Job. Like triage, it writes to a ticket through the
    service layer, as source *AI* with no user. It needs no service account and no permissions
    on Event Ticket. The person who starts a run needs the `run` permission on the Job.

    The agent uses no Nautobot token. It uses the credential of the AI provider and the credential of each
    MCP server, each in its secrets group. Give the MCP server credential only the rights that its
    tools need. See [Registering MCP Servers](mcp.md).
