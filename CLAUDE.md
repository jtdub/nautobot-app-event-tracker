# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Nautobot app (`nautobot_event_tracker`) that turns raw network events into tickets and keeps an append-only record of every change. Built in phases (see `docs/architecture.md`): Phase 1 (tickets, service layer, UI/API) and Phase 2 (broker consumers, ingestion pipeline) are implemented; LLM triage, agents, and RAG are later phases and deliberately absent — nothing imports litellm or calls a provider yet.

## Development commands

Everything runs through `invoke` (tasks in `tasks.py`), which wraps `docker compose` against the files in `development/`. Configuration comes from `invoke.yml` (copy `invoke.example.yml`) or `INVOKE_NAUTOBOT_EVENT_TRACKER_*` env vars. `local: true` runs commands directly instead of inside the container.

```bash
invoke build                  # build the dev container
invoke start / stop / destroy # manage the compose stack (Nautobot at http://localhost:8080)
invoke createsuperuser        # default user/password: admin/admin
invoke nbshell / cli / dbshell
invoke logs -s consumer -f    # follow one service's logs
```

### Tests

```bash
invoke tests                    # full suite: all linters + unit tests with coverage
invoke tests --lint-only        # linters only
invoke unittest                 # unit tests only (builds docs first; add --skip-docs-build to skip)
invoke unittest --label nautobot_event_tracker.tests.test_services            # one module
invoke unittest --label nautobot_event_tracker.tests.test_services.TestCreateTicket.test_dedup  # one test
invoke unittest --pattern dedup # -k pattern match
invoke unittest --keepdb --failfast   # faster iteration
```

### Linting / formatting

```bash
invoke autoformat             # ruff format + fix, djhtml (alias: invoke a)
invoke ruff                   # ruff check + format check
invoke pylint                 # pylint with pylint-django + pylint-nautobot
invoke djlint / yamllint / markdownlint / hadolint
invoke check-migrations       # fails if models changed without a migration
invoke makemigrations --name <name> / invoke migrate
```

Ruff line length is 120, Google docstring convention, bandit (`S`) enabled outside tests. Docstrings are required (pydocstyle `D`) except in migrations and tests.

### Changelog

Every PR needs a towncrier fragment in `changes/` named `<issue>.<type>` (types: `added`, `changed`, `fixed`, `dependencies`, `documentation`, `housekeeping`, etc. — see `pyproject.toml`). `invoke generate-release-notes` assembles them at release time.

## Containerlab lab (Phase 2.5)

An optional end-to-end lab: a three-node SR Linux fabric whose real syslog flows through a fluent-bit bridge onto a Redpanda (Kafka API) broker, with a consumer service running against it. One switch (`lab: true` in `invoke.yml`, or the `EVENT_TRACKER_LAB` env var) controls both which compose files load and whether Nautobot loads the lab ingestion config — the `lab-*` tasks set it themselves. Files live in `development/containerlab/`; docs in `docs/dev/lab.md`. Costs ~8 GB of memory.

```bash
invoke lab-up                 # deploy topology + start stack + populate Nautobot
invoke lab-break              # cause a real event (interface/bgp/unreachable/drift)
invoke lab-events             # read raw broker messages
invoke lab-consumer --dry-run # run the consumer in the foreground, decide-only
invoke lab-down               # tear down
```

Without the lab, the dev stack consumes from its own Redis and `invoke send-test-event` publishes canned events to it (`development/send_test_event.py`).

## Architecture

The load-bearing rule (ADR 0001): **`services/tickets.py` is the only code that writes `EventTicket` or `TicketUpdate`.** Views, serializers, forms, the ingestion pipeline, and tests all call service functions; nothing else assigns `ticket.status` or creates a `TicketUpdate`. The service layer enforces (rules S1–S5 from the Phase 1 spec): one transaction and exactly one TicketUpdate per mutation; status changes only along the explicit transition graph in `choices.py` (`TICKET_STATUS_TRANSITIONS`); AI actors may not touch resolved/closed tickets; `source=human` requires a user while `ai`/`system` forbid one; and dedup-key creation joins an existing open ticket instead of duplicating.

Ingestion (Phase 2) is a standalone process, not a Celery task: `nautobot-server eventconsumer` (`management/commands/eventconsumer.py`). Broker access sits behind `ingestion/consumers/base.py` with Kafka (`confluent-kafka`, optional extra) and Redis pub/sub implementations. Each message flows through `ingestion/pipeline.py`: decode/normalize → deterministic prefilter (`prefilter.py`: topic match, severity floor, dedup window, rate limit) → ticket creation via the service layer with `source=system`. Every message ends in exactly one counted outcome (`ingestion/stats.py` → `IngestionStats` model). Ingestion configuration defaults are applied in `ingestion/config.py`, not in `default_settings` — Nautobot merges `PLUGINS_CONFIG` one top-level key at a time.

Other constraints from the ADRs (`docs/decisions/`):

- PostgreSQL only (pgvector later); no MySQL support.
- UI is `NautobotUIViewSet` + UI Component Framework only — no hand-written templates.
- Phase specs live in `docs/specs/` and code comments reference them by section number; keep those references accurate when editing.

Documentation is MkDocs (`invoke docs` serves it; `invoke build-and-check-docs` runs in CI and before `invoke unittest`). Supported: Python 3.10–3.14, Nautobot >=3.2.0 <4.0.0.
