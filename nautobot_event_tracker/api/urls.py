"""Django API urlpatterns declaration for nautobot_event_tracker app."""

from nautobot.apps.api import OrderedDefaultRouter

from nautobot_event_tracker.api import views

router = OrderedDefaultRouter()
# add the name of your api endpoint, usually hyphenated model name in plural, e.g. "my-model-classes"
router.register("event-tracker-example-models", views.EventTrackerExampleModelViewSet)

app_name = "nautobot_event_tracker-api"
urlpatterns = router.urls
