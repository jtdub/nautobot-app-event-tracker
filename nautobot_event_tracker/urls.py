"""Django urlpatterns declaration for nautobot_event_tracker app."""

from django.templatetags.static import static
from django.urls import path
from django.views.generic import RedirectView
from nautobot.apps.urls import NautobotUIViewSetRouter

from nautobot_event_tracker import views

app_name = "nautobot_event_tracker"
router = NautobotUIViewSetRouter()

router.register("event-types", views.EventTypeUIViewSet)
router.register("tickets", views.EventTicketUIViewSet)
router.register("ingestion-stats", views.IngestionStatsUIViewSet)

urlpatterns = [
    path(
        "tickets/<uuid:pk>/transition/",
        views.EventTicketTransitionView.as_view(),
        name="eventticket_transition",
    ),
    path(
        "tickets/<uuid:pk>/attach/",
        views.EventTicketAttachView.as_view(),
        name="eventticket_attach",
    ),
    path(
        "tickets/<uuid:pk>/detach/",
        views.EventTicketDetachView.as_view(),
        name="eventticket_detach",
    ),
    path("docs/", RedirectView.as_view(url=static("nautobot_event_tracker/docs/index.html")), name="docs"),
]

urlpatterns += router.urls
