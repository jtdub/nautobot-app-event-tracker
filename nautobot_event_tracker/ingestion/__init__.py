"""Event ingestion: brokers, normalization, the pre-filter, and the counters.

Nothing in this package writes to `EventTicket` or `TicketUpdate`. Every ticket an ingested event
produces comes from `nautobot_event_tracker.services.tickets`, with `source=system`. See ADR 0001.

Nothing in this package calls a language model either. The pre-filter is rules and arithmetic;
LLM triage is Phase 3 and plugs in at the seam `pipeline.handle_message()` names.
"""
