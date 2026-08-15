"""Django API urlpatterns declaration for nautobot_event_tracker app."""

from nautobot.apps.api import OrderedDefaultRouter

from nautobot_event_tracker.api import views

router = OrderedDefaultRouter()
router.register("event-types", views.EventTypeViewSet)
router.register("tickets", views.EventTicketViewSet)
router.register("ticket-updates", views.TicketUpdateViewSet)

app_name = "nautobot_event_tracker-api"
urlpatterns = router.urls
