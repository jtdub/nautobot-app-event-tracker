"""Nautobot development configuration file."""

import os
import sys

from nautobot.core.settings import *  # noqa: F403  # pylint: disable=wildcard-import,unused-wildcard-import
from nautobot.core.settings_funcs import is_truthy

#
# Debug
#

DEBUG = is_truthy(os.getenv("NAUTOBOT_DEBUG", "false"))
_TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

if DEBUG and not _TESTING:
    DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda _request: True}

    if "debug_toolbar" not in INSTALLED_APPS:  # noqa: F405
        INSTALLED_APPS.append("debug_toolbar")  # noqa: F405
    if "debug_toolbar.middleware.DebugToolbarMiddleware" not in MIDDLEWARE:  # noqa: F405
        MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405

#
# Misc. settings
#

ALLOWED_HOSTS = os.getenv("NAUTOBOT_ALLOWED_HOSTS", "").split(" ")
SECRET_KEY = os.getenv("NAUTOBOT_SECRET_KEY", "")

#
# Database
#

# PostgreSQL is the only supported backend; see ADR 0003.
nautobot_db_engine = "django.db.backends.postgresql"
DATABASES = {
    "default": {
        "NAME": os.getenv("NAUTOBOT_DB_NAME", "nautobot"),  # Database name
        "USER": os.getenv("NAUTOBOT_DB_USER", ""),  # Database username
        "PASSWORD": os.getenv("NAUTOBOT_DB_PASSWORD", ""),  # Database password
        "HOST": os.getenv("NAUTOBOT_DB_HOST", "localhost"),  # Database server
        "PORT": os.getenv("NAUTOBOT_DB_PORT", "5432"),  # Database port
        "CONN_MAX_AGE": int(os.getenv("NAUTOBOT_DB_TIMEOUT", "300")),  # Database timeout
        "ENGINE": nautobot_db_engine,
    }
}

#
# Redis
#

# The django-redis cache is used to establish concurrent locks using Redis.
# Inherited from nautobot.core.settings
# CACHES = {....}

#
# Celery settings are not defined here because they can be overloaded with
# environment variables. By default they use `CACHES["default"]["LOCATION"]`.
#

#
# Logging
#

LOG_LEVEL = "DEBUG" if DEBUG else "INFO"

# Verbose logging during normal development operation, but quiet logging during unit test execution
if not _TESTING:
    LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "normal": {
                "format": "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s : %(message)s",
                "datefmt": "%H:%M:%S",
            },
            "verbose": {
                "format": "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-20s %(filename)-15s %(funcName)30s() : %(message)s",
                "datefmt": "%H:%M:%S",
            },
        },
        "handlers": {
            "normal_console": {
                "level": "INFO",
                "class": "logging.StreamHandler",
                "formatter": "normal",
            },
            "verbose_console": {
                "level": "DEBUG",
                "class": "logging.StreamHandler",
                "formatter": "verbose",
            },
        },
        "loggers": {
            "django": {"handlers": ["normal_console"], "level": "INFO"},
            "nautobot": {
                "handlers": ["verbose_console" if DEBUG else "normal_console"],
                "level": LOG_LEVEL,
            },
        },
    }

#
# Apps
#

# Enable installed Apps. Add the name of each App to the list.
PLUGINS = ["nautobot_event_tracker"]

# Apps configuration settings. These settings are used by various Apps that the user may have installed.
# Each key in the dictionary is the name of an installed App and its value is a dictionary of settings.
PLUGINS_CONFIG = {
    "nautobot_event_tracker": {},
}

# Event ingestion. Two brokers, one payload shape.
#
# The lab points the consumer at Redpanda and reads what its syslog bridge produces. Without it, the
# consumer reads the same shape off the Redis this stack already runs - so `invoke start`, then
# `invoke eventconsumer`, then `invoke send-test-event` produces a ticket on any machine, without
# containerlab and without 8 GB of memory. Redis pub/sub drops anything published while nothing is
# listening, which is exactly why ADR 0004 calls it the development broker and Kafka the reference
# one; for watching a message become a ticket it is enough.
#
# The topic configuration is the lab's either way, so what you learn about the field map with Redis
# is true of the lab as well.


def _lab_ingestion():
    """Load the lab's ingestion block, wherever this configuration file happens to be running from.

    In the container this file is `/opt/nautobot/nautobot_config.py` and the repository is mounted
    at `/source`; run natively, it is the file in `development/` with the lab's beside it. Looking
    only next to this file was right in one of those and a crash in the other - and this file is
    imported by every process in the stack, so it took all of them down at once.

    Returns `None` when neither is there, which is a copy of this file somewhere of its own. Nothing
    is configured then, rather than something invented.
    """
    import importlib.util  # pylint: disable=import-outside-toplevel

    here = os.path.dirname(os.path.abspath(__file__))
    for directory in (os.path.join(here, "containerlab"), "/source/development/containerlab"):
        path = os.path.join(directory, "nautobot_config_lab.py")
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location("nautobot_config_lab", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.LAB_INGESTION
    return None


LAB_INGESTION = _lab_ingestion()

# Where LAB_INGESTION is None nothing is configured, which is the right answer for a copy of this
# file with no checkout behind it: the app is then installed and waiting to be pointed at a broker.
if LAB_INGESTION is not None and is_truthy(os.getenv("EVENT_TRACKER_LAB", "false")):
    PLUGINS_CONFIG["nautobot_event_tracker"]["ingestion"] = LAB_INGESTION
elif LAB_INGESTION is not None:
    PLUGINS_CONFIG["nautobot_event_tracker"]["ingestion"] = {
        "consumer": "redis",
        "consumer_name": "development",
        "redis": {
            # Database 2: Nautobot's cache and Celery have 0 and 1, and a consumer subscribing over
            # the top of either is a debugging session nobody enjoys.
            "url": f"redis://{os.getenv('NAUTOBOT_REDIS_HOST', 'redis')}:{os.getenv('NAUTOBOT_REDIS_PORT', '6379')}/2",
            "password": os.getenv("NAUTOBOT_REDIS_PASSWORD", ""),
        },
        # Short, so a developer watching the stats page sees a bucket roll while still looking.
        "stats_bucket_seconds": 60,
        "stats_flush_seconds": 5,
        "topics": LAB_INGESTION["topics"],
    }
