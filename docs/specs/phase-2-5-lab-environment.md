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
- **The estate it creates is the lab's fabric, node for node.** The same three devices, the same interface names, the same management and fabric addressing, the same links as cables. *An earlier draft invented a separate estate — `edge-rtr-01`, `core-sw-01`, Cisco-shaped interface names, addresses out of TEST-NET-1 — which shared three device names with the lab and agreed with it about nothing else. Running both left `leaf-01` holding whichever interfaces were created first, and tickets titled `Gi0/0/1 is down on leaf-01`, naming an interface the device you can log in to has never had.* Copying the lab's fabric means a ticket is about a device you can go and break, on an interface it really has, whether or not you have ever run the lab.
- **The two are held together by a test.** `test_lab_configuration.py` checks the command's fabric against `topology.clab.yml` and the nodes' own `.cli` files: the device names, each device's interfaces, the addresses on them, the management addresses the topology pins, and the links. They cannot drift apart quietly.
- **Every ticket names an interface its device has.** The title's interface is drawn from that host's own list rather than from the fabric's, so the first thing a reader clicks is not a dead end.
- **`--flush` deletes only what it created**, identified by a tag applied to every ticket, device, cable and address it makes, and never touches a ticket a person opened or a device the lab populated. Tickets are deleted before devices, so an attachment never briefly points at something gone. The location, device type, manufacturer and role are left behind as empty scaffolding, since somebody may have filed their own objects under them.

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

The lab guide gives the `docker exec … sr_cli` line for the first of these verbatim, so a developer can see a ticket appear without learning SR Linux CLI first. That line is the demo.

**Each node gets its own startup configuration** — `leaf-01.cli`, `leaf-02.cli`, `spine-01.cli` — rather than one shared by all three. They agree about where syslog goes and differ about everything else: the fabric addressing, the AS number, the router-id and the neighbours. A single shared file, which is where this started, gave every node the same router-id and no neighbours at all, so no session ever came up and the second row of the table above was a promise the lab could not keep.

**Cost, stated plainly.** Each SR Linux node wants roughly 1 GB of memory and takes tens of seconds to boot; three of them plus Redpanda, Nautobot, PostgreSQL and Redis is an 8 GB machine minimum, comfortably 12. containerlab manages network namespaces directly and so needs privileged Docker, which not every sandbox has — including, possibly, the one this spec was written in. A developer without the memory runs everything else in this repository exactly as before.

**containerlab is a compose service, not an installed binary.** *Revised: an earlier draft told the developer to install it with the upstream script, which is a Linux instruction — containerlab needs a Linux kernel, so on a Mac there is nothing to install and `invoke lab-up` simply failed.* The kernel that matters there is the Docker VM's, and containerlab documents being run as a container on the daemon that hosts the nodes. `development/docker-compose.containerlab.yml` is that, as configuration rather than as flags buried in a task: `privileged`, `network_mode: host`, `pid: host`, the Docker socket, and the repository mounted at its own path — its own, because containerlab hands the nodes' startup configurations to the daemon as bind mounts and the daemon resolves those paths itself. A profile keeps it out of `invoke start`; the lab tasks `run` it one command at a time. That works identically on Linux, where it also pins one version for everybody. The nodes themselves are native on Apple Silicon: SR Linux, Redpanda and Fluent Bit all publish `linux/arm64` images.

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

`development/containerlab/populate_nautobot.py`, run by path — the lab guide gives both the in-container and out-of-container forms.

It reads the topology file and creates: a Location for the lab, a Manufacturer and DeviceType for SR Linux, a Role, the Devices under the names they use in their own log messages, their Interfaces from the topology's links, the management addresses the topology pins, the fabric addresses, and a Cable for every link between two devices. Every object is created with `get_or_create`, so running it twice changes nothing. The topology pins every management address rather than letting containerlab choose one, which is why the script never has to ask a running lab what it assigned.

**The fabric addresses are read out of the nodes' own startup configurations**, not written down a second time in the script. Those `.cli` files are what the devices actually run; a plan kept alongside them is a plan that can disagree with the device, and an interface whose address in Nautobot is not the address on the wire is worse than an interface with no address at all.

**It is a script under `development/`, not a management command.** A script whose only subject is one particular containerlab topology is not something an operator of a ticketing app should find installed. What is *general* about it — "a Device and everything a Device requires" — is not lab knowledge at all, and lives in `nautobot_event_tracker/dcim_fixtures.py`, which the test data command, the test fixtures and this script all use. That chain is six `get_or_create` calls in a particular order, and Nautobot has tightened what a Device requires before; written out three times, the next tightening is found by whoever runs the lab, while they are demonstrating it. See 10.3.

The device names in Nautobot match the hostnames the devices put in their syslog messages. That is the whole reason to bother: it is what makes the Phase 4 enrichment resolver's job real rather than hypothetical, and until then it is what lets a person reading a ticket search for the device by name and find it.

## 7. Invoke tasks

Developers drive this development environment with `invoke`, and the lab is part of it. A step that exists only as a shell line in a document is a step somebody has to find, read and get right, and there is no reason for the lab to be the one part of the repository that works that way.

*A draft of this section had no tasks at all, on the grounds that each would be a shell line wrapped in Python. That argument was wrong about who pays: the wrapper is written once and read never, and the shell line is read every time.*

| Task | Does |
| --- | --- |
| `lab-up` | Deploy the topology, start the stack — broker, syslog bridge and consumer — wait for Nautobot, populate it from the topology |
| `lab-down` | Stop the stack and destroy the topology, in that order, leaving the ordinary dev stack able to start |
| `lab-inspect` | What containerlab thinks is running: the nodes, their kinds, their addresses |
| `lab-populate` | Section 6 on its own, for when the topology is already up |
| `lab-consumer` | `nautobot-server eventconsumer` against the lab configuration, in the foreground; `--dry-run` decides without writing |
| `lab-break` | Cause one of the section 4 events on purpose: `--event interface\|bgp\|unreachable\|drift`, and `--restore` to put it back |
| `lab-events` | Read the raw messages on `network.events`, so a developer can see what the bridge actually produced |
| `lab-console` | Start the Redpanda console for reading the topic in a browser |

`eventconsumer` and `send-test-event` are tasks in their own right, outside the lab section, because what they drive is Phase 2's rather than the lab's.

**The ordinary stack consumes too.** `invoke eventconsumer` on a plain `invoke start` used to fail with "no topics are configured", which is a correct error and a poor answer: the development stack already runs a Redis, and the Redis consumer exists precisely for this. It is now configured against it, reading the *lab's* topic configuration — same field map, same severity map, same dedup template — so `invoke start`, `invoke eventconsumer`, `invoke send-test-event` demonstrates the whole of Phase 2 on any machine, and what a developer learns about the field map there is true of the lab as well. Redis pub/sub keeps nothing, which is the honest difference from Kafka and the reason the lab is still worth its 8 GB.

`lab-up` prints, at the end, what to do next: the Nautobot URL, the command that causes an event, and the one that tears it all down. `lab-break` prints its own `sr_cli` line as it runs it, so a developer learns the command rather than being kept away from it.

**One switch, three ways to throw it.** The lab's compose overlay is added by `tasks.py` itself rather than by a `compose_files` entry, because the same decision also settles whether Nautobot loads the lab's ingestion configuration, and the two must agree: a stack started with the broker but without the configuration comes up with no topics to subscribe to. `lab: true` in `invoke.yml` — documented in `invoke.example.yml` — makes the lab the environment `invoke start` brings up, for somebody who works on ingestion. `EVENT_TRACKER_LAB=true` does it for one shell. The `lab-*` tasks set that variable for their own process, so `invoke lab-up` works with no configuration at all.

**The consumer runs as a service.** With the lab up, `invoke start` brings up Nautobot, the broker, the bridge *and* `nautobot-server eventconsumer` — its own process alongside Nautobot, which is how [ADR 0005](../decisions/0005-standalone-consumer-process.md) says it runs in production, so the development environment demonstrates the deployment rather than a simplification of it. `invoke logs -s consumer` is what it made of each message. `lab-consumer` is for watching it decide in the foreground, or for `--dry-run`; it stops the service for the duration, because two consumers in one group split the partitions and on a one-partition topic the one being watched would see nothing.

## 8. CI

**None of this runs anywhere in CI, on any trigger.** containerlab needs privileged Docker, the nodes need gigabytes, and boot takes minutes.

*Revised: this section proposed a `lab.yml` workflow, manual and nightly on the default branch, uploading the consumer log and ticket list as artifacts.* There is no such workflow. A nightly job is a build that fails while nobody is looking at it and is read, if at all, days later; the pull-request gate already covers everything in this phase that can be checked without a device — including `test_lab_configuration.py`, which holds the lab's field map to the shape its bridge is written to produce. What the lab proves beyond that is proved by a person running it deliberately, which is the only time its answer is worth anything.

## 9. Acceptance criteria

1. **Test data.** `nautobot-server generate_nautobot_event_tracker_test_data` populates a database with tickets in every status, each with a trail built through the service layer; `--flush` removes exactly what it created and nothing else; the same `--seed` twice produces the same tickets. A test asserts the command writes no ticket whose status was assigned directly, by checking every ticket has a `created` update and a status trail consistent with the workflow graph.
2. **The topology comes up.** `invoke lab-up` brings up four nodes, `invoke lab-inspect` reports them running, and `sr_cli "show network-instance default protocols bgp neighbor"` shows the fabric sessions established — on an Apple Silicon Mac as well as on Linux.
3. **Events reach the broker.** After shutting an interface, `rpk topic consume network.events` shows a message within thirty seconds.
4. **Events become tickets.** With `nautobot-server eventconsumer` running against the lab configuration, the same break produces a ticket whose event type, severity and title are what the operator would expect, and whose `payload` holds the parsed message.
5. **The field map matches reality.** No field in the lab's `field_map` resolves to `None` for a message the bridge actually produced. This is criterion 3 of the whole phase's purpose: if it fails, the Phase 2 defaults are wrong and this is how we found out.
6. **Recurrence works on real traffic.** Flapping an interface repeatedly produces one ticket with a rising event count, not many tickets — the S5 rule, tested against a device rather than a fixture.
7. **Nothing ships.** The wheel built from this branch contains no containerlab file, no compose overlay and no Fluent Bit configuration; the only packaged change is the test-data command.
8. **The ordinary stack is unaffected.** `invoke build`, `invoke start` and `invoke tests` behave exactly as before on a machine with no containerlab installed.

## 10. Open questions

**10.1 The resource cost.** Three SR Linux nodes want 8 GB of memory before Nautobot has started. *Proposed reading:* build it anyway, keep it opt-in, and document the requirement at the top of the lab guide. If the target developer machine is smaller, the topology drops to two nodes — one leaf and one spine — which still produces link-down and BGP-down events and loses only the multi-path story.

**10.2 SR Linux specifically.** It is free, publicly pullable, and produces genuinely representative logs. It is also one vendor, and a field map tuned to it may fit nothing else. *Proposed reading:* start with SR Linux because it is the one that costs nothing to run, and treat the Fluent Bit parser as the seam where a second vendor would be added. A lab with FRR alongside it would be cheaper and less representative; a lab with a licensed image would be neither.

**10.3 The population script is not a management command.** *Proposed reading:* keep it out of the app package for the reason in section 6. *Cost:* it is invoked more awkwardly, and it cannot be tested by the app's test suite the way a command could. *Settled, partly:* the half of it that is not lab knowledge — making a Device and everything a Device requires — did move into the package, as `dcim_fixtures`, because the test data command needed exactly the same thing. What stayed outside is the topology, its interface naming and its addresses, which is the half nobody would want installed.

**10.4 Fluent Bit as the bridge.** Alternatives are a syslog-ng container with a Kafka destination, or having SR Linux export gNMI to a collector instead of syslog. *Proposed reading:* Fluent Bit, because it is one small container doing exactly one job and its parser is a file a person can read. gNMI would be more modern and would produce structured data with no parser at all, which is worth revisiting if the syslog parser turns out to be the fragile part.

**10.5 Redpanda in development, Kafka in the ADR.** *Proposed reading:* fine, and say so in the lab guide. The consumer talks the Kafka protocol either way, and a development stack that boots in seconds is worth more than fidelity to a deployment nobody is running here. Swapping in real Kafka is an edit to one compose file on the day the difference matters.

**10.6 The phase number.** This is "2.5" because it depends on Phase 2's consumer existing and blocks nothing in Phase 3. *Proposed reading:* keep the number; it is honest about being a detour. If it should instead be part of Phase 2's own scope, it merges cleanly — the deliverables do not change, only which spec they live in.
