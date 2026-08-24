# Ticket Embedding

One closed ticket, as a vector. The retrieval corpus is these rows, and the
[Similar Tickets](../admin/rag.md) panel is what reads them.

Written by `services/rag.py` and by nothing else. No form creates one, and no API route writes one.

## Fields

| Field | Description |
| --- | --- |
| Ticket | The closed ticket this represents. One row per ticket; deleting the ticket deletes it. |
| Model | The embedding model that produced the vector. Protected, so tidying the registry cannot orphan the corpus. |
| Dimensions | How wide the vector is. Recorded rather than assumed — it is the model's choice. |
| Document | Exactly what was embedded. |
| Document Fingerprint | A digest of that document. Re-closing a ticket nobody edited costs no model call. |
| Indexed At | |

The vector itself is not shown, in the UI or the API. It is thousands of floats, it means nothing
without the model that produced it, and a list endpoint returning it would be unkind to the client.

## Why the document is stored

A similarity score with no visible input is not something anybody can check. When two tickets match
and the match looks wrong, the document is how you find out why — usually that one of them has a
resolution reading "closed, no action" and the vector faithfully captured that.

## What is in the document, and what is not

**In:** the title, the event type, the severity, the description, the resolution, and comments
written by people.

**Not in: the raw event payload.** It is machine noise, it is the half written by whoever emits your
events, and by volume it would dominate the vector — two unrelated tickets from one chatty device
would look alike because their payloads do.

**Not in: comments written by the AI.** The corpus should be what people concluded. Indexing an
agent's own investigation notes means the app starts learning from itself: a guess gets indexed,
retrieved as precedent, and read as though somebody had checked it.

## One model at a time

A vector is only ever compared with vectors from the same model. Cosine distance between two
different models' vectors is a number, and it is meaningless.

The practical consequence is that **changing embedding model makes the panel go quiet** rather than
go wrong. Every existing row belongs to the old model and is never compared with anything again.
`nautobot-server indexclosedtickets --reindex` is how the corpus comes back.

## Who can read one

An embedding is readable by whoever may read **its ticket**, not merely by whoever holds
`view_ticketembedding`. The document is a verbatim copy of the ticket, so gating it on its own
permission would let an ObjectPermission constraint on Event Ticket quietly stop applying.

## Elsewhere

`/api/plugins/event-tracker/ticket-embeddings/`, read-only and without the vector.

**Not on GraphQL, deliberately.** Nautobot restricts a GraphQL type on that type's own model
permission, with no hook for a parent-object restriction — and excluding the document would not be
enough on its own, since a filter predicate over an unrestricted queryset leaks by filtering
without returning the field. Everything safe to read is on the REST API, where the restriction
works.
