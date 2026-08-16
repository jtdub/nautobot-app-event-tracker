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

import ipaddress
import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TOPOLOGY_FILE = HERE / "topology.clab.yml"

#: An address line in a node's startup configuration, which is where the fabric addressing is
#: defined:  `set / interface ethernet-1/1 subinterface 0 ipv4 address 10.1.1.0/31`
ADDRESS_LINE = re.compile(
    r"^set / interface (?P<interface>\S+) subinterface \d+ ipv4 address (?P<address>\S+)$",
    re.MULTILINE,
)

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
    topology = yaml.safe_load(TOPOLOGY_FILE.read_text(encoding="utf-8"))["topology"]
    nodes = topology["nodes"]
    links = topology.get("links", [])

    location, devices = _devices(nodes)
    interfaces = _interfaces(devices, links)
    managed = _assign_management_addresses(devices, nodes)
    fabric = _assign_fabric_addresses(devices)
    cables = _connect(devices, links)

    print(f"Location:   {location}")
    print(f"Devices:    {len(devices)} ({', '.join(sorted(devices))})")
    print(f"Interfaces: {interfaces}")
    print(f"Management: {managed} addresses assigned")
    print(f"Fabric:     {fabric} addresses assigned")
    print(f"Cables:     {cables} connected")


def _devices(nodes):
    """A Device for every topology node that is one, and the location they are all filed under."""
    from nautobot.dcim.models import Device  # pylint: disable=import-outside-toplevel

    from nautobot_event_tracker.dcim_fixtures import (  # pylint: disable=import-outside-toplevel
        default_status,
        ensure_device,
        ensure_device_type,
        ensure_location,
        ensure_role,
    )

    location = ensure_location(location_type_name=LOCATION_TYPE, location_name=LOCATION)
    common = {
        "location": location,
        "device_type": ensure_device_type(manufacturer_name=MANUFACTURER, model_name=DEVICE_TYPE),
        "role": ensure_role(role_name=DEVICE_ROLE),
        "status": default_status(Device),
    }
    devices = {name: ensure_device(name, **common)[0] for name, node in sorted(nodes.items()) if _is_a_device(node)}
    return location, devices


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
    """Give each device the management address the topology pins for it, as its primary IP.

    The topology pins every address rather than letting containerlab choose, so what Nautobot holds
    is what the lab will use on the next deploy as well as on this one.
    """
    from nautobot.dcim.models import Interface  # pylint: disable=import-outside-toplevel

    from nautobot_event_tracker.dcim_fixtures import (  # pylint: disable=import-outside-toplevel
        default_status,
        ensure_address,
        ensure_interface,
        ensure_prefix,
    )

    prefix = ensure_prefix(MANAGEMENT_PREFIX)
    interface_status = default_status(Interface)

    assigned = 0
    for name, device in devices.items():
        address = nodes[name].get("mgmt-ipv4")
        if not address:
            continue

        interface = ensure_interface(device=device, name=MANAGEMENT_INTERFACE, status=interface_status, mgmt_only=True)
        ensure_address(f"{address}/24", interface=interface, prefix=prefix, primary=True)
        assigned += 1
    return assigned


def _assign_fabric_addresses(devices):
    """Give each fabric interface the address its own startup configuration puts on it.

    Read out of the `.cli` files rather than written down a second time here. Those files are what
    the device actually runs, so a plan kept alongside them would be a plan that could disagree
    with the device - and an interface whose address in Nautobot is not the address on the wire is
    worse than an interface with no address at all.
    """
    from nautobot.dcim.models import Interface  # pylint: disable=import-outside-toplevel

    from nautobot_event_tracker.dcim_fixtures import (  # pylint: disable=import-outside-toplevel
        default_status,
        ensure_address,
        ensure_interface,
        ensure_prefix,
    )

    interface_status = default_status(Interface)
    prefixes = {}

    assigned = 0
    for name, device in sorted(devices.items()):
        for interface_name, address in sorted(startup_addresses(name).items()):
            network = str(ipaddress.ip_interface(address).network)
            if network not in prefixes:
                prefixes[network] = ensure_prefix(network)

            interface = ensure_interface(device=device, name=interface_name, status=interface_status)
            ensure_address(address, interface=interface, prefix=prefixes[network])
            assigned += 1
    return assigned


def startup_addresses(node):
    """`{interface name: address}` from a node's startup configuration, empty if it has none."""
    config = HERE / f"{node}.cli"
    if not config.is_file():
        return {}
    return dict(ADDRESS_LINE.findall(config.read_text(encoding="utf-8")))


def _connect(devices, links):
    """Cable the topology's links, so the fabric in Nautobot is the fabric that exists.

    Links to anything that is not a Device - the Alpine client - are skipped: a cable needs two
    terminations, and the client has no interface in Nautobot to be the second.
    """
    from nautobot_event_tracker.dcim_fixtures import ensure_cable  # pylint: disable=import-outside-toplevel

    connected = 0
    for link in links:
        ends = [endpoint.partition(":") for endpoint in link["endpoints"]]
        if len(ends) != 2 or any(node not in devices for node, _, _ in ends):
            continue

        terminations = [devices[node].interfaces.get(name=_interface_name(port)) for node, _, port in ends]
        if ensure_cable(*terminations) is not None:
            connected += 1
    return connected


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
