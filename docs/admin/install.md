# Installing the App in Nautobot

Here you will find detailed instructions on how to **install** and **configure** the App within your Nautobot environment.

## Prerequisites

- The app is compatible with Nautobot 3.2.0 and higher.
- **Databases supported: PostgreSQL only.**

!!! warning "PostgreSQL is required"
    Unlike most Nautobot apps, Event Tracker does not support MySQL. Later phases index closed
    tickets as vector embeddings using the `pgvector` extension, which has no MySQL equivalent
    worth maintaining a second code path for. A MySQL-backed Nautobot deployment cannot install
    this app. See [ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md) for the full
    reasoning.

    `pgvector` itself is **not** required by this release and is not checked for. It becomes a
    requirement when retrieval features land.

!!! note
    Please check the [dedicated page](compatibility_matrix.md) for a full compatibility matrix and the deprecation policy.

### Access Requirements

This release needs no access to anything outside Nautobot. It makes no outbound network calls,
requires no API credentials, and has no external service dependencies.

## Install Guide

!!! note
    Apps can be installed from the [Python Package Index](https://pypi.org/) or locally. See the [Nautobot documentation](https://docs.nautobot.com/projects/core/en/stable/user-guide/administration/installation/app-install/) for more details. The pip package name for this app is [`nautobot-event-tracker`](https://pypi.org/project/nautobot-event-tracker/).

The app is available as a Python package via PyPI and can be installed with `pip`:

```shell
pip install nautobot-event-tracker
```

To ensure Event Tracker is automatically re-installed during future upgrades, create a file named `local_requirements.txt` (if not already existing) in the Nautobot root directory (alongside `requirements.txt`) and list the `nautobot-event-tracker` package:

```shell
echo nautobot-event-tracker >> local_requirements.txt
```

Once installed, the app needs to be enabled in your Nautobot configuration. The following block of code below shows the additional configuration required to be added to your `nautobot_config.py` file:

- Append `"nautobot_event_tracker"` to the `PLUGINS` list.
- Append the `"nautobot_event_tracker"` dictionary to the `PLUGINS_CONFIG` dictionary and override any defaults.

```python
# In your nautobot_config.py
PLUGINS = ["nautobot_event_tracker"]

PLUGINS_CONFIG = {
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
| `attachable_object_types` | The seven DCIM/IPAM/Circuits models above | `app_label.model` strings naming the object types that may be attached to a ticket. Attaching anything else is refused, and the object picker only offers types on this list. |
| `llm` | `{}` | Settings for the LLM service layer — currently only `usage_retention_days`. Providers, models and credentials are registry objects, not settings; see [Configuring LLM Providers](llm.md). |

Widening the list is a deliberate act: it decides what a ticket — and, in later phases, an AI
triage step — is allowed to point at. Adding `extras.secret` would be a poor idea.

Once the Nautobot configuration is updated, run the Post Upgrade command (`nautobot-server post_upgrade`) to run migrations and clear any cache:

```shell
nautobot-server post_upgrade
```

Then restart (if necessary) the Nautobot services which may include:

- Nautobot
- Nautobot Workers
- Nautobot Scheduler

```shell
sudo systemctl restart nautobot nautobot-worker nautobot-scheduler
```

## Permissions

Alongside Django's usual four permissions per model, the app adds one:

| Permission | Codename | Grants |
| --- | --- | --- |
| Can transition event ticket status | `nautobot_event_tracker.transition_eventticket` | Moving a ticket through the workflow |

It is deliberately separate from `change_eventticket`, and neither implies the other. That lets you
give a first-line team the ability to move tickets through the workflow without the ability to
rewrite ticket content — or the reverse, for a team that curates ticket detail but does not own
the queue.

A typical operator role needs `view` and `add` on all three models, `change` on Event Ticket, and
`transition_eventticket`.

Two permissions are easy to forget because their absence degrades a page rather than blocking it:

- **`view_ticketupdate`** — without it the ticket page renders, but the update trail panel is
  hidden. A user who can see tickets but not their history is usually not what you meant.
- **`view_eventtype`** — without it the event type picker on the ticket form has nothing to offer,
  so tickets cannot be created.

### The AI actor, and why triage has no account

[Event triage](llm.md) runs inside the consumer process, which already has database access, so it
needs **no Nautobot user, no API token, and no permissions** — there is nothing to set up. Its
actions are attributed by kind, not by account: every update it writes carries source *AI* and no
user, and the domain rules *require* that combination, so triage could not act through an account
even if you made one.

Two guarantees hold regardless of configuration, because they are enforced in the service layer
rather than by permissions:

- An AI actor cannot modify a resolved or closed ticket — not its status, not its comments, not
  its attachments. Permissions cannot grant this.
- An AI action is never recorded against a person's username. The actor kind is stored on every
  update, and AI entries carry no user.

!!! note "Forward-looking — agents in a later phase"
    A service account becomes relevant when AI **agents** arrive (a later phase) and reach
    Nautobot over its REST API or MCP tools, where a token must authenticate the *transport* even
    though the recorded actor stays AI-with-no-user. Plan that account with `view`, `add`,
    `change` and `transition_eventticket` on Event Ticket, `view` on Event Type and Ticket
    Update — and explicitly no `delete` on anything: nothing in the AI path ever needs to remove
    a record. Restrict its token as you would any service credential.
