"""Move the AI registry to the AI Models app (ADR 0010).

Four tables go: `LLMProvider`, `LLMModel`, `MCPServer` and `MCPTool`. Their replacements are
`nautobot_ai_models.AIProvider`, `AIModel`, `MCPServer` and `MCPTool`, and the three foreign keys
that pointed into the local tables are repointed at them first.

No rows are copied. This app has never been released, so there is no deployment whose registry
would be lost, and nothing here is worth the cross-app data migration it would take: the AI Models
app records the same fields under different names, and its two discovery Jobs rebuild the model
and tool halves from the endpoints themselves. Register the providers and servers again, and run
them.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        # The tables this migration points at have to exist first. Naming the other app's
        # migration is what orders the two graphs against each other; without it Django is free to
        # run this one first and the AlterField fails on a missing table. `0002` rather than
        # `0001`, because that is the migration that adds the provider dialect, the model kind and
        # the two enabled flags - the fields the 1.1 floor is a floor for.
        ("nautobot_ai_models", "0002_registry_fields_for_consuming_apps"),
        ("nautobot_event_tracker", "0011_analytics_indexes"),
    ]

    operations = [
        # The three foreign keys, before anything is dropped. `AlterField` across apps rewrites the
        # constraint in place; the columns keep their names and every row keeps its value, which is
        # a value that no longer resolves - and does not have to, since the tables it referenced go
        # in the same transaction.
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
        # Constraints before their tables, and children before parents.
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
