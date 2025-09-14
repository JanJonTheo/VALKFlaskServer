# Documentation: app.py

## Overview

`app.py` is the central Flask API module for the multi-tenant BGS Data backend. It provides REST endpoints for events, activities, summaries, Discord integration, system queries, and protected factions. The multi-tenant architecture allows the use of different databases and webhooks per tenant.

---

## Main Components

### 1. Initialization and Configuration
- **Flask App**: Initialization of the Flask application.
- **Tenant Configuration**: Loading tenants from `tenant.json`.
- **Logging**: Rotating file and stream logging.
- **Database Setup**: Dynamic DB configuration per tenant (SQLite/Postgres etc.).
- **API Version**: Default and per-tenant configurable.

### 2. Multi-Tenant Mechanism
- **get_tenant_by_apikey(apikey)**: Finds the tenant by API key.
- **set_tenant_db_config(tenant)**: Sets the DB session and engine for the current request context.
- **require_api_key**: Decorator, checks API key, sets tenant and DB, checks API version.

### 3. Event and Activity Endpoints
- **/events [POST]**: Stores events, processes various event types, detects tick changes.
- **/activities [PUT]**: Stores activities and associated systems/factions.

### 4. Summaries and Leaderboards
- **/api/summary/<key> [GET]**: Various evaluations (market, missions, bounties, etc.) with time filter.
- **/api/summary/top5/<key> [GET]**: Top-5 lists for different categories.
- **/api/summary/leaderboard [GET]**: Complex leaderboard with many metrics.

### 5. Discord Integration
- **/api/summary/discord/top5all [POST]**: Sends top-5 statistics to the tenant's Discord webhook.
- **/api/summary/discord/tick [POST]**: Triggers daily tick statistics to Discord.
- **/api/summary/discord/syntheticcz [POST]**: CZ summary for space CZs to Discord.
- **/api/summary/discord/syntheticgroundcz [POST]**: CZ summary for ground CZs to Discord.

### 6. System and Faction Queries
- **/api/system-summary/ [GET]**: Provides system data from the EDDN DB, with many filter options.
- **/api/protected-faction [GET, POST, PUT, DELETE]**: Management of protected factions per tenant.
- **/api/protected-faction/systems [GET]**: Lists all system names from the EDDN DB.

### 7. Other Endpoints
- **/api/table/<tablename> [GET]**: Returns arbitrary table contents (only for existing tables).
- **/api/sync/cmdrs [POST]**: Synchronizes Cmdr data with Inara.
- **/api/login [POST]**: User login authentication.

### 8. Background Processes and Schedulers
- **initialize_all_tenant_databases()**: Initializes all tenant DBs and tables.
- **get_latest_tickid()**: Retrieves the last tick per tenant.
- **Multiprocess Start**: Starts EDDN client, shoutout scheduler, tick watch, conflict scheduler, cmdr sync.

---

## Postman Collection
- Please refer to the provided Postman collection for testing and interacting with the API endpoints.

---
## Notes on Multi-Tenant Architecture
- Each request is assigned to a tenant based on the API key.
- DB session and engine are set dynamically per request.
- Webhooks (e.g., for Discord) are read per tenant.
- DB structure initialization is performed for all tenants at startup.

---

## Error Handling
- Invalid API keys, DB issues, and validation errors are answered with appropriate HTTP status codes and error messages.
- Logging is performed for all critical operations.

---

## Extensibility
- New endpoints can easily be made multi-tenant capable using the `@require_api_key` decorator.
- New event or evaluation types can be integrated into the existing structures.

---

## See Also
- `models.py`: Database models
- `tenant.json`: Tenant configuration
- `fac_shoutout_scheduler.py`, `fdev_tick_monitor.py`, `fac_conflict_scheduler.py`: Schedulers and background jobs

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**
