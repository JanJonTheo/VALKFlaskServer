# Documentation: fac_shoutout_scheduler.py

## Overview

`fac_shoutout_scheduler.py` is a background scheduling and reporting module for the multi-tenant BGS Data backend. It is responsible for generating daily and periodic summaries of in-game activities (such as market events, missions, combat, and conflict zones) and sending formatted reports to each tenant's Discord webhook. The module uses APScheduler for scheduling, SQLAlchemy for database access, and supports flexible reporting for both space and ground conflict zones.

---

## Main Components

### 1. Logging Setup
- Initializes a rotating file logger (`tick_scheduler.log`) for all scheduler and reporting actions.
- Logs all important events, errors, and Discord notification results.

### 2. Tenant Management
- Loads tenant configuration from `tenant.json`.
- Each tenant can have its own database URI and Discord webhooks (e.g., for shoutouts).
- Helper functions to retrieve tenant-specific engines and webhooks.

### 3. Summary Generation
- **format_discord_summary**: Generates a daily summary (for the previous day) of key activities (market, missions, influence, bounties, exploration, etc.) for each tenant. Formats the results and sends them to the tenant's Discord webhook.
- **send_syntheticcz_summary_to_discord**: Generates and sends a summary of space conflict zone (CZ) activity for a given period (e.g., last day, week, month) per tenant/system. Includes per-commander breakdowns.
- **send_syntheticgroundcz_summary_to_discord**: Similar to the above, but for ground conflict zones, including settlement and commander breakdowns.

### 4. Scheduling
- **start_scheduler**: Starts the APScheduler background scheduler with three main jobs:
  - Daily summary at 00:00 UTC (`format_discord_summary`).
  - Space CZ summary at 00:01 UTC (`send_syntheticcz_summary_to_discord`).
  - Ground CZ summary at 00:02 UTC (`send_syntheticgroundcz_summary_to_discord`).
- Ensures only one scheduler instance is running at a time.
- Graceful shutdown on process exit using `atexit`.

### 5. SQL and Data Handling
- Uses parameterized SQL queries for all reporting, with time period filters.
- Aggregates and formats data for Discord-friendly output, including top lists and detailed breakdowns.
- Handles missing data, connection errors, and skips tenants with incomplete configuration.

---

## Usage
- Import and call `start_scheduler(app, db)` from the main application to enable all scheduled Discord reporting.
- Can also call `format_discord_summary`, `send_syntheticcz_summary_to_discord`, or `send_syntheticgroundcz_summary_to_discord` directly for ad-hoc reporting.
- Designed for use in a multi-tenant environment, with all operations performed per-tenant.

---

## Extensibility
- Additional summary types or Discord notification formats can be added by extending the summary functions.
- The scheduling logic can be adjusted to support different intervals or additional jobs.
- Can be adapted to support other notification channels (e.g., Slack, email) by extending the notification logic.

---

## See Also
- `tenant.json`: Tenant configuration and Discord webhook URLs
- `app.py`: Main Flask application, which starts the scheduler
- `fdev_tick_monitor.py`: Related tick monitoring and notification module

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

