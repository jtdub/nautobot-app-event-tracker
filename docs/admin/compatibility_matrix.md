# Compatibility Matrix

| Event Tracker Version | Nautobot First Support Version | Nautobot Last Support Version | AI Models App |
| ------------- | -------------------- | ------------- | ------------- |
| 1.0.X         | 3.2.0                | 3.99.99        | >=1.1.0,<2.0.0 |

## How to read this

Each Event Tracker release names the range of Nautobot versions that the tests cover. The
range is in three places, and all three agree:

- `pyproject.toml`, as the `nautobot` dependency pin. `pip` enforces this pin when you install the
  app.
- `EventTrackerConfig.min_version` and `max_version` in `nautobot_event_tracker/__init__.py`.
  Nautobot checks these at startup. They catch a Nautobot that you upgrade in place under an
  installed app. A pin cannot catch this case.
- This table.

If you change one of the three, change all three.

## What the range means

The app uses Nautobot v3 APIs throughout: the UI Component Framework, `NautobotUIViewSet`, and the
`nautobot.apps.*` public API. An older Nautobot does not only warn. It fails to render. The upper
bound is the next major version, where Nautobot can remove a public API.

Every Nautobot API that the app uses comes from `nautobot.apps.*`. Nautobot keeps this surface
stable across minor releases. There is one deliberate exception: `TokenPermissions`, noted in
`nautobot_event_tracker/api/views.py`. Core models are a second exception that Nautobot allows.
Nautobot does not re-export `Device`, `Status`, `ExternalIntegration` or `Secret`
through `nautobot.apps.models`. It tells you to import them from their own apps. A Nautobot minor
upgrade needs no change here. If one does, check `TokenPermissions` first.

## Support and deprecation

Event Tracker supports only the most recent release. A new Nautobot minor release needs no
version change, because `3.99.99` and `<4.0.0` already allow each 3.x release. The app supports the
new release only after the test suite passes against it. A new Nautobot major release is the point
where the three declarations change together. When the app drops support for a Nautobot version, it
raises `min_version`. This is a minor release of this app, and the
[release notes](release_notes/index.md) call it out.

The app supports Python 3.11 through 3.14. Nautobot 3.2 also runs on Python 3.10, but this app does
not. PostgreSQL is the only supported database
([ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md)). The app does not support MySQL,
and does not plan to.

## The AI Models app

`nautobot-ai-models` owns the registry that this app reads. The pin is a floor with a major-version
ceiling. It is in `pyproject.toml` only. The `min_version` mechanism of Nautobot checks Nautobot and
not a sibling app. Because of this, you keep no second place in step.

The floor is 1.1, not 1.0, and each part of this is needed. Version 1.1 is the release that records
these facts:

- The dialect of a provider, and whether the provider is enabled.
- The kind of a model, and its generation parameters.
- The two MCP registry-policy settings that
  [ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) relies on.

With 1.0, this app cannot address an Ollama endpoint. It cannot tell a chat model from an embedding
model. It cannot keep an unreviewed tool away from an agent. A new floor is a minor release of this app. The
[release notes](release_notes/index.md) call it out.
