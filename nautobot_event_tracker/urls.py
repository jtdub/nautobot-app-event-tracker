"""Django urlpatterns declaration for nautobot_event_tracker app."""

from django.templatetags.static import static
from django.urls import path
from django.views.generic import RedirectView
from nautobot.apps.urls import NautobotUIViewSetRouter

from nautobot_event_tracker import views

app_name = "nautobot_event_tracker"
router = NautobotUIViewSetRouter()

# The standard is for the route to be the hyphenated version of the model class name plural.
# for example, ExampleModel would be example-models.
router.register("event-tracker-example-models", views.EventTrackerExampleModelUIViewSet)


urlpatterns = [
    path("docs/", RedirectView.as_view(url=static("nautobot_event_tracker/docs/index.html")), name="docs"),
]

urlpatterns += router.urls
