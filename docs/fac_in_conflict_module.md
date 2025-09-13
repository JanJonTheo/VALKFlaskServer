# Documentation: fac_in_conflict.py

## Overview

`fac_in_conflict.py` implements the API endpoints and logic for reporting and notifying about faction conflicts in the multi-tenant BGS Data backend. It provides endpoints for retrieving current and previous tick conflicts for a tenant's faction and for sending formatted conflict reports to the tenant's Discord webhook. The module supports robust data extraction, multi-tenant logic, and integration with the tick monitor.

---

## Main Components

### 1. Tenant Configuration
- Loads all tenants from `tenant.json` at startup.
- Provides a helper function to look up a tenant by API key.

### 2. Conflict Extraction Logic
- **extract_tenant_conflicts**: For a given tick and faction, extracts all relevant conflict data from the event table.
  - Parses raw JSON event data, handles both JSON and Python literal formats.
  - Identifies conflicts involving the tenant's faction.
  - Aggregates data per system, including war type, factions, stakes, won days, and involved commanders.
  - Ensures only the latest event per system is used.

### 3. API Endpoints
- **/api/fac-in-conflict-current-tick [GET]**: Returns all current and previous tick conflicts for the tenant's faction, including system, war type, factions, stakes, won days, and involved commanders. Requires API key authentication.
- **/api/discord/fac-in-conflict-current-tick [POST]**: Sends a formatted summary of current tick conflicts to the tenant's Discord webhook. Requires API key authentication. Handles Discord API responses and error reporting.

### 4. Integration with Tick Monitor
- Uses the `last_tick` value from `fdev_tick_monitor.py` to include the current galaxy tick in reports.

### 5. Error Handling
- Handles missing or invalid API keys, insufficient tick data, and Discord API errors.
- All errors are returned as JSON responses with appropriate HTTP status codes.

---

## Usage
- Register the routes by calling `register_fac_conflict_routes(app, db, require_api_key)` in the main Flask application.
- Endpoints are protected by the `require_api_key` decorator to ensure tenant isolation and security.
- Designed for use in a multi-tenant environment, with all operations performed per-tenant.

---

## Extensibility
- Additional endpoints or reporting formats can be added by extending the route registration logic.
- The conflict extraction logic can be adapted to support new event types or data sources.

---

## See Also
- `tenant.json`: Tenant configuration and Discord webhook URLs
- `app.py`: Main Flask application, which registers the conflict routes
- `fac_conflict_scheduler.py`: Scheduler that triggers Discord notifications for conflicts
- `fdev_tick_monitor.py`: Provides the current galaxy tick value

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

