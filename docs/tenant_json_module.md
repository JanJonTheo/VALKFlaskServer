# Documentation: tenant.json

## Overview

`tenant.json` is the central configuration file for multi-tenant support in the BGS Data backend. It defines all tenants (organizations, factions, or groups) that use the backend, including their API keys, database URIs, Discord webhooks, and other metadata. This file enables the backend to dynamically route requests, manage data isolation, and send notifications per tenant.

---

## Main Components

### 1. Tenant Structure
Each entry in the JSON array represents a tenant and includes the following fields:

- **name**: Human-readable name of the tenant (e.g., "Your Tenant Name").
- **api_key**: Unique API key for authenticating requests from this tenant.
- **api_version**: The API version this tenant is expected to use (enforces compatibility).
- **db_uri**: SQLAlchemy database URI for the tenant's data (e.g., `sqlite:///db/bgs_data_your_tenant.db`).
- **discord_webhooks**: Object containing Discord webhook URLs for various notification types:
  - `bullis`: General notifications
  - `shoutout`: Daily/periodic summaries
  - `conflict`: Faction conflict notifications
  - `bgs`: BGS tick and status updates
  - `shoutout_alt`: Alternate summary channel
- **faction_name**: Name of the main faction for this tenant (used in reporting and filtering).
- **faction_logo**: Path or URL to the faction's logo (used in UI or notifications).

### 2. Example Entry
```json
{
  "name": "Your Tenant Name",
  "api_key": "your-unique-api-key-here",
  "api_version": "1.6.0",
  "db_uri": "sqlite:///db/bgs_data_your_tenant.db",
  "discord_webhooks": {
    "bullis": "...",
    "shoutout": "...",
    "conflict": "...",
    "bgs": "...",
    "shoutout_alt": "..."
  },
  "faction_name": "Your Faction Name",
  "faction_logo": "/assets/eic.png"
}
```

---

## Usage
- The backend loads `tenant.json` at startup to build the list of tenants.
- Each API request is authenticated using the `api_key` and routed to the correct tenant context.
- Database connections, Discord notifications, and reporting are all tenant-specific, using the values from this file.
- Tenants can be added, removed, or updated by editing this file and restarting the backend.

---

## Extensibility
- Additional fields (e.g., more webhooks, feature flags, custom settings) can be added per tenant as needed.
- The structure supports any number of tenants, each with their own isolated configuration.

---

## Security
- API keys should be kept secret and rotated regularly.
- The file should not be exposed publicly or committed to public version control.

---

## See Also
- `.env`: Global environment configuration (shared secrets, DB URIs, etc.)
- `app.py`: Main Flask application, loads and uses tenant configuration
- All scheduler and notification modules (use tenant-specific webhooks)

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

