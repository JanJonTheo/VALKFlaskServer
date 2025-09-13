# Documentation: fac_conflict_scheduler.py

## Overview

`fac_conflict_scheduler.py` is a background scheduling module for the multi-tenant BGS Data backend. It is responsible for periodically triggering the reporting of faction conflicts for each tenant by calling the appropriate API endpoint in the main Flask server. The module uses APScheduler for scheduling, reads tenant configuration from `tenant.json`, and supports robust error handling and logging.

---

## Main Components

### 1. Tenant Configuration
- Loads all tenants from `tenant.json` at startup.
- Each tenant can have its own API key, API version, and Discord webhook(s).

### 2. Scheduler Setup
- Uses APScheduler's `BackgroundScheduler` with UTC timezone.
- For each tenant, schedules a job to trigger the `/api/discord/fac-in-conflict-current-tick` endpoint at 0:00, 6:00, 12:00, and 18:00 UTC.
- Each job is tenant-specific and uses the tenant's API key and version for authentication.
- Scheduler is gracefully shut down on process exit using `atexit`.

### 3. API Triggering Logic
- For each scheduled run, sends a POST request to the Flask server's `/api/discord/fac-in-conflict-current-tick` endpoint.
- Includes the tenant's API key and API version in the request headers.
- Logs the result of each request, including success, failure, and any HTTP errors.

### 4. Logging and Error Handling
- Uses the standard Python `logging` module for all actions and errors.
- All network and scheduling errors are caught and logged, ensuring robust operation.

---

## Usage
- Import and call `start_fac_conflict_scheduler(app, db)` from the main application to enable scheduled faction conflict reporting for all tenants.
- Designed for use in a multi-tenant environment, with all operations performed per-tenant.

---

## Extensibility
- Additional scheduling times or endpoints can be added by modifying the scheduler setup.
- The notification logic can be extended to support other channels or reporting formats.

---

## See Also
- `tenant.json`: Tenant configuration and Discord webhook URLs
- `app.py`: Main Flask application, which exposes the reporting endpoint
- `fac_in_conflict.py`: Implements the API endpoint for reporting faction conflicts

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

