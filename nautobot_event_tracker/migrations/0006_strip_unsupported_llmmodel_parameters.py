"""Strip keys `LLMModel.default_parameters` no longer accepts from rows written before it did.

The field became an allowlist after a review found that its denylist missed `base_url`, one of
litellm's aliases for the endpoint - so a row written earlier may still hold a key that
`clean()` now refuses. Such a row is not merely stale: it cannot be saved at all, because
`full_clean()` inspects the whole field even when an operator edits an unrelated one, which
includes the edit that unticks *Enabled*.

The service layer already refuses to pass these keys, so removing them changes no call. What it
changes is that the row becomes editable again, and that nothing endpoint-shaped stays in a
change-logged, REST-served column.
"""

from django.db import migrations

#: Frozen deliberately rather than imported from `models.LLMModel`. A migration says what was true
#: on the day it ran; importing the live tuple would make this rewrite history every time the
#: allowlist changes.
ALLOWED_PARAMETERS = (
    "extra_body",
    "frequency_penalty",
    "logit_bias",
    "n",
    "presence_penalty",
    "reasoning_effort",
    "seed",
    "stop",
    "temperature",
    "timeout",
    "top_k",
    "top_p",
)


def strip_unsupported_parameters(apps, schema_editor):
    """Remove every key outside the allowlist, leaving the generation parameters alone."""
    llm_model = apps.get_model("nautobot_event_tracker", "LLMModel")
    for model in llm_model.objects.exclude(default_parameters={}):
        parameters = model.default_parameters or {}
        kept = {key: value for key, value in parameters.items() if key in ALLOWED_PARAMETERS}
        if kept != parameters:
            model.default_parameters = kept
            model.save(update_fields=["default_parameters"])


class Migration(migrations.Migration):
    """Data-only migration; the column itself does not change."""

    dependencies = [
        ("nautobot_event_tracker", "0005_ingestionstats_triage_counters"),
    ]

    operations = [
        # Not reversible in any useful sense: the keys are gone, and re-adding them would mean
        # inventing values. `noop` says that plainly rather than leaving the migration unreversible
        # and the whole app un-downgradable with it.
        migrations.RunPython(strip_unsupported_parameters, migrations.RunPython.noop),
    ]
