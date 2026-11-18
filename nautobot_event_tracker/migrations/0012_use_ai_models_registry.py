"""Move the AI registry to the AI Models app (ADR 0010).

Four tables go: `LLMProvider`, `LLMModel`, `MCPServer` and `MCPTool`. Their replacements are
`nautobot_ai_models.AIProvider`, `AIModel`, `MCPServer` and `MCPTool`, and the three foreign keys
that pointed into the local tables are repointed at them first.

No rows are copied. This app has never been released, so there is no deployment whose registry
would be lost, and nothing here is worth the cross-app data migration it would take: the AI Models
app records the same fields under different names, and its two discovery Jobs rebuild the model
and tool halves from the endpoints themselves. Register the providers and servers again, and run
them.

The migration depends on `nautobot_ai_models.0002`, which adds the fields that the 1.1 floor needs. It changes
the three foreign keys first, then removes constraints, child tables and parent tables. The migration needs empty
`LLMUsageRecord`, `TicketEmbedding` and `AgentToolCall` tables, because PostgreSQL checks each row against the new
foreign key.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("nautobot_ai_models", "0002_registry_fields_for_consuming_apps"),
        ("nautobot_event_tracker", "0011_analytics_indexes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="llmusagerecord",
            name="model",
            field=models.ForeignKey(
                help_text="The registered model that served the call. Lives in the AI Models app (ADR 0010).",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="event_tracker_usage_records",
                to="nautobot_ai_models.aimodel",
            ),
        ),
        migrations.AlterField(
            model_name="ticketembedding",
            name="model",
            field=models.ForeignKey(
                help_text="Which model produced it. A vector is comparable only with its own model's (R2).",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="event_tracker_embeddings",
                to="nautobot_ai_models.aimodel",
            ),
        ),
        migrations.AlterField(
            model_name="agenttoolcall",
            name="tool",
            field=models.ForeignKey(
                help_text="Which tool was asked for. PROTECT: a call is evidence, and evidence outlives a registry edit.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="event_tracker_calls",
                to="nautobot_ai_models.mcptool",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="llmmodel",
            name="event_tracker_llmmodel_provider_name_unique",
        ),
        migrations.RemoveConstraint(
            model_name="mcptool",
            name="event_tracker_mcptool_server_name_unique",
        ),
        migrations.RemoveField(model_name="llmmodel", name="provider"),
        migrations.RemoveField(model_name="llmmodel", name="tags"),
        migrations.RemoveField(model_name="llmprovider", name="external_integration"),
        migrations.RemoveField(model_name="llmprovider", name="tags"),
        migrations.RemoveField(model_name="mcptool", name="server"),
        migrations.RemoveField(model_name="mcptool", name="tags"),
        migrations.RemoveField(model_name="mcpserver", name="external_integration"),
        migrations.RemoveField(model_name="mcpserver", name="tags"),
        migrations.DeleteModel(name="LLMModel"),
        migrations.DeleteModel(name="LLMProvider"),
        migrations.DeleteModel(name="MCPTool"),
        migrations.DeleteModel(name="MCPServer"),
    ]
