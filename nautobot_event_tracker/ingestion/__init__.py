"""Event ingestion: brokers, normalization, the pre-filter, triage, and the counters.

Nothing in this package writes to `EventTicket` or `TicketUpdate`. Every ticket an ingested event
produces comes from `nautobot_event_tracker.services.tickets`. See ADR 0001.

Nothing in this package imports a language model client either. LLM triage (`triage.py`) speaks
only to `services.llm`, which is the app's one litellm import site (rule L2); the pre-filter in
front of it stays rules and arithmetic, so noise never costs a token (T1).
"""
