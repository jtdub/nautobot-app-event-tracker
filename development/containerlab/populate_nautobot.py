#!/usr/bin/env python
"""Mirror the containerlab topology into Nautobot.

Run it after `containerlab deploy`:

    NAUTOBOT_CONFIG=development/nautobot_config.py python development/containerlab/populate_nautobot.py

Idempotent: everything is `get_or_create`, so running it twice changes nothing. Run it again after
redeploying the lab and it will pick up whatever addresses containerlab assigned this time.

**This is a development script, not a management command.** A command in the app package ships in
the wheel, and a ticketing app that installs something creating Devices is not what an operator
signed up for. The cost is that it is invoked by path rather than by name, which for a script only
developers run is a fair trade. See the Phase 2.5 spec, open question 10.3.

Why bother at all: the device names here match the hostnames the devices put in their syslog
messages. That is what makes Phase 4's enrichment resolver a real problem rather than a
hypothetical one, and until then it is what lets a person reading a ticket search for the device by
name and find it.
"""

import json
import os
import subprocess  # noqa: S404
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TOPOLOGY_FILE = HERE / "topology.clab.yml"

LOCATION_TYPE = "Lab"
LOCATION = "containerlab"
MANUFACTURER = "Nokia"
DEVICE_TYPE = "SR Linux"
DEVICE_ROLE = "Lab Switch"
MANAGEMENT_PREFIX = "172.30.30.0/24"

#: containerlab prefixes every container with the topology name; Nautobot should hold the short
#: name, because that is what the device puts in its syslog messages.
CLAB_PREFIX = "clab-event-tracker-"


def main():
    """Create everything the topology describes, then say what was made."""
    topology = yaml.safe_load(TOPOLOGY_FILE.read_text(encoding="utf-8"))
    nodes = topology["topology"]["nodes"]
    links = topology["topology"].get("links", [])

    location = _location()
    device_type = _device_type()
    role = _role()
    addresses = _management_addresses(nodes)

    devices = {}
    for name, node in sorted(nodes.items()):
        if node.get("kind") != "nokia_srlinux":
            # The client is a plain Alpine container. It is not network equipment and does not
            # belong in DCIM; inventing a Device for it would be inventing inventory.
            continue
        devices[name] = _device(name, location, device_type, role)

    interfaces = _interfaces(devices, links)
    assigned = _assign_management_addresses(devices, addresses)

    print(f"Location:   {location}")
    print(f"Devices:    {len(devices)} ({', '.join(sorted(devices))})")
    print(f"Interfaces: {interfaces}")
    print(f"Management: {assigned} addresses assigned")


def _location():
    """The lab's location, and a location type that admits devices."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.dcim.models import Device, Location, LocationType

    location_type, _ = LocationType.objects.get_or_create(name=LOCATION_TYPE)
    location_type.content_types.add(ContentType.objects.get_for_model(Device))

    location, _ = Location.objects.get_or_create(
        name=LOCATION,
        defaults={"location_type": location_type, "status": _status(Location)},
    )
    return location


def _device_type():
    """One device type for the whole lab: every node runs the same image."""
    from nautobot.dcim.models import DeviceType, Manufacturer

    manufacturer, _ = Manufacturer.objects.get_or_create(name=MANUFACTURER)
    device_type, _ = DeviceType.objects.get_or_create(manufacturer=manufacturer, model=DEVICE_TYPE)
    return device_type


def _role():
    """A role the devices can hold."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.dcim.models import Device
    from nautobot.extras.models import Role

    role, _ = Role.objects.get_or_create(name=DEVICE_ROLE)
    role.content_types.add(ContentType.objects.get_for_model(Device))
    return role


def _device(name, location, device_type, role):
    """One device, under the name it uses in its own log messages."""
    from nautobot.dcim.models import Device

    device, _ = Device.objects.get_or_create(
        name=name,
        defaults={
            "device_type": device_type,
            "role": role,
            "location": location,
            "status": _status(Device),
        },
    )
    return device


def _interfaces(devices, links):
    """The interfaces the topology's links describe, on both of their ends."""
    from nautobot.dcim.models import Interface

    created = 0
    for link in links:
        for endpoint in link["endpoints"]:
            node, _, port = endpoint.partition(":")
            if node not in devices:
                continue
            _, made = Interface.objects.get_or_create(
                device=devices[node],
                name=_interface_name(port),
                defaults={"type": "1000base-t", "status": _status(Interface)},
            )
            created += int(made)
    return created


def _interface_name(port):
    """Turn containerlab's `e1-1` into SR Linux's own `ethernet-1/1`.

    The device logs the second form, and a ticket that names an interface Nautobot does not hold is
    a ticket nobody can follow.
    """
    if port.startswith("e") and "-" in port:
        card, _, index = port[1:].partition("-")
        return f"ethernet-{card}/{index}"
    return port


def _management_addresses(nodes):
    """The management address of each node, from the topology or from a running lab.

    The topology file is the source when it pins addresses, which this one does. `containerlab
    inspect` is the fallback for a topology that lets containerlab choose.
    """
    addresses = {name: node["mgmt-ipv4"] for name, node in nodes.items() if node.get("mgmt-ipv4")}
    if addresses:
        return addresses
    return _inspect_addresses()


def _inspect_addresses():
    """Ask a running lab what it assigned. Returns an empty map when the lab is not up."""
    try:
        output = subprocess.run(  # noqa: S603
            ["containerlab", "inspect", "--topo", str(TOPOLOGY_FILE), "--format", "json"],  # noqa: S607
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Could not ask containerlab for addresses ({error}); skipping management IPs.")
        return {}

    containers = json.loads(output)
    if isinstance(containers, dict):
        containers = containers.get("containers", [])
    return {
        container["name"].removeprefix(CLAB_PREFIX): container["ipv4_address"].split("/")[0]
        for container in containers
        if container.get("ipv4_address")
    }


def _assign_management_addresses(devices, addresses):
    """Give each device its management address, on a management interface."""
    from nautobot.dcim.models import Interface
    from nautobot.ipam.models import IPAddress, Namespace, Prefix

    namespace = Namespace.objects.get_or_create(name="Global")[0]
    prefix, _ = Prefix.objects.get_or_create(
        prefix=MANAGEMENT_PREFIX,
        namespace=namespace,
        defaults={"status": _status(Prefix), "type": "network"},
    )

    assigned = 0
    for name, device in devices.items():
        address = addresses.get(name)
        if not address:
            continue

        interface, _ = Interface.objects.get_or_create(
            device=device,
            name="mgmt0",
            defaults={"type": "1000base-t", "status": _status(Interface), "mgmt_only": True},
        )
        ip_address, _ = IPAddress.objects.get_or_create(
            address=f"{address}/24",
            parent=prefix,
            defaults={"status": _status(IPAddress)},
        )
        interface.ip_addresses.add(ip_address)

        if device.primary_ip4 != ip_address:
            device.primary_ip4 = ip_address
            device.validated_save()
        assigned += 1
    return assigned


def _status(model):
    """The `Active` status for this model, or whatever it has if there is no such thing."""
    from nautobot.extras.models import Status

    statuses = Status.objects.get_for_model(model)
    return statuses.filter(name="Active").first() or statuses.first()


if __name__ == "__main__":
    if not os.getenv("NAUTOBOT_CONFIG"):
        sys.exit("Set NAUTOBOT_CONFIG to your Nautobot configuration file first.")

    import nautobot

    nautobot.setup()
    main()
