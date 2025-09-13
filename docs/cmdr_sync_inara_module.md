# Documentation: cmdr_sync_inara.py

## Overview

`cmdr_sync_inara.py` is a background synchronization and reporting module for the multi-tenant BGS Data backend. It is responsible for periodically synchronizing commander (Cmdr) profiles from the Inara API for each tenant, updating the local database with the latest ranks, squadron, and profile information, and sending summary reports to tenant-specific Discord webhooks. The module uses APScheduler for scheduling, SQLAlchemy for database access, and supports robust error handling and logging.

---

## Main Components

### 1. Tenant Configuration
- Loads all tenants from `tenant.json` at startup.
- Each tenant can have its own database URI, Inara API key, and Discord webhooks (e.g., for reporting sync results).

### 2. Inara API Integration
- **fetch_inara_profile(cmdr_name, inara_api_key)**: Fetches a commander's profile from the Inara API, including ranks, squadron, and profile URL. Handles API rate limits and errors.
- Uses a personal or tenant-specific Inara API key for authentication.

### 3. Commander Synchronization Logic
- **sync_cmdrs_with_inara(db, inara_api_key, tenant_name)**: For each unique commander in the event table, fetches the latest profile from Inara and updates or inserts the Cmdr record in the database. Handles rate limiting by pausing between requests and on errors.
- Commits changes to the database after each update, with error handling and rollback on failure.

### 4. Multi-Tenant Sync Task
- **run_cmdr_sync_task(app, db=None)**: For each tenant, creates a database session and runs the commander sync logic. Collects log output and sends a summary to the tenant's Discord webhook (if configured).
- Handles missing configuration, connection errors, and Discord API errors.

### 5. Scheduling
- **start_cmdr_sync_scheduler(app, db)**: Schedules the commander sync task to run daily at 01:00 UTC using APScheduler.
- Ensures only one scheduler instance is running at a time.

### 6. Logging and Error Handling
- Uses the standard Python `logging` module for all actions and errors.
- All network, database, and notification errors are caught and logged, ensuring robust operation.
- Rate limiting is handled by pausing 60 seconds between requests and on errors.

---

## Usage
- Import and call `start_cmdr_sync_scheduler(app, db)` from the main application to enable daily commander synchronization for all tenants.
- Can also call `run_cmdr_sync_task(app, db)` directly for ad-hoc synchronization.
- Designed for use in a multi-tenant environment, with all operations performed per-tenant.

---

## Extensibility
- Additional commander data fields can be added by extending the profile extraction logic.
- The scheduling logic can be adjusted to support different intervals or additional jobs.
- Can be adapted to support other notification channels (e.g., Slack, email) by extending the notification logic.

---

## See Also
- `tenant.json`: Tenant configuration and Discord webhook URLs
- `app.py`: Main Flask application, which starts the scheduler
- `models.py`: Defines the Cmdr and Event models used for synchronization
- `fac_shoutout_scheduler.py`: Related scheduler for Discord notifications

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

