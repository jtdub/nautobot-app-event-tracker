"""Ticket service layer.

This module is the only code in the app that writes to `EventTicket` or `TicketUpdate`. Views,
serializers, forms and test fixtures all call these functions; none of them assign `ticket.status`
or create a `TicketUpdate` directly. See ADR 0001.

Rules implemented here, referenced by number from the Phase 1 spec:

* **S1** Every mutating function runs in one transaction and writes exactly one TicketUpdate.
* **S2** Status changes follow `TICKET_STATUS_TRANSITIONS` and nothing else.
* **S3** An AI actor may not mutate a resolved or closed ticket.
* **S4** `source=human` requires a user; `source` in (ai, system) requires no user.
* **S5** Creation with a dedup key joins an existing open ticket instead of duplicating it.
"""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from nautobot_event_tracker.choices import (
    TERMINAL_STATUSES,
    TICKET_STATUS_TRANSITIONS,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.models import EventTicket, TicketUpdate
from nautobot_event_tracker.services.exceptions import (
    InvalidActorError,
    InvalidTransitionError,
    TicketImmutableError,
)

__all__ = [
    "add_comment",
    "assign",
    "attach_object",
    "create_ticket",
    "create_ticket_for_user",
    "detach_object",
    "get_allowed_transitions",
    "get_attachable_object_types",
    "get_related_objects",
    "set_severity",
    "transition",
]

ATTACHMENT_UPDATE_TYPES = (UpdateTypeChoices.OBJECT_ATTACHED, UpdateTypeChoices.OBJECT_DETACHED)


def get_attachable_object_types():
    """Return the configured allowlist of attachable object types as `app_label.model` strings."""
    app_config = settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {})
    return [str(entry).lower() for entry in app_config.get("attachable_object_types", [])]


def _validate_actor(source, user):
    """Rule S4: bind the acting source to the presence or absence of a user."""
    if source not in TicketSourceChoices.values():
        raise InvalidActorError(f"'{source}' is not a valid ticket source.")
    if source == TicketSourceChoices.HUMAN and user is None:
        raise InvalidActorError("A mutation with source 'human' requires the acting user.")
    if source in (TicketSourceChoices.AI, TicketSourceChoices.SYSTEM) and user is not None:
        raise InvalidActorError(f"A mutation with source '{source}' must not carry a user.")


def _check_mutable(ticket, source):
    """Rule S3: an AI actor may not mutate a resolved or closed ticket.

    Called before actor validation and before transition legality, so that an AI acting on a
    terminal ticket always sees this error rather than a downstream one.
    """
    if source == TicketSourceChoices.AI and ticket.status in TERMINAL_STATUSES:
        raise TicketImmutableError(
            f"Source 'ai' may not modify a ticket with status '{ticket.status}'. "
            "Resolved and closed tickets are immutable to AI actors."
        )


def _record(*, ticket, update_type, source, user=None, message="", **fields):
    """Write one TicketUpdate. The only place TicketUpdate rows are created."""
    update = TicketUpdate(
        ticket=ticket,
        update_type=update_type,
        source=source,
        user=user,
        message=message,
        **fields,
    )
    update.full_clean()
    update.save()
    return update


def _attached_keys(ticket):
    """Return the set of (content_type_id, object_id) currently attached to the ticket."""
    attached = set()
    updates = ticket.updates.filter(update_type__in=ATTACHMENT_UPDATE_TYPES).order_by("created")
    for update in updates:
        key = (update.related_object_type_id, update.related_object_id)
        if update.update_type == UpdateTypeChoices.OBJECT_ATTACHED:
            attached.add(key)
        else:
            attached.discard(key)
    return attached


def create_ticket(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    title,
    event_type,
    source,
    severity=None,
    description="",
    user=None,
    dedup_key="",
    payload=None,
    related_objects=None,
    occurred_at=None,
):
    """Create a ticket in `new`, or join an existing open ticket with the same dedup key (S5).

    Returns the `EventTicket`, which may be a pre-existing one.
    """
    _validate_actor(source, user)

    if not event_type.enabled:
        raise ValidationError(f"Event type '{event_type}' is disabled and cannot be used for new tickets.")

    occurred_at = occurred_at or timezone.now()
    severity = severity or event_type.default_severity

    with transaction.atomic():
        if dedup_key:
            existing = (
                EventTicket.objects.select_for_update()
                .filter(dedup_key=dedup_key)
                .exclude(status__in=TERMINAL_STATUSES)
                .order_by("-last_seen")
                .first()
            )
            if existing is not None:
                existing.event_count += 1
                # max() keeps last_seen monotonic under out-of-order delivery, which at-least-once
                # brokers make normal (ADR 0004).
                existing.last_seen = max(existing.last_seen, occurred_at)
                existing.full_clean()
                existing.save()
                _record(
                    ticket=existing,
                    update_type=UpdateTypeChoices.RECURRENCE,
                    source=source,
                    user=user,
                    message=f"Event recurred; this is occurrence {existing.event_count}.",
                )
                return existing

        ticket = EventTicket(
            title=title,
            event_type=event_type,
            status=TicketStatusChoices.NEW,
            severity=severity,
            source=source,
            description=description,
            dedup_key=dedup_key,
            event_count=1,
            first_seen=occurred_at,
            last_seen=occurred_at,
            payload=payload or {},
        )
        ticket.full_clean()
        ticket.save()

        _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.CREATED,
            source=source,
            user=user,
            message=f"Ticket opened with severity '{severity}'.",
        )

        for obj in related_objects or []:
            attach_object(ticket=ticket, obj=obj, source=source, user=user)

        return ticket


def create_ticket_for_user(  # pylint: disable=too-many-arguments
    *,
    user,
    title,
    event_type,
    severity=None,
    description="",
    dedup_key="",
    payload=None,
    assignee=None,
    tags=None,
):
    """Create a human-sourced ticket, applying optional assignment and tags.

    Shared by the REST and UI create paths so that "a person opened a ticket" has one definition
    and the two transports cannot drift apart.
    """
    ticket = create_ticket(
        title=title,
        event_type=event_type,
        source=TicketSourceChoices.HUMAN,
        user=user,
        severity=severity,
        description=description,
        dedup_key=dedup_key,
        payload=payload,
    )
    if assignee is not None:
        assign(ticket=ticket, assignee=assignee, source=TicketSourceChoices.HUMAN, user=user)
    if tags:
        ticket.tags.set(tags)
    return ticket


def add_comment(*, ticket, message, source, user=None):
    """Append a comment to the ticket."""
    _check_mutable(ticket, source)
    _validate_actor(source, user)

    if not message:
        raise ValidationError("A comment requires a message.")

    with transaction.atomic():
        return _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.COMMENT,
            source=source,
            user=user,
            message=message,
        )


def transition(*, ticket, to_status, source, user=None, message="", resolution=""):  # pylint: disable=too-many-arguments
    """Move the ticket along the workflow graph and record the change (S2)."""
    _check_mutable(ticket, source)
    _validate_actor(source, user)

    allowed = TICKET_STATUS_TRANSITIONS.get(ticket.status, frozenset())
    if to_status not in allowed:
        raise InvalidTransitionError(
            f"'{ticket.status}' -> '{to_status}' is not a permitted transition. "
            f"Permitted from '{ticket.status}': {sorted(allowed) or 'none, this status is terminal'}."
        )

    if to_status == TicketStatusChoices.RESOLVED and not resolution:
        raise ValidationError("Resolving a ticket requires a resolution.")

    from_status = ticket.status
    now = timezone.now()

    with transaction.atomic():
        ticket.status = to_status

        if to_status == TicketStatusChoices.RESOLVED:
            ticket.resolved_at = now
            ticket.resolution = resolution
        elif to_status == TicketStatusChoices.CLOSED:
            ticket.closed_at = now
            if ticket.resolved_at is None:
                ticket.resolved_at = now
            if resolution:
                ticket.resolution = resolution
            elif not ticket.resolution:
                ticket.resolution = f"Closed from '{from_status}' without a recorded resolution."
        elif from_status in TERMINAL_STATUSES:
            # Reopening: drop the terminal bookkeeping so C1 and C2 stay satisfied.
            ticket.resolved_at = None
            ticket.closed_at = None
            ticket.resolution = ""

        ticket.full_clean()
        ticket.save()

        return _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.STATUS_CHANGE,
            source=source,
            user=user,
            message=message,
            from_status=from_status,
            to_status=to_status,
        )


def attach_object(*, ticket, obj, source, user=None, message=""):
    """Attach a Nautobot object to the ticket.

    Returns `None` without writing a row when the object is already attached, so that a
    re-delivered event does not pollute the timeline.
    """
    _check_mutable(ticket, source)
    _validate_actor(source, user)

    content_type = ContentType.objects.get_for_model(obj)
    label = f"{content_type.app_label}.{content_type.model}"
    allowed_types = get_attachable_object_types()
    if label not in allowed_types:
        raise ValidationError(
            f"Objects of type '{label}' may not be attached to a ticket. "
            f"Permitted types: {', '.join(sorted(allowed_types)) or 'none configured'}."
        )

    if (content_type.pk, obj.pk) in _attached_keys(ticket):
        return None

    with transaction.atomic():
        return _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.OBJECT_ATTACHED,
            source=source,
            user=user,
            message=message or f"Attached {label} '{obj}'.",
            related_object_type=content_type,
            related_object_id=obj.pk,
        )


def detach_object(*, ticket, obj, source, user=None, message=""):
    """Detach an object from the ticket by appending an `object_detached` update.

    Returns `None` without writing a row when the object is not currently attached. Deliberately
    does not check the allowlist, so an object attached under an older configuration stays
    removable.
    """
    _check_mutable(ticket, source)
    _validate_actor(source, user)

    content_type = ContentType.objects.get_for_model(obj)
    if (content_type.pk, obj.pk) not in _attached_keys(ticket):
        return None

    label = f"{content_type.app_label}.{content_type.model}"
    with transaction.atomic():
        return _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.OBJECT_DETACHED,
            source=source,
            user=user,
            message=message or f"Detached {label} '{obj}'.",
            related_object_type=content_type,
            related_object_id=obj.pk,
        )


def assign(*, ticket, assignee, source, user=None):
    """Assign the ticket to a user, or unassign it with `assignee=None`.

    Returns `None` without writing a row when the assignment is unchanged.
    """
    _check_mutable(ticket, source)
    _validate_actor(source, user)

    current_id = ticket.assigned_to_id
    new_id = assignee.pk if assignee is not None else None
    if current_id == new_id:
        return None

    with transaction.atomic():
        ticket.assigned_to = assignee
        ticket.full_clean()
        ticket.save()

        message = f"Assigned to {assignee}." if assignee is not None else "Unassigned."
        return _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.ASSIGNMENT,
            source=source,
            user=user,
            message=message,
        )


def set_severity(*, ticket, severity, source, user=None):
    """Change the ticket's severity.

    Returns `None` without writing a row when the severity is unchanged.
    """
    _check_mutable(ticket, source)
    _validate_actor(source, user)

    if ticket.severity == severity:
        return None

    previous = ticket.severity
    with transaction.atomic():
        ticket.severity = severity
        ticket.full_clean()
        ticket.save()

        return _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.SEVERITY_CHANGE,
            source=source,
            user=user,
            message=f"Severity changed from '{previous}' to '{severity}'.",
        )


def get_allowed_transitions(ticket, *, source=TicketSourceChoices.HUMAN):
    """Return the statuses reachable from the ticket's current status for this source.

    Read-only. The UI and the API both call this rather than reading the graph themselves, so the
    graph keeps exactly one consumer path.
    """
    if source == TicketSourceChoices.AI and ticket.status in TERMINAL_STATUSES:
        return frozenset()
    return TICKET_STATUS_TRANSITIONS.get(ticket.status, frozenset())


def get_related_objects(ticket):
    """Return the objects currently attached to the ticket, grouped by content type.

    Groups are ordered by content type label, objects within a group by string representation.
    Rows whose target has since been deleted are skipped rather than raising.
    """
    by_type = {}
    for content_type_id, object_id in _attached_keys(ticket):
        by_type.setdefault(content_type_id, []).append(object_id)

    grouped = {}
    for content_type_id, object_ids in by_type.items():
        try:
            content_type = ContentType.objects.get_for_id(content_type_id)
        except ContentType.DoesNotExist:
            continue
        model = content_type.model_class()
        if model is None:
            continue
        objects = sorted(model.objects.filter(pk__in=object_ids), key=str)
        if objects:
            grouped[content_type] = objects

    return dict(sorted(grouped.items(), key=lambda item: str(item[0])))
