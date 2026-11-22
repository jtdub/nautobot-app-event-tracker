# Uninstall the App from Nautobot

Use these steps to remove the app from your Nautobot environment.

## Database Cleanup

Do this step before you remove the app from `nautobot_config.py`. Stop the [consumer](ingestion.md) first.

!!! warning "This deletes your data"
    The next command deletes each ticket and each other record of the app. This includes the update
    trail of each ticket. You cannot undo it. Make a database backup before you run the command.

Run this command to roll back each migration of the app:

```shell
nautobot-server migrate nautobot_event_tracker zero
```

The app makes no custom field and no relationship, so you do not need to remove either.

The command does not remove the `pgvector` extension from the database. Other tables in the
database can use a vector column.

## Remove App configuration

Remove the configuration that you added in `nautobot_config.py` from `PLUGINS` and `PLUGINS_CONFIG`.
Keep `nautobot_ai_models` in `PLUGINS` if a different app uses it.

## Uninstall the package

Remove the package:

```bash
$ pip3 uninstall nautobot-event-tracker
```

Then remove the `nautobot-event-tracker` line from `local_requirements.txt`.

## Restart Nautobot

Restart Nautobot, its workers and its scheduler:

```shell
sudo systemctl restart nautobot nautobot-worker nautobot-scheduler
```

Keep the consumer stopped. Remove its service unit, because the `eventconsumer` command does not exist after the uninstall.
