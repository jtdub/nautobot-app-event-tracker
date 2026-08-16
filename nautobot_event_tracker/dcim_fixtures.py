"""Creating the DCIM objects a demo or a lab needs, in one place.

Three callers need "a Device and everything a Device requires": the test data command, the
containerlab population script, and the test fixtures. The chain is six `get_or_create` calls in a
particular order, and Nautobot has tightened its requirements before - 2.x gave `Role` content
types and made `Location.status` mandatory. Written out three times, that kind of change is found
by whoever runs the lab, at the moment they are demonstrating it.

Nothing here is lab-specific: every name is a parameter. The lab's knowledge - the topology, its
interface naming, its management addresses - stays in `development/`, which may import this because
the dependency runs that way round.
"""

from django.contrib.contenttypes.models import ContentType
from nautobot.apps.choices import InterfaceTypeChoices, PrefixTypeChoices
from nautobot.dcim.models import Cable, Device, DeviceType, Interface, Location, LocationType, Manufacturer
from nautobot.extras.models import Role, Status
from nautobot.ipam.models import IPAddress, Namespace, Prefix


def default_status(model):
    """The status a demo object of this model gets: `Active`, or whatever the model has.

    One rule, rather than three spellings of it. `Status.objects.get_for_model(...).first()` agrees
    only by accident - statuses order by name, so `Active` sorts first among the stock ones and
    stops doing so the day somebody adds `Aborted`.
    """
    statuses = Status.objects.get_for_model(model)
    return statuses.filter(name="Active").first() or statuses.first()


def ensure_location(*, location_type_name, location_name):
    """A location that admits devices, and the type it needs."""
    location_type, _ = LocationType.objects.get_or_create(name=location_type_name)
    location_type.content_types.add(ContentType.objects.get_for_model(Device))
    location, _ = Location.objects.get_or_create(
        name=location_name,
        defaults={"location_type": location_type, "status": default_status(Location)},
    )
    return location


def ensure_device_type(*, manufacturer_name, model_name):
    """A device type and its manufacturer."""
    manufacturer, _ = Manufacturer.objects.get_or_create(name=manufacturer_name)
    device_type, _ = DeviceType.objects.get_or_create(manufacturer=manufacturer, model=model_name)
    return device_type


def ensure_role(*, role_name):
    """A role a device can hold."""
    role, _ = Role.objects.get_or_create(name=role_name)
    role.content_types.add(ContentType.objects.get_for_model(Device))
    return role


def ensure_device(name, *, location, device_type, role, status=None):
    """One device. Returns it and whether this call created it.

    The flag matters to callers that mark what they own: a device somebody else made is not theirs
    to tag, and therefore not theirs to delete.
    """
    device, created = Device.objects.get_or_create(
        name=name,
        defaults={
            "device_type": device_type,
            "role": role,
            "location": location,
            "status": status if status is not None else default_status(Device),
        },
    )
    return device, created


def ensure_interface(*, device, name, status=None, mgmt_only=False):
    """One interface on a device."""
    interface, _ = Interface.objects.get_or_create(
        device=device,
        name=name,
        defaults={
            "type": InterfaceTypeChoices.TYPE_1GE_FIXED,
            "status": status if status is not None else default_status(Interface),
            "mgmt_only": mgmt_only,
        },
    )
    return interface


def ensure_prefix(prefix, *, namespace_name="Global"):
    """The prefix an address is filed under. Nautobot requires one before it will hold the address."""
    namespace, _ = Namespace.objects.get_or_create(name=namespace_name)
    prefix, _ = Prefix.objects.get_or_create(
        prefix=prefix,
        namespace=namespace,
        defaults={"status": default_status(Prefix), "type": PrefixTypeChoices.TYPE_NETWORK},
    )
    return prefix


def ensure_address(address, *, interface, prefix, status=None, primary=False):
    """One address, on one interface, optionally the device's primary.

    A device whose interfaces hold no addresses is a device nobody can reach from Nautobot, and a
    ticket about it cannot answer the first question anybody asks: what is its IP.
    """
    ip_address, _ = IPAddress.objects.get_or_create(
        address=address,
        parent=prefix,
        defaults={"status": status if status is not None else default_status(IPAddress)},
    )
    interface.ip_addresses.add(ip_address)

    device = interface.device
    if primary and device.primary_ip4 != ip_address:
        device.primary_ip4 = ip_address
        device.validated_save()
    return ip_address


def ensure_cable(interface_a, interface_b, status=None):
    """The cable between two interfaces, if neither is already cabled.

    Returns the cable, or `None` when either end is already occupied - which is what a second run
    finds, and also what somebody else's cable looks like. Neither is ours to replace.
    """
    if interface_a.cable is not None or interface_b.cable is not None:
        return None

    cable = Cable(
        termination_a=interface_a,
        termination_b=interface_b,
        status=status if status is not None else _cable_status(),
    )
    cable.validated_save()
    return cable


def _cable_status():
    """`Connected`, or whatever the installation calls a cable that is in service."""
    statuses = Status.objects.get_for_model(Cable)
    return statuses.filter(name="Connected").first() or statuses.first()
