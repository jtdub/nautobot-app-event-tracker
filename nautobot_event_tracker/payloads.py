"""Reading values out of an event payload.

One function, in a module of its own, because two packages need it and neither may import the
other. `ingestion` reads paths to normalize a message; `services.enrichment` reads them to find
the objects an event names, and the service layer must not import the ingestion package (Phase 4A
rule E3). Left in `ingestion/normalize.py`, where it started, it would have made the dependency
run the wrong way.
"""

#: Returned when a path is not in the payload at all, which is a different answer from a path that
#: is there and holds nothing. The enrichment resolver reads the difference: a missing path means
#: the rule and the payload disagree, and an empty value means the producer said there is none
#: (rule E7). Callers that do not care leave the default alone and get `None` for both.
MISSING = object()


def resolve_path(payload, path, default=None):
    """Walk a dotted path into nested objects, returning `default` rather than raising.

    A missing key, a non-object where an object was expected, and an empty path all yield
    `default`. A key containing a literal dot is not addressable, which is a limitation worth
    knowing and not worth an escaping syntax.
    """
    current = payload
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
