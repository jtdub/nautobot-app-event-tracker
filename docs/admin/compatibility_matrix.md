# Compatibility Matrix

| Event Tracker Version | Nautobot First Support Version | Nautobot Last Support Version |
| ------------- | -------------------- | ------------- |
| 1.0.X         | 3.2.0                | 3.99.99        |

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

Everything the app imports comes from `nautobot.apps.*`, which Nautobot maintains as a stable
surface across minor releases, with one deliberate exception (`TokenPermissions`, noted in
`nautobot_event_tracker/api/views.py`). A Nautobot minor upgrade should need no change here; that
exception is the first thing to check if one does.

## Support and deprecation

The most recent Event Tracker release is the supported one. A new Nautobot minor release is picked
up by widening `max_version` and the pin once the test suite passes against it. Dropping support
for a Nautobot version raises `min_version` and is a minor release of this app, called out in the
[release notes](release_notes/index.md).

Python support follows Nautobot's: 3.10 through 3.14. PostgreSQL is the only supported database
([ADR 0003](../decisions/0003-postgresql-with-pgvector-only.md)); MySQL is not supported and is not planned.
