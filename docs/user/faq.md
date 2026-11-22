# Frequently Asked Questions

## Why does the ticket edit form have no status field?

Every transition must follow the workflow and leave an entry in the update trail. In the web interface, the **Transition** menu on the ticket page is the only way to change a status. The REST API uses the action `tickets/<id>/transition/`. A REST `PATCH` that sets the status fails with an error. See [Using the App](app_use_cases.md).

## Why can I not reopen a closed ticket?

**Closed** is the final status. No transition leaves it. If you close a ticket by mistake, open a new ticket. A **Resolved** ticket can go back to **In Progress**.

## Why did an event not become a ticket?

Open **Apps > Event Tracker > Ingestion Stats**. The page counts the events that the consumer received, and the tickets that they opened or joined. It also counts the events that the consumer dropped, and each drop under the rule that refused it. An event that matches an open ticket with the same dedup key joins that ticket and makes no new ticket. Event triage can also suppress an event. See [Running the event consumer](../admin/ingestion.md) and [Configuring LLM Providers](../admin/llm.md).

## Does the app need a language model?

No. If you register no AI provider, the app makes no model call. Event triage and the agent are off until you turn them on. See [Configuring LLM Providers](../admin/llm.md).

## Can the agent change the network without approval?

No. The agent calls only the read-only tools that you enabled. The agent proposes a tool that is marked **Writable** and stops. A person with the **Can approve or deny a proposed agent tool call** permission must approve the call first. See [Running the Agent](../admin/agents.md).

## Which databases does the app support?

The app supports PostgreSQL only. It does not support MySQL. See [Installing the App in Nautobot](../admin/install.md).

## Where do I register AI providers and models?

The AI Models app owns the registry. Open **AI Tools > AI Models > AI Providers**. Event Tracker only reads the registry. See [Configuring LLM Providers](../admin/llm.md).

## Why does a dashboard panel show a shorter window than I asked?

The app deletes old ingestion stats and old LLM usage records. The dashboard does not draw data that the app deleted. The panel title then shows, for example, **"last 30 days (retention; 90 requested)"**. To see a longer window, raise `ingestion.stats_retention_days` or `llm.usage_retention_days`. See [The Analytics Dashboard](../admin/dashboard.md).
