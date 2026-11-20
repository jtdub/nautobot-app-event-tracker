# Compatibility Matrix

| Event Tracker Version | Nautobot First Support Version | Nautobot Last Support Version | AI Models App |
| ------------- | -------------------- | ------------- | ------------- |
| 1.0.X         | 3.2.0                | 3.99.99        | >=1.1.0,<2.0.0 |

## How to read this

Each Event Tracker release names the range of Nautobot versions it is tested against. The range is
declared in three places, and all three say the same thing:

- `pyproject.toml`, as the `nautobot` dependency pin. This is what `pip` enforces when the app is
  installed.
- `EventTrackerConfig.min_version` and `max_version` in `nautobot_event_tracker/__init__.py`. This
  is what Nautobot checks at startup, and it is the one that catches a Nautobot upgraded in place
  underneath an already-installed app — the case a pin cannot see.
- This table.

If you change one, change all three.

## What the range means

The app uses Nautobot v3 APIs throughout — the UI Component Framework, `NautobotUIViewSet`, and the
`nautobot.apps.*` public API — so an older Nautobot does not merely warn, it fails to render. The
upper bound is the next major version, which is where Nautobot may remove a public API.

Every Nautobot API the app uses comes from `nautobot.apps.*`, which Nautobot maintains as a stable
surface across minor releases, with one deliberate exception (`TokenPermissions`, noted in
`nautobot_event_tracker/api/views.py`). Core models are the sanctioned exception to the rule rather
than a breach of it: Nautobot does not re-export `Device`, `Status`, `ExternalIntegration` or
`Secret` through `nautobot.apps.models`, and directs you to import them from their own apps. A
Nautobot minor upgrade should need no change here; `TokenPermissions` is the first thing to check
if one does.

## Support and deprecation

The most recent Event Tracker release is the supported one. A new Nautobot minor release needs no
version change at all — `3.99.99` and `<4.0.0` already admit every 3.x — but it is picked up only
once the test suite passes against it. A new Nautobot major release is where the three declarations
move together. Dropping support for a Nautobot version raises `min_version` and is a minor release
of this app, called out in the [release notes](release_notes/index.md).

This app supports Python 3.11 through 3.14. Nautobot 3.2 also runs on Python 3.10, but this app
does not. PostgreSQL is the only supported database
([ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md)); MySQL is not supported and is not planned.

## The AI Models app

`nautobot-ai-models` owns the registry this app reads, and the pin is a floor with a major-version
ceiling, declared in `pyproject.toml` alone — Nautobot's `min_version` mechanism checks Nautobot,
not a sibling app, so there is no second place to keep in step.

The floor is 1.1 rather than 1.0 and every part of that is load-bearing: 1.1 is the release that
records a provider's dialect and whether it is enabled, a model's kind and its generation
parameters, and the two MCP registry-policy settings
[ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) relies on. Against 1.0
this app cannot address an Ollama endpoint, tell a chat model from an embedding one, or keep an
unreviewed tool out of an agent's hands. Raising the floor again is a minor release of this app and
is called out in the [release notes](release_notes/index.md).
