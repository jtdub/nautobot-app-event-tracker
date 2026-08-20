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
from django.db import connection, transaction
from django.utils import timezone

from nautobot_event_tracker.choices import (
    ATTACHMENT_UPDATE_TYPES,
    TERMINAL_STATUSES,
    TICKET_STATUS_TRANSITIONS,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
    shortest_transition_path,
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
    "content_type_label",
    "content_types_from_labels",
    "create_ticket",
    "create_ticket_for_user",
    "detach_object",
    "get_allowed_transitions",
    "get_attachable_content_types",
    "get_attachable_object_types",
    "get_related_objects",
    "join_ticket",
    "resolve_object",
    "set_severity",
    "ticket_ids_with_attached_types",
    "transition",
]


def content_type_label(content_type):
    """Render a ContentType as the `app_label.model` string this app uses everywhere."""
    return f"{content_type.app_label}.{content_type.model}"


def get_attachable_object_types():
    """Return the configured allowlist of attachable object types as `app_label.model` strings."""
    app_config = settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {})
    return [str(entry).lower() for entry in app_config.get("attachable_object_types", [])]


def content_types_from_labels(labels):
    """Return a ContentType queryset for these `app_label.model` strings, skipping unknown ones.

    Unknown labels are skipped rather than raising: both callers - the configured allowlist and a
    filter's query string - are better served by ignoring a stale entry than by failing outright.
    """
    pks = []
    for label in labels:
        app_label, _, model = str(label).lower().partition(".")
        try:
            pks.append(ContentType.objects.get_by_natural_key(app_label, model).pk)
        except ContentType.DoesNotExist:
            continue
    return ContentType.objects.filter(pk__in=pks).order_by("app_label", "model")


def get_attachable_content_types():
    """Return the allowlist as a ContentType queryset.

    The allowlist is configured as strings, but forms and filters need ContentType objects. Doing
    the conversion here keeps the label format known to one module.
    """
    return content_types_from_labels(get_attachable_object_types())


def resolve_object(object_type, object_id):
    """Turn an `app_label.model` string (or a ContentType) and an ID into a model instance.

    Raises `ValidationError` when the type is unknown or no such object exists, so that every
    transport reports the same thing for the same mistake.
    """
    if isinstance(object_type, ContentType):
        content_type = object_type
    else:
        app_label, _, model = str(object_type).lower().partition(".")
        try:
            content_type = ContentType.objects.get_by_natural_key(app_label, model)
        except ContentType.DoesNotExist as error:
            raise ValidationError(f"Unknown object type '{object_type}'.") from error

    model_class = content_type.model_class()
    if model_class is None:
        raise ValidationError(f"Object type '{content_type_label(content_type)}' has no model.")

    obj = model_class.objects.filter(pk=object_id).first()
    if obj is None:
        raise ValidationError(f"No {content_type_label(content_type)} with ID {object_id}.")
    return obj


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


def _replay_attachments(rows):
    """Fold ordered (ticket, type, object, update_type) rows into the set still attached.

    This is the attachment-derivation rule of spec 3.4, and the only implementation of it. Callers
    supply the scope; the rule lives here.
    """
    attached = set()
    for ticket_id, content_type_id, object_id, update_type in rows:
        key = (ticket_id, content_type_id, object_id)
        if update_type == UpdateTypeChoices.OBJECT_ATTACHED:
            attached.add(key)
        else:
            attached.discard(key)
    return attached


def _attachment_rows(queryset):
    """Order an attach/detach queryset and reduce it to the four columns the replay reads."""
    return (
        queryset.filter(update_type__in=ATTACHMENT_UPDATE_TYPES)
        .order_by("created")
        .values_list("ticket_id", "related_object_type_id", "related_object_id", "update_type")
    )


def _attached_keys(ticket):
    """Return the set of (content_type_id, object_id) currently attached to the ticket."""
    rows = _attachment_rows(ticket.updates)
    return {(content_type_id, object_id) for _, content_type_id, object_id in _replay_attachments(rows)}


def ticket_ids_with_attached_types(queryset, content_types):
    """Return the IDs of tickets in `queryset` currently holding an object of one of these types.

    Scoped to the queryset so the replay reads only the rows that could matter, rather than every
    attachment row in the database.
    """
    if not content_types:
        return set()
    rows = _attachment_rows(
        TicketUpdate.objects.filter(
            ticket__in=queryset.values("pk"),
            related_object_type__in=content_types,
        )
    )
    return {ticket_id for ticket_id, _, _ in _replay_attachments(rows)}


def effective_severity(severity, event_type):
    """The severity a ticket of this type will end up with.

    One expression, in the layer that owns the write. The ingestion pre-filter has to weigh an
    event against its severity floor before the ticket exists, so it calls this rather than
    restating the fallback - the same way every transport reads the workflow graph through
    `get_allowed_transitions()` instead of keeping a copy.
    """
    return severity or event_type.default_severity


def open_tickets_for_dedup_key(dedup_key):
    """The open tickets an event with this dedup key would join, newest first (S5's lookup).

    One expression, in the layer that owns the write, for the reason `effective_severity` is:
    Phase 3's triage has to know whether S5 will join an event before any ticket is written, and a
    second copy of the predicate would drift from this one silently.
    """
    return EventTicket.objects.filter(dedup_key=dedup_key).exclude(status__in=TERMINAL_STATUSES).order_by("-last_seen")


def _lock_dedup_key(dedup_key):
    """Hold a transaction-scoped lock on this dedup key until the surrounding transaction ends.

    `select_for_update()` cannot serialize this: until the first ticket for a key exists there is
    no row to lock, so two simultaneous first deliveries would each find nothing and each open a
    ticket - the one case rule S5 exists to prevent. An advisory lock is keyed on the value rather
    than on a row, so it holds before the row exists. PostgreSQL only, which ADR 0003 requires
    anyway. Two different keys that happen to share a hash serialize needlessly and correctly.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"nautobot_event_tracker:{dedup_key}"])


def _attach_all(*, ticket, objects, source, user):
    """Attach each object to the ticket, skipping the ones already attached."""
    for obj in objects or []:
        attach_object(ticket=ticket, obj=obj, source=source, user=user)


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
    pk=None,
):
    """Create a ticket in `new`, or join an existing open ticket with the same dedup key (S5).

    `pk` lets a caller choose the ticket's primary key, which the REST API allows on create and
    data imports rely on. It is ignored when the call joins an existing ticket, which keeps its own.

    Returns the `EventTicket`, which may be a pre-existing one. The returned instance carries
    `was_created`: True when this call opened the ticket, False when it joined an existing one.
    Callers that must not treat a recurrence as a new ticket - the UI and REST create paths, which
    would otherwise apply the caller's custom fields to somebody else's ticket - read that flag
    rather than guessing from the event count.
    """
    _validate_actor(source, user)

    if not event_type.enabled:
        raise ValidationError(f"Event type '{event_type}' is disabled and cannot be used for new tickets.")

    occurred_at = occurred_at or timezone.now()
    severity = effective_severity(severity, event_type)

    with transaction.atomic():
        if dedup_key:
            _lock_dedup_key(dedup_key)
            # `join_ticket` re-reads this row under a row lock before writing it; the advisory
            # lock above only keeps two consumers off the same dedup key.
            existing = open_tickets_for_dedup_key(dedup_key).first()
            if existing is not None:
                return join_ticket(
                    ticket=existing,
                    source=source,
                    user=user,
                    occurred_at=occurred_at,
                    related_objects=related_objects,
                )

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
        if pk is not None:
            # Set rather than passed to the constructor: `id=None` would override the field's
            # uuid4 default with a null.
            ticket.id = pk
        ticket.full_clean()
        ticket.save()

        _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.CREATED,
            source=source,
            user=user,
            message=f"Ticket opened with severity '{severity}'.",
        )

        _attach_all(ticket=ticket, objects=related_objects, source=source, user=user)
        ticket.was_created = True
        return ticket


def join_ticket(  # pylint: disable=too-many-arguments
    *, ticket, source, user=None, occurred_at=None, message="", related_objects=None
):
    """Record that this event is that ticket again: the S5 join, callable in its own right.

    Extracted from `create_ticket`'s dedup branch so that Phase 3's triage `attach` (spec 6.4)
    reuses the same semantics rather than inventing new ones - there is one implementation of a
    recurrence, whether a dedup key found the ticket or a model chose it. Writes exactly one
    `recurrence` update (S1); `message` overrides the stock occurrence line when the caller has
    something better to say.

    The ticket is re-read under a row lock here rather than trusted as handed over, because this
    writes the whole row: a stale instance saves back over whatever happened between the caller's
    read and this write - a human's status change, another consumer's own recurrence. The lock
    lives here rather than at each call site so that both callers get it, and because neither has
    one that covers this: `create_ticket`'s advisory lock is keyed on the dedup string, which
    serializes two consumers on that key and nothing else, and triage's attach path holds nothing
    at all. Raises `EventTicket.DoesNotExist` if the ticket was deleted in that window.

    Returns the ticket with `was_created=False`, matching what `create_ticket` returns for a join.
    """
    # Checked twice on purpose. Here, so that a refusal costs no lock and the error a caller sees
    # is the same one it saw before the lock existed; again below on the locked row, because that
    # is the copy this writes, and only that check cannot go stale (S3).
    _check_mutable(ticket, source)
    _validate_actor(source, user)
    occurred_at = occurred_at or timezone.now()

    with transaction.atomic():
        ticket = EventTicket.objects.select_for_update().get(pk=ticket.pk)
        _check_mutable(ticket, source)
        ticket.event_count += 1
        # max() keeps last_seen monotonic under out-of-order delivery, which at-least-once
        # brokers make normal (ADR 0004).
        ticket.last_seen = max(ticket.last_seen, occurred_at)
        ticket.full_clean()
        ticket.save()
        _record(
            ticket=ticket,
            update_type=UpdateTypeChoices.RECURRENCE,
            source=source,
            user=user,
            message=message or f"Event recurred; this is occurrence {ticket.event_count}.",
        )
        # A recurrence can implicate objects the first occurrence did not name, so the
        # attachments are applied to the joined ticket too. Ones already attached are no-ops.
        _attach_all(ticket=ticket, objects=related_objects, source=source, user=user)
        ticket.was_created = False
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
    pk=None,
):
    """Create a human-sourced ticket, applying optional assignment and tags.

    Shared by the REST and UI create paths so that "a person opened a ticket" has one definition
    and the two transports cannot drift apart.

    On a dedup join the assignee and tags are left alone: the ticket belongs to an earlier event,
    and somebody may already be working it. Both transports guard every other field on
    `was_created` for the same reason.
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
        pk=pk,
    )
    if ticket.was_created:
        if assignee is not None:
            assign(ticket=ticket, assignee=assignee, source=TicketSourceChoices.HUMAN, user=user)
        if tags:
            ticket.tags.set(tags)
    return ticket


def walk_to_status(  # pylint: disable=too-many-arguments
    *, ticket, to_status, source, user=None, message="", resolution=""
):
    """Move a ticket to `to_status` by the shortest legal route, one transition at a time.

    Every step is an ordinary `transition()`, so the trail reads like a ticket somebody worked and
    every rule that governs a transition governs these too. `resolution` is required when the route
    passes through `resolved`, exactly as it is for the single move.

    Raises `InvalidTransitionError` when no route exists - out of `closed`, for instance.
    """
    route = shortest_transition_path(to_status, from_status=ticket.status)
    if route is None:
        raise InvalidTransitionError(f"There is no legal route from '{ticket.status}' to '{to_status}'.")

    updates = []
    for step in route:
        updates.append(
            transition(
                ticket=ticket,
                to_status=step,
                source=source,
                user=user,
                message=message,
                resolution=resolution if step == TicketStatusChoices.RESOLVED else "",
            )
        )
    return updates


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


def _apply_terminal_bookkeeping(ticket, *, from_status, to_status, resolution, now):
    """Keep the terminal timestamps and resolution consistent with the new status.

    This is what makes rules C1 and C2 hold across a transition: entering a terminal state stamps
    the times and records how it ended, and leaving one clears both so a reopened ticket does not
    carry a stale answer.
    """
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
        ticket.resolved_at = None
        ticket.closed_at = None
        ticket.resolution = ""


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
        _apply_terminal_bookkeeping(
            ticket, from_status=from_status, to_status=to_status, resolution=resolution, now=now
        )
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


def _record_attachment(*, ticket, obj, content_type, update_type, source, user, message):  # pylint: disable=too-many-arguments
    """Write one attach or detach row, or nothing if it would be a no-op.

    Attaching what is already attached and detaching what is not attached are both no-ops, so that
    a re-delivered event does not pollute the timeline.
    """
    attaching = update_type == UpdateTypeChoices.OBJECT_ATTACHED
    already_attached = (content_type.pk, obj.pk) in _attached_keys(ticket)
    if attaching == already_attached:
        return None

    verb = "Attached" if attaching else "Detached"
    with transaction.atomic():
        return _record(
            ticket=ticket,
            update_type=update_type,
            source=source,
            user=user,
            message=message or f"{verb} {content_type_label(content_type)} '{obj}'.",
            related_object_type=content_type,
            related_object_id=obj.pk,
        )


def attach_object(*, ticket, obj, source, user=None, message=""):
    """Attach a Nautobot object to the ticket.

    Returns `None` without writing a row when the object is already attached, so that a
    re-delivered event does not pollute the timeline.
    """
    _check_mutable(ticket, source)
    _validate_actor(source, user)

    content_type = ContentType.objects.get_for_model(obj)
    label = content_type_label(content_type)
    allowed_types = get_attachable_object_types()
    if label not in allowed_types:
        raise ValidationError(
            f"Objects of type '{label}' may not be attached to a ticket. "
            f"Permitted types: {', '.join(sorted(allowed_types)) or 'none configured'}."
        )

    return _record_attachment(
        ticket=ticket,
        obj=obj,
        content_type=content_type,
        update_type=UpdateTypeChoices.OBJECT_ATTACHED,
        source=source,
        user=user,
        message=message,
    )


def detach_object(*, ticket, obj, source, user=None, message=""):
    """Detach an object from the ticket by appending an `object_detached` update.

    Returns `None` without writing a row when the object is not currently attached. Deliberately
    does not check the allowlist, so an object attached under an older configuration stays
    removable.
    """
    _check_mutable(ticket, source)
    _validate_actor(source, user)

    return _record_attachment(
        ticket=ticket,
        obj=obj,
        content_type=ContentType.objects.get_for_model(obj),
        update_type=UpdateTypeChoices.OBJECT_DETACHED,
        source=source,
        user=user,
        message=message,
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
