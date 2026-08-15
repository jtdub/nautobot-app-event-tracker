"""Test the read-only surfaces over IngestionStats.

The point of every test here is the same: these rows are written by one process, as a record of
what it saw, and nothing outside that process may change them through any route.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from nautobot.apps.testing import APIViewTestCases, ViewTestCases

from nautobot_event_tracker.models import IngestionStats
from nautobot_event_tracker.tests import fixtures


def create_stats():
    """Three counter rows, in three windows."""
    now = timezone.now().replace(second=0, microsecond=0)
    return [
        fixtures.create_ingestionstats(
            bucket_start=now - timedelta(minutes=5 * index),
            received=10 * index,
            tickets_opened=index,
            dropped=index,
            drops_by_reason={"lab-estate": index},
        )
        for index in range(1, 4)
    ]


class IngestionStatsAPITest(  # pylint: disable=too-many-ancestors
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
):
    """The REST surface: readable, and nothing else."""

    model = IngestionStats

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        create_stats()

    def test_post_is_not_offered(self):
        """Creating a counter row would be inventing traffic that never arrived."""
        self.add_permissions("nautobot_event_tracker.add_ingestionstats")
        response = self.client.post(self._get_list_url(), {}, format="json", **self.header)
        self.assertEqual(response.status_code, 405)

    def test_patch_is_not_offered(self):
        """Nor would editing one be anything but falsifying a record."""
        self.add_permissions("nautobot_event_tracker.change_ingestionstats")
        response = self.client.patch(
            self._get_detail_url(IngestionStats.objects.first()),
            {"received": 0},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 405)

    def test_delete_is_not_offered(self):
        """Retention prunes these; a client does not."""
        self.add_permissions("nautobot_event_tracker.delete_ingestionstats")
        response = self.client.delete(self._get_detail_url(IngestionStats.objects.first()), **self.header)
        self.assertEqual(response.status_code, 405)

    def test_the_drop_breakdown_is_returned(self):
        """It is the field an operator came for."""
        self.add_permissions("nautobot_event_tracker.view_ingestionstats")
        response = self.client.get(self._get_detail_url(IngestionStats.objects.first()), **self.header)
        self.assertIn("drops_by_reason", response.data)


class IngestionStatsViewTest(  # pylint: disable=too-many-ancestors
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """The UI surface: a list and a detail page, and no way to change anything."""

    model = IngestionStats

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        create_stats()

    def test_there_is_no_edit_route(self):
        """A route that does not exist cannot be reached by a determined URL."""
        with self.assertRaises(Exception):
            reverse(
                "plugins:nautobot_event_tracker:ingestionstats_edit", kwargs={"pk": IngestionStats.objects.first().pk}
            )

    def test_there_is_no_add_route(self):
        """Same for creating one."""
        with self.assertRaises(Exception):
            reverse("plugins:nautobot_event_tracker:ingestionstats_add")

    def test_there_is_no_delete_route(self):
        """And for deleting one."""
        with self.assertRaises(Exception):
            reverse(
                "plugins:nautobot_event_tracker:ingestionstats_delete",
                kwargs={"pk": IngestionStats.objects.first().pk},
            )

    def test_the_list_is_refused_without_the_permission(self):
        """The nav entry hides it, and the view refuses it: both, not either."""
        self.client.force_login(get_user_model().objects.create(username="nobody"))
        response = self.client.get(reverse("plugins:nautobot_event_tracker:ingestionstats_list"))
        self.assertEqual(response.status_code, 403)

    def test_the_detail_page_shows_the_drop_breakdown(self):
        """'Which rule is eating my events' is what this page is for."""
        self.user.is_superuser = True
        self.user.save()
        stats = IngestionStats.objects.exclude(drops_by_reason={}).first()
        response = self.client.get(stats.get_absolute_url())
        self.assertContains(response, "lab-estate")
