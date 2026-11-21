"""The signal test of the service layer: finding the soft time limit inside another error."""

from celery.exceptions import SoftTimeLimitExceeded
from django.test import SimpleTestCase

from nautobot_event_tracker.services.exceptions import LLMCallError, is_soft_time_limit


class _Group(Exception):
    """A stand-in for `ExceptionGroup`, which Python 3.10 does not have."""

    def __init__(self, exceptions):
        super().__init__("a group")
        self.exceptions = exceptions


class TestIsSoftTimeLimit(SimpleTestCase):
    """AC-10 - the signal arrives bare, wrapped by litellm, or inside a group."""

    def test_the_error_itself(self):
        self.assertTrue(is_soft_time_limit(SoftTimeLimitExceeded()))

    def test_the_error_as_a_cause(self):
        try:
            raise LLMCallError("The call failed.") from SoftTimeLimitExceeded()
        except LLMCallError as error:
            self.assertTrue(is_soft_time_limit(error))

    def test_the_error_as_a_context(self):
        try:
            try:
                raise SoftTimeLimitExceeded()
            except SoftTimeLimitExceeded:
                raise ValueError("A second fault.")  # pylint: disable=raise-missing-from
        except ValueError as error:
            self.assertIsNone(error.__cause__)
            self.assertTrue(is_soft_time_limit(error))

    def test_the_error_in_an_exception_group(self):
        group = _Group([ValueError("one"), SoftTimeLimitExceeded()])

        self.assertTrue(is_soft_time_limit(group))

    def test_a_different_error_is_not_the_signal(self):
        self.assertFalse(is_soft_time_limit(ValueError("no")))

    def test_a_cycle_ends(self):
        first, second = ValueError("first"), ValueError("second")
        first.__cause__ = second
        second.__cause__ = first

        self.assertFalse(is_soft_time_limit(first))
