# Phase 2.5 — The lab environment

!!! warning "Draft — not yet approved"
    This is the execution spec for Phase 2.5. Section 10 lists the calls that most need a second opinion, and 10.1 asks whether the resource cost is acceptable before any of it is built. Nothing in this spec has been implemented.

## 1. Scope

Phase 2.5 is the only phase that ships no product code. Everything in it lives under `development/` or in the app's test-data command, and none of it changes what an operator installs.

It exists because Phase 2 is built on a guess. The field maps, the severity map, the dedup key template and the pre-filter defaults in the Phase 2 spec were written from what a syslog payload *usually* looks like, not from anything a real device sent. A lab that emits genuine SR Linux events turns that guess into evidence, exercises the Kafka consumer against a real broker rather than the stub the unit tests use, and gives anyone evaluating the app something to look at that is not a fixture.

Three deliverables:

1. **Test data.** Fill in the scaffolded `generate_nautobot_event_tracker_test_data` command so a developer gets a populated ticket list in one step.
2. **A containerlab topology.** SR Linux nodes whose logs are real, wired so that an operator can cause an event on purpose.
3. **A slim broker.** Redpanda rather than Kafka and ZooKeeper, plus the bridge that carries device logs into it.

Explicitly out of scope: anything in CI's pull-request gate (section 8), the enrichment resolver (Phase 4), and any change to the ingestion code itself. If running the lab shows the Phase 2 defaults are wrong, that is a Phase 2 change, made in Phase 2's own files — this phase produces the evidence, not the fix.

## 2. Why a lab rather than more fixtures

A fixture asserts that the code does what its author believed. It cannot discover that SR Linux writes its severity as a word where the configuration expected a number, that its timestamps carry a timezone the parser mishandles, or that a link flap produces eleven messages rather than one. Those are the failures this phase is for, and none of them are reachable from a test the same author wrote.

The trade is that a lab is heavy, slow and occasionally broken in ways that have nothing to do with the app. That is why it is opt-in, out of the PR gate, and specified as a separate phase rather than folded into Phase 2: a developer who never runs it must lose nothing.

## 3. Test data

`nautobot_event_tracker/management/commands/generate_nautobot_event_tracker_test_data.py` is currently the cookiecutter stub, with two `TODO`s. This is the one deliverable that lives in the app package, because Nautobot's own `generate_test_data` calls each installed app's command and a developer expects ours to do something.

```
nautobot-server generate_nautobot_event_tracker_test_data [--flush] [--seed SEED]
```

**What it creates.** Around fifty tickets spread across the seeded event types and every status, with comments, assignments, severity changes, attachments and recurrences in their trails — enough that the list view, every filter, the grouped attachment panel and the timeline all have something to show.

**Rules it obeys:**

- **Every ticket is built through the service layer**, exactly as the test fixtures are. A command that assigned `status` directly would be the first violation of the rule the app exists to enforce, and it would sit in the app package where an operator might read it as an example.
- **Tickets in a terminal state get there by walking the graph**, so their trails read like a ticket somebody actually worked.
- **Deterministic.** `--seed` defaults to a constant, so two developers comparing screenshots see the same data. Randomness comes from a seeded `random.Random`, never the module-level functions.
- **It creates the devices its tickets are about.** *Revised: an earlier draft refused to, arguing that a ticketing app inventing DCIM objects leaves a demo database full of devices nobody can explain. That is what `generate_test_data` is for, and Nautobot's own populates a whole demo estate; the objection is answered by tagging rather than by abstaining.* Everything it creates carries `event-tracker-test-data`, so it is explicable at a glance and `--flush` removes it. Where the database already holds a device of the same name — the lab's `leaf-01`, after section 6 has run — that one is used and left untagged, so a flush cannot delete it.
- **`--flush` deletes only what it created**, identified by a tag applied to every ticket and device it makes, and never touches a ticket a person opened or a device the lab populated. Tickets are deleted before devices, so an attachment never briefly points at something gone. The location, device type, manufacturer and role are left behind as empty scaffolding, since somebody may have filed their own objects under them.

**What it does not create.** No `IngestionStats` rows: those are a record of a consumer having run, and fabricating them would put numbers on the stats page that never corresponded to a message. A developer who wants stats runs the lab.

## 4. The topology

`development/containerlab/topology.clab.yml`, four nodes:

```
    ┌──────────┐        ┌──────────┐
    │ leaf-01  ├────────┤ leaf-02  │
    └────┬─────┘        └────┬─────┘
         │                   │
         └────────┬──────────┘
                  │
            ┌─────┴──────┐
            │  spine-01  │
            └────────────┘
                  │
            ┌─────┴──────┐
            │  client-01 │   (linux, iperf/ping)
            └────────────┘
```

Three `nokia_srlinux` nodes and one Alpine client. The image is `ghcr.io/nokia/srlinux`, which is publicly pullable and needs no licence. eBGP between the leaves and the spine gives a session that can be taken down, and the client gives traffic that can be interrupted.

**The events this can produce on purpose**, which are exactly the ones the seeded event catalogue names:

| What you do | What the device logs | Seeded event type |
| --- | --- | --- |
| `clab-…-leaf-01: interface ethernet-1/1 admin-state disable` | link down | Interface Down |
| Shut the peer's interface | BGP session transitions | BGP Session Down |
| `docker stop clab-…-leaf-02` | peer unreachable, session down | Device Unreachable |
| Change a configuration and commit | configuration commit messages | Configuration Drift |

An `invoke lab-break interface` task performs the first of these, so a developer can see a ticket appear without learning SR Linux CLI first. That task is the demo.

**Cost, stated plainly.** Each SR Linux node wants roughly 1 GB of memory and takes tens of seconds to boot; three of them plus Redpanda, Nautobot, PostgreSQL and Redis is an 8 GB machine minimum, comfortably 12. containerlab needs privileged Docker and manages network namespaces directly, so it does not run inside every sandbox — including, possibly, the one this spec was written in. A developer without the memory runs everything else in this repository exactly as before.

## 5. The broker and the bridge

`development/docker-compose.redpanda.yml`, an optional compose file layered on the existing stack the way `docker-compose.redis.yml` already is.

**Redpanda, not Kafka.** One container, no ZooKeeper and no KRaft controller to configure, a few hundred megabytes rather than a few gigabytes, and it speaks the Kafka protocol — `confluent-kafka` cannot tell the difference, which is the entire point. The reference deployment in [ADR 0004](../decisions/0004-pluggable-event-broker-consumers.md) is still Kafka; this is the development stand-in for it, and the ADR does not need reopening to say so.

**Fluent Bit as the bridge.** SR Linux exports syslog to a remote server; Fluent Bit takes syslog in and writes Kafka out. Its configuration lives in `development/containerlab/fluent-bit.conf` and does three things:

1. Listen for syslog on UDP 5140.
2. Parse the message into fields — host, facility, severity, message text — with a parser written for SR Linux's format.
3. Produce JSON to the `network.events` topic.

The parsed shape is what the Phase 2 field map is written against, and this file is therefore the answer to the question "where do these dotted paths come from". If the two disagree, one of them is wrong, and section 9's acceptance test is what discovers which.

A `redpanda console` container is included and disabled by default: seeing the raw messages is worth a great deal the first time the field map does not match, and worth nothing afterwards.

## 6. Populating Nautobot from the topology

`development/containerlab/populate_nautobot.py`, run by `invoke lab-populate`.

It reads the topology file and `containerlab inspect --format json`, and creates: a Location for the lab, a Manufacturer and DeviceType for SR Linux, a Role, the Devices under their clab names, their Interfaces from the topology's links, and the management IPs containerlab assigned. Every object is created with `get_or_create`, so running it twice changes nothing.

**It is a script under `development/`, not a management command.** A management command in the app package ships in the wheel, and a command that creates DCIM objects is not something an operator of a ticketing app should find installed. The cost is that it runs through `nautobot-server nbshell` rather than as a first-class command, which for a development script is a fair price. See 10.3.

The device names in Nautobot match the hostnames the devices put in their syslog messages. That is the whole reason to bother: it is what makes the Phase 4 enrichment resolver's job real rather than hypothetical, and until then it is what lets a person reading a ticket search for the device by name and find it.

## 7. Invoke tasks

Added to `tasks.py`, in their own section, each one refusing to run when containerlab is absent rather than failing halfway:

| Task | Does |
| --- | --- |
| `lab-up` | Start the compose stack with the Redpanda overlay, deploy the topology, wait for the nodes, populate Nautobot |
| `lab-down` | Destroy the topology and stop the overlay, leaving the ordinary dev stack alone |
| `lab-populate` | Section 6, on its own, for when the topology is already up |
| `lab-consumer` | Run `nautobot-server eventconsumer` against the lab configuration in the foreground |
| `lab-break <event>` | Cause one of the section 4 events on purpose |
| `lab-events` | Tail the `network.events` topic, so a developer can see what the bridge actually produced |

`lab-up` prints, at the end, the two things a person needs next: the Nautobot URL and the `invoke lab-break interface` line.

## 8. CI

**None of this runs in the pull-request gate.** containerlab needs privileged Docker, the nodes need gigabytes, and boot takes minutes; putting that in front of every pull request buys a signal the unit tests already give and costs flakiness that has nothing to do with the change under review.

What does go in CI is one workflow, `lab.yml`, triggered manually (`workflow_dispatch`) and nightly on the default branch, which brings the lab up, runs the section 9 acceptance script, and uploads the consumer log and the resulting ticket list as artifacts. When it fails, it fails on a schedule with a person's attention available, not on a contributor's pull request.

## 9. Acceptance criteria

1. **Test data.** `nautobot-server generate_nautobot_event_tracker_test_data` populates a database with tickets in every status, each with a trail built through the service layer; `--flush` removes exactly what it created and nothing else; the same `--seed` twice produces the same tickets. A test asserts the command writes no ticket whose status was assigned directly, by checking every ticket has a `created` update and a status trail consistent with the workflow graph.
2. **The topology comes up.** `invoke lab-up` deploys four nodes, and `containerlab inspect` reports them running.
3. **Events reach the broker.** After `invoke lab-break interface`, `invoke lab-events` shows a message on `network.events` within thirty seconds.
4. **Events become tickets.** With `nautobot-server eventconsumer` running against the lab configuration, the same break produces a ticket whose event type, severity and title are what the operator would expect, and whose `payload` holds the parsed message.
5. **The field map matches reality.** No field in the lab's `field_map` resolves to `None` for a message the bridge actually produced. This is criterion 3 of the whole phase's purpose: if it fails, the Phase 2 defaults are wrong and this is how we found out.
6. **Recurrence works on real traffic.** Flapping an interface repeatedly produces one ticket with a rising event count, not many tickets — the S5 rule, tested against a device rather than a fixture.
7. **Nothing ships.** The wheel built from this branch contains no containerlab file, no compose overlay and no Fluent Bit configuration; the only packaged change is the test-data command.
8. **The ordinary stack is unaffected.** `invoke build`, `invoke start` and `invoke tests` behave exactly as before on a machine with no containerlab installed.

## 10. Open questions

**10.1 The resource cost.** Three SR Linux nodes want 8 GB of memory before Nautobot has started. *Proposed reading:* build it anyway, keep it opt-in, and document the requirement at the top of the lab guide. If the target developer machine is smaller, the topology drops to two nodes — one leaf and one spine — which still produces link-down and BGP-down events and loses only the multi-path story.

**10.2 SR Linux specifically.** It is free, publicly pullable, and produces genuinely representative logs. It is also one vendor, and a field map tuned to it may fit nothing else. *Proposed reading:* start with SR Linux because it is the one that costs nothing to run, and treat the Fluent Bit parser as the seam where a second vendor would be added. A lab with FRR alongside it would be cheaper and less representative; a lab with a licensed image would be neither.

**10.3 The population script is not a management command.** *Proposed reading:* keep it out of the app package for the reason in section 6. *Cost:* it is invoked more awkwardly, and it cannot be tested by the app's test suite the way a command could. If you would rather have a command guarded by a `DEBUG`-only check, that is a defensible alternative and it changes section 6.

**10.4 Fluent Bit as the bridge.** Alternatives are a syslog-ng container with a Kafka destination, or having SR Linux export gNMI to a collector instead of syslog. *Proposed reading:* Fluent Bit, because it is one small container doing exactly one job and its parser is a file a person can read. gNMI would be more modern and would produce structured data with no parser at all, which is worth revisiting if the syslog parser turns out to be the fragile part.

**10.5 Redpanda in development, Kafka in the ADR.** *Proposed reading:* fine, and say so in the lab guide. The consumer talks the Kafka protocol either way, and a development stack that boots in seconds is worth more than fidelity to a deployment nobody is running here. The nightly CI job is the place to swap in real Kafka if the difference ever matters.

**10.6 The phase number.** This is "2.5" because it depends on Phase 2's consumer existing and blocks nothing in Phase 3. *Proposed reading:* keep the number; it is honest about being a detour. If it should instead be part of Phase 2's own scope, it merges cleanly — the deliverables do not change, only which spec they live in.
