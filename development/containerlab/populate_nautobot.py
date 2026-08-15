#!/usr/bin/env python
"""Mirror the containerlab topology into Nautobot.

Run it after `containerlab deploy`:

    NAUTOBOT_CONFIG=development/nautobot_config.py python development/containerlab/populate_nautobot.py

Idempotent: everything is `get_or_create`, so running it twice changes nothing. Run it again after
redeploying the lab and it will pick up whatever addresses the topology pins.

**This is a development script, not a management command.** A command in the app package ships in
the wheel, and a script whose only subject is one particular containerlab topology is not something
an operator installed a ticketing app to get. What is general - "a Device and everything a Device
requires" - lives in `nautobot_event_tracker.dcim_fixtures`, which this imports; the lab's own
knowledge, the topology and its interface naming, stays here. See the Phase 2.5 spec, question 10.3.

Why bother at all: the device names here match the hostnames the devices put in their syslog
messages. That is what makes Phase 4's enrichment resolver a real problem rather than a
hypothetical one, and until then it is what lets a person reading a ticket search for the device by
name and find it.

Django models are imported inside the functions that use them: this file is run as a script, and
until `nautobot.setup()` at the bottom has run there is no configured Django to import them from.
"""

import os
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
MANAGEMENT_INTERFACE = "mgmt0"

#: Only these become Devices. The client is a plain Alpine container; it is not network equipment,
#: and inventing a Device for it would be inventing inventory.
DEVICE_KIND = "nokia_srlinux"


def main():
    """Create everything the topology describes, then say what was made."""
    from nautobot.dcim.models import Device  # pylint: disable=import-outside-toplevel

    from nautobot_event_tracker.dcim_fixtures import (  # pylint: disable=import-outside-toplevel
        default_status,
        ensure_device,
        ensure_device_type,
        ensure_location,
        ensure_role,
    )

    topology = yaml.safe_load(TOPOLOGY_FILE.read_text(encoding="utf-8"))["topology"]
    nodes = topology["nodes"]

    location = ensure_location(location_type_name=LOCATION_TYPE, location_name=LOCATION)
    common = {
        "location": location,
        "device_type": ensure_device_type(manufacturer_name=MANUFACTURER, model_name=DEVICE_TYPE),
        "role": ensure_role(role_name=DEVICE_ROLE),
        "status": default_status(Device),
    }
    devices = {name: ensure_device(name, **common)[0] for name, node in sorted(nodes.items()) if _is_a_device(node)}

    interfaces = _interfaces(devices, topology.get("links", []))
    assigned = _assign_management_addresses(devices, nodes)

    print(f"Location:   {location}")
    print(f"Devices:    {len(devices)} ({', '.join(sorted(devices))})")
    print(f"Interfaces: {interfaces}")
    print(f"Management: {assigned} addresses assigned")


def _is_a_device(node):
    """Whether this topology node belongs in DCIM."""
    return node.get("kind") == DEVICE_KIND


def _interfaces(devices, links):
    """The interfaces the topology's links describe, on both of their ends."""
    from nautobot.dcim.models import Interface  # pylint: disable=import-outside-toplevel

    from nautobot_event_tracker.dcim_fixtures import default_status, ensure_interface  # pylint: disable=C0415

    endpoints = {
        (node, _interface_name(port))
        for link in links
        for node, _, port in (endpoint.partition(":") for endpoint in link["endpoints"])
        if node in devices
    }

    status = default_status(Interface)
    for node, name in sorted(endpoints):
        ensure_interface(device=devices[node], name=name, status=status)
    return len(endpoints)


def _interface_name(port):
    """Turn containerlab's `e1-1` into SR Linux's own `ethernet-1/1`.

    The device logs the second form, and a ticket that names an interface Nautobot does not hold is
    a ticket nobody can follow. Anything not shaped like `e<card>-<index>` is left alone: it is
    already the name its owner uses.
    """
    card, dash, index = port.removeprefix("e").partition("-")
    if dash and card.isdigit() and index.isdigit():
        return f"ethernet-{card}/{index}"
    return port


def _assign_management_addresses(devices, nodes):
    """Give each device the management address the topology pins for it.

    The topology pins every address rather than letting containerlab choose, so what Nautobot holds
    is what the lab will use on the next deploy as well as on this one.
    """
    from nautobot.dcim.models import Interface  # pylint: disable=import-outside-toplevel
    from nautobot.ipam.models import IPAddress  # pylint: disable=import-outside-toplevel

    from nautobot_event_tracker.dcim_fixtures import default_status, ensure_interface  # pylint: disable=C0415

    prefix = _management_prefix()
    interface_status = default_status(Interface)
    address_status = default_status(IPAddress)

    assigned = 0
    for name, device in devices.items():
        address = nodes[name].get("mgmt-ipv4")
        if not address:
            continue

        interface = ensure_interface(device=device, name=MANAGEMENT_INTERFACE, status=interface_status, mgmt_only=True)
        ip_address, _ = IPAddress.objects.get_or_create(
            address=f"{address}/24",
            parent=prefix,
            defaults={"status": address_status},
        )
        interface.ip_addresses.add(ip_address)

        if device.primary_ip4 != ip_address:
            device.primary_ip4 = ip_address
            device.validated_save()
        assigned += 1
    return assigned


def _management_prefix():
    """The management network the topology's addresses live in."""
    from nautobot.apps.choices import PrefixTypeChoices  # pylint: disable=import-outside-toplevel
    from nautobot.ipam.models import Namespace, Prefix  # pylint: disable=import-outside-toplevel

    from nautobot_event_tracker.dcim_fixtures import default_status  # pylint: disable=import-outside-toplevel

    namespace, _ = Namespace.objects.get_or_create(name="Global")
    prefix, _ = Prefix.objects.get_or_create(
        prefix=MANAGEMENT_PREFIX,
        namespace=namespace,
        defaults={"status": default_status(Prefix), "type": PrefixTypeChoices.TYPE_NETWORK},
    )
    return prefix


if __name__ == "__main__":
    import nautobot
    from nautobot.core.cli import get_config_path

    # Whatever `nautobot-server` itself would use: NAUTOBOT_CONFIG, or the configuration under
    # NAUTOBOT_ROOT - which is what makes this runnable inside the development container, where
    # nothing sets NAUTOBOT_CONFIG and the file is at /opt/nautobot/nautobot_config.py.
    if not os.path.exists(get_config_path()):
        sys.exit(f"No Nautobot configuration at {get_config_path()}. Set NAUTOBOT_CONFIG and try again.")

    nautobot.setup()
    main()
