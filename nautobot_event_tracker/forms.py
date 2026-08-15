"""Forms for nautobot_event_tracker."""

from django import forms
from nautobot.apps.constants import CHARFIELD_MAX_LENGTH
from nautobot.apps.forms import NautobotBulkEditForm, NautobotFilterForm, NautobotModelForm, TagsBulkEditFormMixin

from nautobot_event_tracker import models


class EventTrackerExampleModelForm(NautobotModelForm):  # pylint: disable=too-many-ancestors
    """EventTrackerExampleModel creation/edit form."""

    class Meta:
        """Meta attributes."""

        model = models.EventTrackerExampleModel
        fields = "__all__"


class EventTrackerExampleModelBulkEditForm(TagsBulkEditFormMixin, NautobotBulkEditForm):  # pylint: disable=too-many-ancestors
    """EventTrackerExampleModel bulk edit form."""

    pk = forms.ModelMultipleChoiceField(
        queryset=models.EventTrackerExampleModel.objects.all(), widget=forms.MultipleHiddenInput
    )
    description = forms.CharField(required=False, max_length=CHARFIELD_MAX_LENGTH)

    class Meta:
        """Meta attributes."""

        nullable_fields = [
            "description",
        ]


class EventTrackerExampleModelFilterForm(NautobotFilterForm):  # pylint: disable=too-many-ancestors
    """Filter form to filter searches."""

    model = models.EventTrackerExampleModel
    field_order = ["q", "name"]

    q = forms.CharField(
        required=False,
        label="Search",
        help_text="Search within Name.",
    )
    name = forms.CharField(required=False, label="Name")
