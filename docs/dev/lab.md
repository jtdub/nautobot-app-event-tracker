# The containerlab lab

A three-node SR Linux fabric whose logs are real, a broker, and a bridge between them. Break a link
on a device and watch a ticket appear.

## Why it exists

Phase 2's ingestion is built on a guess. The field maps, the severity map and the dedup key template
were written from what a syslog payload *usually* looks like, not from anything a device sent. A
fixture can only assert that the code does what its author believed; it cannot discover that SR
Linux words a severity differently, or that one link flap produces eleven messages rather than one.

This lab is what turns the guess into evidence. It is also the only way to exercise the Kafka
consumer against a real broker rather than the stub the unit tests use.

Nothing else in this repository needs it. If all you want is to watch an event become a ticket, the
ordinary development stack consumes from its own Redis and `invoke send-test-event` publishes to it
— see [the development environment guide](dev_environment.md). What that cannot tell you is whether
a real SR Linux says any of what the field map expects, which is the whole of this.

## What you need

| | |
| --- | --- |
| Memory | 8 GB free, comfortably 12. Each SR Linux node wants about 1 GB |
| Docker | That is all. Docker Desktop, OrbStack, Colima or a Linux daemon |
| Time | The nodes take tens of seconds each to boot |

**containerlab is not installed; it is run from its own image.** It needs a Linux kernel — it makes
network namespaces and veth pairs directly — so on macOS there is nothing to install anyway: the
kernel that matters is the one inside the Docker VM, and `invoke lab-up` reaches it by running
containerlab in a container on that daemon, with `--privileged --network host --pid host` and the
Docker socket, which is containerlab's own documented way of being run this way. On Linux it costs
nothing and means every developer runs the same version.

**Apple Silicon works.** SR Linux publishes `linux/arm64` images, so the nodes run natively rather
than under emulation. So do Redpanda and Fluent Bit.

The SR Linux image (`ghcr.io/nokia/srlinux`) is publicly pullable and needs no licence, which is
most of why this lab uses it.

If your machine is smaller, drop `leaf-02` and its links from the topology. Two nodes still produce
link-down and BGP-down events; you lose only the multi-path story.

## Bringing it up

```shell
invoke lab-up
```

That is the whole thing. It deploys the topology, starts the development stack with the lab's broker,
its syslog bridge and **a running event consumer**, waits for Nautobot, and mirrors the topology into
it — so the devices named in the tickets are devices you can click on. Add `--test-data` to fill the
ticket list at the same time.

The consumer runs as a service, the way it runs in production: its own process alongside Nautobot,
rather than inside it. `invoke logs -s consumer` is what it made of each message.

**Turning it on for good.** `lab-up` needs no configuration — it sets `EVENT_TRACKER_LAB` for its own
run. If you work on ingestion often enough to want the lab to be your normal environment, copy
`invoke.example.yml` to `invoke.yml` and set:

```yaml
---
nautobot_event_tracker:
  lab: true
```

Then plain `invoke start`, `invoke logs`, `invoke exec` and `invoke stop` all include the broker, the
bridge and the consumer, and Nautobot loads the lab's ingestion configuration. For one shell instead
of for good, `export EVENT_TRACKER_LAB=true` does the same thing. Don't also add
`docker-compose.redpanda.yml` to `compose_files`: the setting adds it, and the compose file on its
own would give you a broker Nautobot has not been told about.

Either works with or without the topology deployed — the management network the two share is
created by whichever of them gets there first — but a broker with no devices sending to it is only
useful with `invoke send-test-event`.

**The steps, if one of them fails.** `lab-up` is these in order, and each is a task of its own:

| | |
| --- | --- |
| `invoke lab-populate` | Mirror the topology into Nautobot: a Location, a Nokia SR Linux device type, one device per node under the name it uses in its own log messages, the interfaces its links describe, their addresses — management and fabric — and a cable for every link. Idempotent; run it again after redeploying |
| `invoke generate-test-data` | Fifty tickets across every status, each with a trail and attached to the device its title names |
| `invoke lab-consumer` | The consumer in the foreground, where you can watch it decide. It stops the consumer service for the duration and starts it again afterwards — two consumers in one group split the partitions, and on a one-partition topic the one you are watching would see nothing |

`generate-test-data` creates **this same fabric** when it is missing — the same three devices, the
same interfaces, the same addresses, the same links — and uses what is already there when it is not.
So `leaf-01` in a ticket is the `leaf-01` you can shut an interface on, `ethernet-1/2` in a title is
an interface it really has, and the lab's own devices are never tagged, cabled or deleted by it.
Running it before the lab and populating afterwards gives exactly the same database as the other way
round; `test_lab_configuration.py` is what keeps that true.

`invoke generate-test-data --flush` deletes everything a previous run made and then generates a
fresh set — that is how to re-run it without ending up with a hundred tickets. To clean up instead
of regenerating, `invoke generate-test-data --flush --count 0` deletes them and makes nothing.
Either way, a ticket or a device you made yourself is untagged and is left alone.

## Making something happen

```shell
invoke lab-break
```

Which shuts `ethernet-1/1` on `leaf-01`, printing the `sr_cli` line it runs — worth reading, because
it is how you cause the next one without asking. Within a second or two the consumer logs a message
and a ticket appears under **Apps → Event Tracker → Tickets**, titled with what the device actually
said. `invoke lab-break --restore` puts it back.

| What you run | What you get |
| --- | --- |
| `invoke lab-break` | **Interface Down**, and once BGP is up, **BGP Session Down** with it |
| `invoke lab-break --event bgp --device spine-01` | **BGP Session Down** from the other end |
| `invoke lab-break --event unreachable --device leaf-02` | **Device Unreachable** on its neighbours |
| `invoke lab-break --event drift` | **Configuration Drift** |

`--device` and `--interface` choose where; every one of them takes `--restore`.

`invoke lab-inspect` lists what containerlab has running, with each node's address, when you want to
know whether the topology is up before blaming anything downstream of it.

The BGP events need the fabric sessions to be up, which takes a minute or so after the nodes boot.
Each node has its own startup configuration (`leaf-01.cli` and its siblings) carrying its AS number,
its router-id and its neighbours; check they came up before blaming the pipeline:

```shell
docker exec -it clab-event-tracker-leaf-01 sr_cli \
  "show network-instance default protocols bgp neighbor"
```

Flap the same interface a few times: you should get **one** ticket with a rising event count, not
one per flap. That is rule S5 working against a real device rather than a fixture.

## Watching what happened

- **Apps → Event Tracker → Ingestion Stats** — what the consumer received, opened, joined,
  suppressed and dropped, and which rule dropped it.
- `invoke lab-events` — the raw messages on the broker, for when the field map does not match and
  you need to see what the bridge really sent. `--follow` keeps reading as they arrive.
- `invoke lab-console` — the same thing in a browser at <http://localhost:8090>. Worth a great deal
  the first time something does not line up, and nothing afterwards.

## Tuning the filters against real traffic

```shell
invoke lab-consumer --dry-run
```

Every message is decided and reported, and nothing is written or acknowledged — so the consumer
group's offsets do not move and the same messages are still there afterwards. This is how to work
out why something is or is not becoming a ticket without filling the database while you do it.

## When the tickets look wrong

The path a message takes is: **device → syslog → Fluent Bit → Redpanda → consumer → ticket**, and
three files decide what comes out.

| File | Decides |
| --- | --- |
| `development/containerlab/parsers.conf` | How a syslog line is taken apart |
| `development/containerlab/classify.lua` | Which event type a message text implies, and the interface it names |
| `development/containerlab/nautobot_config_lab.py` | How the resulting JSON maps onto a ticket |

**Every ticket says Unclassified.** `classify.lua` did not recognise the message text. Read the raw
message in the console and add a pattern there — not a rule in the app's configuration, which is
downstream of the problem.

**Tickets have empty or strange titles.** The field map and the bridge disagree about the payload
shape. `nautobot_event_tracker/tests/test_lab_configuration.py` asserts they agree about the shape
the bridge is *written* to produce; if that passes and the tickets are still wrong, the bridge is not
producing what it claims, and the console will show you what it really sends.

**Every event opens its own ticket.** The dedup key template is not resolving: one of `event.type`,
`host` or `interface` is missing from the payload altogether. The consumer logs a warning about this
once per topic per five minutes, naming the template.

**Unrelated events join one ticket.** The opposite symptom, and the more likely one, because
`classify.lua` fills `interface` in with an empty string when the message names no interface —
deliberately, so that the key still resolves. Every interface-less event of one type on one host
therefore joins a single ticket. If you want them apart, give the template a field that
distinguishes them rather than removing `interface`, which brings back the symptom above.

**Nothing arrives at all.** Check in order: is the consumer running (`invoke logs -s consumer`); does
**Ingestion Stats** show anything received; does `invoke lab-events` show messages on the topic; is
Fluent Bit logging (`invoke logs -s fluent-bit`); does the device have the remote server configured
(`docker exec -it clab-event-tracker-leaf-01 sr_cli "info / system logging"`).

**The consumer is reading Redis rather than the broker.** Nautobot came up without
`EVENT_TRACKER_LAB`, so it loaded the ordinary development ingestion configuration — which happens
if the stack was already running before `invoke lab-up`, or if `docker-compose.redpanda.yml` is in
your `compose_files` but `lab: true` is not set. `invoke lab-down && invoke lab-up` fixes it.

**`network event-tracker-mgmt declared as external, but could not be found`.** Something ran
`docker compose` directly rather than through `invoke`, before either containerlab or the tasks had
created that network. `docker network create --subnet 172.30.30.0/24 event-tracker-mgmt`, or just
use `invoke start`, which does it for you.

## Tearing it down

```shell
invoke lab-down
```

Which stops the stack and then destroys the topology, in that order — compose's containers sit on
containerlab's management network, and containerlab cannot remove a network something is still
attached to. Add `--volumes` to discard the database with it.

Without `--volumes`, the devices Nautobot holds survive: they are ordinary DCIM objects, and the
tickets that point at them stay meaningful. Delete them by hand if you want the database clean.

## What this lab does not do

It is not part of continuous integration. containerlab needs privileged Docker and gigabytes of
memory, and boot takes minutes; putting that in front of every pull request would buy a signal the
unit tests already give and cost flakiness unrelated to the change under review. Run it when you
change ingestion, or when you want to see the app work on something real.
