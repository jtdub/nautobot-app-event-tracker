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

Nothing else in this repository needs it. If you never run it, you lose nothing.

## What you need

| | |
| --- | --- |
| Memory | 8 GB free, comfortably 12. Each SR Linux node wants about 1 GB |
| Docker | **Privileged**, and able to manage network namespaces — containerlab does this directly |
| [containerlab](https://containerlab.dev) | `bash -c "$(curl -sL https://get.containerlab.dev)"` |
| Time | The nodes take tens of seconds each to boot |

The SR Linux image (`ghcr.io/nokia/srlinux`) is publicly pullable and needs no licence, which is
most of why this lab uses it.

If your machine is smaller, drop `leaf-02` and its links from the topology. Two nodes still produce
link-down and BGP-down events; you lose only the multi-path story.

## Bringing it up

**1. Add the broker overlay to your `invoke.yml`.** The lab's broker and syslog bridge are a compose
overlay, layered the way the redis one is:

```yaml
---
nautobot_event_tracker:
  nautobot_ver: "3.2.0"
  python_ver: "3.12"
  compose_files:
    - "docker-compose.base.yml"
    - "docker-compose.redis.yml"
    - "docker-compose.postgres.yml"
    - "docker-compose.dev.yml"
    - "docker-compose.redpanda.yml"
```

**2. Deploy the topology first.** The overlay attaches to the management network containerlab
creates, so the lab has to exist before the stack starts:

```shell
sudo containerlab deploy --topo development/containerlab/topology.clab.yml
```

**3. Start Nautobot with the lab's ingestion configuration.** `EVENT_TRACKER_LAB` is what points the
consumer at Redpanda and describes the payloads the bridge produces; without it the development
stack behaves exactly as it always has.

```shell
EVENT_TRACKER_LAB=true invoke start
```

**4. Mirror the topology into Nautobot**, so the devices in the tickets are devices you can click
on:

```shell
invoke exec --command "python /source/development/containerlab/populate_nautobot.py"
```

or, outside the container:

```shell
NAUTOBOT_CONFIG=development/nautobot_config.py python development/containerlab/populate_nautobot.py
```

It is idempotent — run it again after redeploying the lab and nothing changes. It creates a
Location, a Nokia SR Linux device type, one device per node under the name that device uses in its
own log messages, the interfaces its links describe, and the management addresses the topology pins.

**5. Optionally, fill the ticket list**, so there is something to look at before you have broken
anything:

```shell
invoke generate-test-data
```

Fifty tickets across every status, each with a trail and attached to the device its title names. It
creates its own demo devices too, but having populated Nautobot from the topology in step 4 it will
use those instead where the names match — so `leaf-01` in a ticket is the `leaf-01` you can shut an
interface on.

`invoke generate-test-data --flush` deletes everything a previous run made and then generates a
fresh set — that is how to re-run it without ending up with a hundred tickets. To clean up instead
of regenerating, `invoke generate-test-data --flush --count 0` deletes them and makes nothing.
Either way, a ticket or a device you made yourself is untagged and is left alone.

**6. Run the consumer**, in the foreground where you can watch it:

```shell
invoke exec --command "nautobot-server eventconsumer"
```

## Making something happen

Shut an interface on a leaf:

```shell
docker exec -it clab-event-tracker-leaf-01 sr_cli \
  "enter candidate" \
  "set / interface ethernet-1/1 admin-state disable" \
  "commit now"
```

Within a second or two the consumer logs a message and a ticket appears under **Apps → Event Tracker
→ Tickets**, titled with what the device actually said. Bring it back with `admin-state enable`.

The BGP events need the fabric sessions to be up, which takes a minute or so after the nodes boot.
Each node has its own startup configuration (`leaf-01.cli` and its siblings) carrying its AS number,
its router-id and its neighbours; check they came up before blaming the pipeline:

```shell
docker exec -it clab-event-tracker-leaf-01 sr_cli \
  "show network-instance default protocols bgp neighbor"
```

Other events worth causing, all of which the seeded event catalogue has a type for:

| What you do | What you get |
| --- | --- |
| Shut an interface, as above | **Interface Down** |
| Shut the peer's side of a link | **BGP Session Down** |
| `docker stop clab-event-tracker-leaf-02` | **Device Unreachable** on its neighbours |
| Any `commit now` on a device | **Configuration Drift** |

Flap the same interface a few times: you should get **one** ticket with a rising event count, not
one per flap. That is rule S5 working against a real device rather than a fixture.

## Watching what happened

- **Apps → Event Tracker → Ingestion Stats** — what the consumer received, opened, joined,
  suppressed and dropped, and which rule dropped it.
- The raw messages on the broker, when the field map does not match and you need to see why:

  ```shell
  docker compose --project-name nautobot-event-tracker \
    --project-directory development \
    -f development/docker-compose.redpanda.yml --profile console up -d redpanda-console
  ```

  Then open <http://localhost:8090> and read the `network.events` topic. Worth a great deal the first
  time something does not line up, and nothing afterwards.

- Or from the command line:

  ```shell
  docker exec -it nautobot-event-tracker-redpanda-1 rpk topic consume network.events --num 5
  ```

## Tuning the filters against real traffic

```shell
invoke exec --command "nautobot-server eventconsumer --dry-run"
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

**Nothing arrives at all.** Check in order: is the consumer running; does **Ingestion Stats** show
anything received; does the console show messages on the topic; is Fluent Bit logging
(`docker logs nautobot-event-tracker-fluent-bit-1`); does the device have the remote server
configured (`docker exec -it clab-event-tracker-leaf-01 sr_cli "info / system logging"`).

## Tearing it down

```shell
invoke stop
sudo containerlab destroy --topo development/containerlab/topology.clab.yml --cleanup
```

The devices Nautobot holds are not removed with the lab — they are ordinary DCIM objects, and the
tickets that point at them stay meaningful. Delete them by hand if you want the database clean.

## What this lab does not do

It is not part of continuous integration. containerlab needs privileged Docker and gigabytes of
memory, and boot takes minutes; putting that in front of every pull request would buy a signal the
unit tests already give and cost flakiness unrelated to the change under review. Run it when you
change ingestion, or when you want to see the app work on something real.
