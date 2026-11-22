# Ticket Embedding

A Ticket Embedding is one closed ticket, as a vector. The retrieval corpus is these rows. The [Similar Tickets](../admin/rag.md) panel reads them.

Only `services/rag.py` writes these rows. The web interface has no add form and no edit form. The REST endpoint accepts no write method.

## Fields

The detail page of an embedding shows the bold labels below. The list shows **Indexed** for `indexed_at`. The list has no column for the document or its fingerprint.

| Field | Description |
| --- | --- |
| **Ticket** (`ticket`) | The closed ticket that this row represents. One row exists for each ticket. If you delete the ticket, the app deletes the row. |
| **Model** (`model`) | The embedding model that made the vector. The registry refuses to delete a model that has a row, so the corpus keeps its model. |
| **Dimensions** (`dimensions`) | The width of the vector. The app records it, because the model decides it. |
| **Indexed At** (`indexed_at`) | The time when the app indexed the ticket. |
| **Document Fingerprint** (`document_fingerprint`) | A digest of the document. If you close again a ticket that nobody edited, the app makes no model call. |
| **Document** (`document`) | Exactly what the app embedded. The detail page shows it in a panel. |

The vector itself does not show in the UI or the API. It has thousands of numbers. It means nothing without the model that made it. A list endpoint that returned it would be hard for the client to use.

## Why the app stores the document

A similarity score with no visible input is not possible to check. When two tickets match and the match looks wrong, read the document. Often one ticket has the resolution "closed, no action", and the vector holds that text.

## What the document has {#what-is-in-the-document-and-what-is-not}

**The document has** the title, the event type, the severity, the description, the resolution, and the comments that people wrote.

**The document does not have the raw event payload.** The payload is machine data. Whoever emits your events writes half of it. Its volume would dominate the vector. Two unrelated tickets from one device that sends many events would look alike.

**The document does not have the comments that the AI wrote.** The corpus must hold what people concluded. If the app indexed the investigation notes of an agent, the app would use its own output as a source. A guess would become a precedent, and a reader would think that a person checked it.

## One model at a time {#one-model-at-a-time}

The app compares a vector only with vectors from the same model. The cosine distance between vectors of two different models is a number with no meaning.

If you change the embedding model, the panel shows nothing. It does not show wrong results. Each existing row belongs to the old model, and the app does not compare it again. Run `nautobot-server indexclosedtickets --reindex` to build the corpus again.

## Who can read an embedding {#who-can-read-one}

A user can read an embedding if the user can read **its ticket**. The `view_ticketembedding` permission alone is not enough. The document is a copy of the ticket text. If the app used only the permission of the embedding, an ObjectPermission constraint on Event Ticket would not apply.

## Elsewhere

The REST API serves embeddings at `/api/plugins/event-tracker/ticket-embeddings/`. It is read-only and has no vector.

**TicketEmbedding is not on GraphQL, on purpose.** Nautobot restricts a GraphQL type with the permission of that type only. It has no hook for a restriction by the parent object. A filter on an unrestricted queryset also leaks data, because it filters on the field without it returning the field. The REST API restricts the rows correctly. Use it.
