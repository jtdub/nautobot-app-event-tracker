# Upgrading the App

Use these steps to upgrade the app in your Nautobot environment.

## Upgrade Guide

A new release can change the data models of the app. The upgrade runs the database migrations for
these changes.

1. Update the `nautobot-event-tracker` package with `pip`, in the runtime environment of your
   Nautobot installation:

    ```shell
    pip install --upgrade nautobot-event-tracker
    ```

2. Run the Post Upgrade command. The command runs the migrations and clears the cache:

    ```shell
    nautobot-server post_upgrade
    ```

3. Restart Nautobot and its workers and scheduler:

    ```shell
    sudo systemctl restart nautobot nautobot-worker nautobot-scheduler
    ```

4. Restart the consumer. See [Running the Event Consumer](ingestion.md).

!!! note
    On a managed PostgreSQL, a migration can fail because the database user cannot create the
    `pgvector` extension. If this happens, ask a superuser to run `CREATE EXTENSION vector;` and
    run step 2 again. See [Prerequisites](install.md#prerequisites).
