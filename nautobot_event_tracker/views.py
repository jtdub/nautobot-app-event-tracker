"""Views for nautobot_event_tracker."""

from nautobot.apps.ui import ObjectDetailContent, ObjectFieldsPanel, SectionChoices
from nautobot.apps.views import NautobotUIViewSet

from nautobot_event_tracker import filters, forms, models, tables
from nautobot_event_tracker.api import serializers


class EventTrackerExampleModelUIViewSet(NautobotUIViewSet):
    """ViewSet for EventTrackerExampleModel views."""

    bulk_update_form_class = forms.EventTrackerExampleModelBulkEditForm
    filterset_class = filters.EventTrackerExampleModelFilterSet
    filterset_form_class = forms.EventTrackerExampleModelFilterForm
    form_class = forms.EventTrackerExampleModelForm
    lookup_field = "pk"
    queryset = models.EventTrackerExampleModel.objects.all()
    serializer_class = serializers.EventTrackerExampleModelSerializer
    table_class = tables.EventTrackerExampleModelTable

    # Here is an example of using the UI  Component Framework for the detail view.
    # More information can be found in the Nautobot documentation:
    # https://docs.nautobot.com/projects/core/en/stable/development/core/ui-component-framework/
    object_detail_content = ObjectDetailContent(
        panels=[
            ObjectFieldsPanel(
                weight=100,
                section=SectionChoices.LEFT_HALF,
                fields="__all__",
                # Alternatively, you can specify a list of field names:
                # fields=[
                #     "name",
                #     "description",
                # ],
                # Some fields may require additional configuration, we can use value_transforms
                # value_transforms={
                #     "name": [helpers.bettertitle]
                # },
            ),
            # If there is a ForeignKey or M2M with this model we can use ObjectsTablePanel
            # to display them in a table format.
            # ObjectsTablePanel(
            # weight=200,
            # section=SectionChoices.RIGHT_HALF,
            # table_class=tables.EventTrackerExampleModelTable,
            # You will want to filter the table using the related_name
            # filter="eventtrackerexamplemodels",
            # ),
        ],
    )
