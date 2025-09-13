# Documentation: fdev_tick_monitor.py

## Overview

`fdev_tick_monitor.py` is a background service module for monitoring the official Elite Dangerous BGS tick (galaxy tick) as published by "Zoy's" galtick.json service. It is designed for use in a multi-tenant BGS backend and is responsible for polling the tick status, detecting changes, and notifying each tenant's Discord webhook when a new tick is detected.

---

## Main Components

### 1. Tenant Configuration
- Loads tenant configuration from `tenant.json` at startup.
- Each tenant can have its own Discord webhook(s) for tick notifications.

### 2. Tick State Management
- Maintains the last known tick in the `last_tick` dictionary.
- On startup, initializes the tick state by fetching the current value from the galtick.json endpoint.

### 3. Polling and Scheduling
- Uses APScheduler's `BackgroundScheduler` to poll the tick endpoint every 5 minutes.
- The polling function fetches the latest tick and compares it to the last known value.
- If a new tick is detected, it updates the state and triggers Discord notifications.
- Scheduler is gracefully shut down on process exit using `atexit`.

### 4. Discord Notification
- For each tenant, retrieves the configured Discord webhook (type "bgs" by default).
- Sends a formatted message to the webhook when a new tick is detected.
- Handles HTTP errors and logs the result for each tenant.

### 5. Logging and Error Handling
- Uses the standard Python `logging` module for all actions and errors.
- All network and notification errors are caught and logged, ensuring robust operation.

---

## Usage
- Import and call `first_tick_check()` at application startup to initialize the tick state.
- Call `start_tick_watch_scheduler()` to start the background polling process.
- The module is designed to be used as part of a larger Flask or background service, not as a standalone script.

---

## Extensibility
- Additional notification channels (e.g., email, Slack) can be added by extending the notification logic.
- The polling interval can be adjusted by changing the `IntervalTrigger` parameter.
- The module can be adapted to monitor other tick sources or endpoints if needed.

---

## See Also
- `tenant.json`: Tenant configuration and Discord webhook URLs
- `app.py`: Main Flask application, which starts the tick monitor
- `fac_shoutout_scheduler.py`: Related scheduler for Discord notifications

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

