"""What an event is about: finding the Nautobot objects its payload names.

Phase 4A, rules E1-E10. A ticket that says `Interface ethernet-1/1 is down` on `leaf-01` carries
those as strings inside a JSON payload; Nautobot knows both of them as objects. This module is
what looks. It reads configured paths out of a payload, finds the rows they name, and hands them
back - it writes nothing. Everything it finds reaches a ticket through `services.tickets`, which
stays the only code that writes an `EventTicket` or a `TicketUpdate` (ADR 0001).

It lives in `services/` rather than `ingestion/` because the dependency runs one way: `ingestion`
imports `services` and never the reverse, which is the arrangement `effective_severity()` already
has. It also means a "re-resolve this ticket" action can reach this code later without moving it.

Rules implemented here, referenced by number from the Phase 4A spec:

* **E3** - reads only; imports nothing from `ingestion`.
* **E5** - nothing found is a counter, never an exception. No failure in this module may reach the
  ticket write, because a resolver that raised inside `_write_ticket()`'s transaction would cost
  the ticket rather than the attachment.
* **E6** - two matches attach nothing: picking one picks it by primary-key order.
* **E7** - an absent path is a fault; an empty value is an answer. So is a scope whose own rule
  found nothing, which is a consequence of that miss rather than a second fault.
* **E8** - the cache holds misses too, and is bounded by entry count.
"""

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from django.apps import apps

from nautobot_event_tracker.payloads import MISSING, resolve_path

logger = logging.getLogger(__name__)

#: Why a rule found nothing. Log keys and test vocabulary, deliberately a closed set: the counter
#: they feed is a single number, and a per-reason breakdown would be `drops_by_reason` again.
MISS_PATH_ABSENT = "path_absent"
MISS_NOT_SCALAR = "not_scalar"
MISS_NO_MATCH = "no_match"
MISS_AMBIGUOUS = "ambiguous"
MISS_ERROR = "error"


@dataclass(frozen=True)
class ResolveRule:
    """One lookup: a path in the payload, and where to go looking for what it holds.

    `scope` is `((field on this rule's model, name of an earlier rule), ...)`. It exists because an
    interface name is unique per device and not globally: every switch in the estate has an
    `ethernet-1/1`, so the lookup has to be told which device, and the only thing that knows is the
    rule that already found it.

    Parsed and validated by `ingestion.config`, which is where a bad rule is refused. Nothing here
    re-checks that the model is attachable or the field exists; by the time a rule reaches this
    module, startup has already said both (E4).
    """

    name: str
    path: str
    model: str
    field: str
    scope: tuple = ()

    @property
    def model_class(self):
        """The model this rule looks in. Cheap: `apps.get_model` is a dictionary lookup."""
        return apps.get_model(self.model)


@dataclass(frozen=True)
class Resolution:
    """What one event's rules found: the objects, and the rules that found nothing.

    `misses` carries `(rule name, reason)` pairs for logging. Skips are not misses and are not in
    here at all - see E7.
    """

    objects: tuple = ()
    misses: tuple = ()


#: What "this event resolved to nothing" is, so that it is one value rather than a value and a
#: `None`, and so a topic with no rules costs no allocation.
EMPTY = Resolution()


class Resolver:  # pylint: disable=too-few-public-methods
    """The lookups, and the cache in front of them.

    Constructed once with the loaded configuration and called per event, the shape `PreFilter` and
    `TriageFilter` already have. It holds nothing but its cache, so two consumers of the same
    configuration are independent and both correct.
    """

    def __init__(self, *, ttl_seconds, max_entries, clock=time.monotonic):
        """Hold an answer for this long, and hold this many of them."""
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        #: key -> (read at, object or None, miss reason or None). An `OrderedDict` because the
        #: eviction is least-recently-used and this is what Python gives you for that.
        self._cache = OrderedDict()

    def resolve(self, payload, rules):
        """Run the rules in order and return what they found.

        Order matters twice: a later rule may scope on an earlier one's result, and the objects
        come back in the order the operator wrote the rules, which is the order they read best on
        the ticket.
        """
        if not rules:
            return EMPTY

        found = {}
        objects = []
        seen = set()
        misses = []

        for rule in rules:
            obj, miss = self._resolve_one(payload, rule, found)
            if obj is not None:
                found[rule.name] = obj
                key = (rule.model, obj.pk)
                if key not in seen:
                    seen.add(key)
                    objects.append(obj)
            elif miss is not None:
                misses.append((rule.name, miss))

        return Resolution(objects=tuple(objects), misses=tuple(misses))

    def _resolve_one(self, payload, rule, found):
        """One rule: `(object, None)`, `(None, miss reason)` or `(None, None)` for a skip."""
        value = resolve_path(payload, rule.path, default=MISSING)
        if value is MISSING:
            # E7 - the rule and the payload disagree about what this message carries.
            return None, MISS_PATH_ABSENT
        if isinstance(value, (dict, list, tuple)):
            # One value, one object. A list would mean deciding what an ambiguous element does,
            # which is E6 multiplied by the list's length (spec 11.7).
            return None, MISS_NOT_SCALAR

        text = "" if value is None else str(value).strip()
        if not text:
            # E7 - the producer said there is none, which for a message about a BGP session is the
            # truth rather than a fault. The lab's bridge writes exactly this on purpose.
            return None, None

        scope = {}
        for field, rule_name in rule.scope:
            parent = found.get(rule_name)
            if parent is None:
                # E7 - a consequence of the parent's miss, not a second fault. `load()` gives the
                # same argument about "no topics are configured".
                return None, None
            scope[field] = parent

        return self._look_up(rule, text, scope)

    def _look_up(self, rule, text, scope):
        """The query, or the answer already held for it."""
        key = (
            rule.model,
            rule.field,
            text.casefold(),
            tuple(sorted((field, str(obj.pk)) for field, obj in scope.items())),
        )
        now = self._clock()

        entry = self._cache.get(key)
        if entry is not None and now - entry[0] < self.ttl_seconds:
            self._cache.move_to_end(key)
            return entry[1], entry[2]

        try:
            # Two rows, not a count and then a fetch: "is there exactly one" and "which one" are
            # the same question, and one query answers both. Case-insensitive because a device
            # logs its hostname in whatever case its own configuration holds, and `LEAF-01` is not
            # a different switch from `leaf-01`.
            matches = list(rule.model_class.objects.filter(**{f"{rule.field}__iexact": text}, **scope)[:2])
        except Exception as error:  # pylint: disable=broad-except
            # E5. Broad on purpose and stated rather than implied: this runs immediately before the
            # ticket write, and there is no failure here worth losing an event over. Not cached,
            # because whatever this was, it was not an answer.
            logger.warning("Enrichment rule '%s' could not run (%s); attaching nothing", rule.name, error)
            return None, MISS_ERROR

        if len(matches) == 1:
            return self._remember(key, now, matches[0], None)
        if not matches:
            return self._remember(key, now, None, MISS_NO_MATCH)

        # E6 - and worth a warning of its own: two objects of one type with one name is a fact
        # about the estate that somebody should hear about.
        logger.warning(
            "Enrichment rule '%s' matched more than one %s with %s '%s'; attaching neither",
            rule.name,
            rule.model,
            rule.field,
            text,
        )
        return self._remember(key, now, None, MISS_AMBIGUOUS)

    def _remember(self, key, now, obj, miss):
        """Hold this answer, evicting the least recently used once the cache is full.

        Misses are held too. A cache that stores only hits makes the failing case the expensive
        one - an unknown hostname arriving ten thousand times must not be ten thousand queries -
        and that lesson is the Phase 3 review's, paid for once already.

        Bounded by entry count, which `EventTypeCache` has no need to be: that one holds a table an
        operator maintains, and this one is keyed on values out of the payload, which whoever emits
        the events controls. An unbounded dictionary keyed on those is a memory leak with a trigger.
        """
        self._cache[key] = (now, obj, miss)
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)
        return obj, miss
