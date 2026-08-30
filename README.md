![VALT Logo](static/VALK_logo.jpg)

# Flask API for BGS-Tally Data

## Project Description

This project provides a RESTful API for receiving, storing, and processing Background Simulation (BGS), Colonisation, and Thargoid War activity data for the game Elite Dangerous. It is designed to integrate with BGS-Tally and other tools to support faction management, colonisation tracking, and war tracking.

---

## Multi-Tenant Architecture

This backend supports multiple tenants (factions, groups, or organizations), each with their own database, API key, and Discord webhooks. Tenant configuration is managed via `tenant.json`. All endpoints and background jobs are multi-tenant aware, ensuring data isolation and per-tenant notifications.

---

## Features

- Receive BGS, Colonisation, and Thargoid War activity data via POST and PUT requests
- Store incoming data in a tenant-specific database
- Send notifications to Discord via tenant-specific webhooks
- Scheduled shoutouts, tick monitoring, and conflict reporting per tenant
- Commander synchronization with Inara API
- EDDN data ingestion and system/faction info endpoints
- Objectives and protected faction management
- Multi-tenant authentication and API versioning

---

## API Endpoints

The following API endpoints are available. Their specification is based on the description in `bgs_tally_openapi.json`, which forms the basis for development. All API functions are also included in `postman_collection.json` for use with the Postman software.

**Events**

- `POST /events` : Submit one or more Journal or Synthetic Events

**Activities**

- `PUT /activities` : Add or update activity for a given tick
- `POST /api/manual/activity` : Submit one manually captured Discord form activity. The bot must not send `tick`, `tickid`, `ticktime`, or `tick_mode`; the server always resolves the current tick at save time. Existing `/activities` and `/events` behavior remains unchanged.
- `POST /api/manual/activity/undo` : Delete the Discord user's last saved manual activity in the current server-resolved tick.
- `POST /api/manual/activity/clear-ct` : Delete all saved manual activities for the Discord user in the current server-resolved tick.
- `GET /api/manual/lookup/systems?q=<query>&limit=25` : Read-only Discord autocomplete for systems.
- `GET /api/manual/lookup/factions?system=<system>&q=<query>&limit=25` : Read-only Discord autocomplete for factions, optionally scoped to a system.
- `GET /api/manual/lookup/cmdrs?q=<query>&limit=25` : Read-only Discord autocomplete for commander names.

**Manual Discord Activities**

`POST /api/manual/activity` is the external save endpoint for manual VALKBot BGS activity submissions. It authenticates with the same `apikey` and `apiversion` headers as the existing API, resolves the current tick server-side, resolves `SystemAddress` when possible, writes `manual_activity_submission`, `activity/system/faction`, and matching `event` detail rows in one DB transaction, then posts a BGS-Tally-style Discord webhook server-side after the commit. Webhook failures do not roll back the saved activity. The manual activity Discord webhook is tenant-specific and is configured in `tenant.json`, not `.env`.

`POST /api/manual/activity/undo` and `POST /api/manual/activity/clear-ct` use the same tenant authentication and resolve the current tick server-side. The client must not send tick fields. Ownership is determined by `discord.user_id`; only submissions with `status == "saved"` are affected. Deletion is soft: submissions remain in `manual_activity_submission` with `status = "deleted"` and audit JSON in `error_message`, while generated synthetic `event` detail rows and parent `event` rows are removed and activity aggregates are reversed without going below zero. Webhook messages are kept consistent by updating the remaining grouped message or deleting the message if no saved submissions remain.

For `undo`, the server deletes the latest saved submission for the Discord user in the current tick. If the latest `submission_id` ends with `:combat_bond`, the server removes that suffix and deletes all saved submissions with the same base id, so the CZ activity and its generated combat-bond event are undone together. For `clear-ct`, the server deletes every saved submission for that Discord user in the current tick.

Discord autocomplete uses the read-only `/api/manual/lookup/...` endpoints. They never save activity, events, submissions, or send webhooks, and they never return tick data. Responses are capped at 25 entries and contain only autocomplete-safe fields:

```json
[
  {"name": "Synuefe OX-Y b47-0", "address": 1234567890123}
]
```

```json
[
  {"name": "Valkyries of Trade", "state": "None"}
]
```

```json
[
  {"name": "JanJonTeo"}
]
```

Example payload:

```json
{
  "submission_id": "discord:123:456:987",
  "source": "discord_modal",
  "discord": {
    "guild_id": "123",
    "channel_id": "456",
    "user_id": "789",
    "message_id": null,
    "interaction_id": "987"
  },
  "cmdr": "JanJonTeo",
  "client_timestamp": "2026-05-27T13:49:00Z",
  "system": {
    "name": "Synuefe OX-Y b47-0",
    "address": null
  },
  "faction": {
    "name": "Valkyries of Trade",
    "state": "None"
  },
  "activity": {
    "type": "bounty_voucher",
    "amount": 2500000,
    "count": null,
    "influence": null,
    "cz_type": null,
    "settlement": null
  },
  "note": "Manual entry from Discord modal"
}
```

Supported `activity.type` values: `bounty_voucher`, `combat_bond`, `exploration_sale`, `mission_completed`, `mission_failed`, `space_cz`, `ground_cz`, `scenario`, `murder_space`, `murder_ground`, `black_market_trade`, `market_buy`, `market_sell`.

Delete request payload for both `/undo` and `/clear-ct`:

```json
{
  "source": "discord_modal",
  "discord": {
    "guild_id": "123",
    "channel_id": "456",
    "user_id": "789",
    "interaction_id": "undo-987"
  }
}
```

Successful delete response:

```json
{
  "status": "deleted",
  "operation": "undo",
  "tickid": "tick-1",
  "ticktime": "2026-05-27T09:00:00Z",
  "deleted_count": 1,
  "deleted_activities": [
    {
      "submission_id": "discord:123:456:987",
      "cmdr": "JanJonTeo",
      "system": "Synuefe OX-Y b47-0",
      "faction": "Valkyries of Trade",
      "activity_type": "bounty_voucher",
      "amount": 2500000,
      "count": null,
      "influence": null,
      "cz_type": null,
      "settlement": null,
      "event_ids": [123],
      "captured_at": "2026-05-27T13:49:00Z",
      "webhook_status": "posted"
    }
  ],
  "webhook_status": "deleted",
  "webhook_error": null
}
```

If no matching saved submission exists, the delete endpoints return HTTP 200 with `status: "no_op"`, `deleted_count: 0`, an empty `deleted_activities` list, and `webhook_status: "skipped"`.

Manual activity webhook tenant configuration:

```json
{
  "name": "Tenant Name",
  "discord_webhooks": {
    "manual_activity": "https://discord.com/api/webhooks/..."
  },
  "manual_activity_webhook": {
    "enabled": true,
    "username": "BGS-Tally Manual",
    "avatar_url": "",
    "timeout_seconds": 10
  }
}
```

If `manual_activity_webhook.enabled` is `false`, the submission is saved and `webhook_status` is `disabled`. If `discord_webhooks.manual_activity` is empty or missing, the submission is saved and `webhook_status` is `not_configured`.

**Colonisation APIs**

All Colonisation endpoints require the regular `apikey` and `apiversion` headers.

- `GET /api/colonisation/targets?cmdr=JanJonTheo`
- `GET /api/colonisation/summary?cmdr=JanJonTheo&market_id=3965326082`
- `GET /api/colonisation/summary/text?cmdr=JanJonTheo&market_id=3965326082`
- `GET /api/colonisation/contributions?group_by=construction&period=ct`
- `GET /api/colonisation/constructions?status=open&period=ct`
- `GET /api/colonisation/deliveries?cmdr=JanJonTheo&market_id=3965326082`
- `POST /api/colonisation/deliveries`
- `POST /api/colonisation/status`

`summary` is built from received BGS-Tally journal events (`ColonisationConstructionDepot`, `ColonisationContribution`, and nearby `Docked`/`Location` events). It returns the construction target, market id, commodity rows (`Need`, `Prov`, `Rem`, `State`), totals, session delivery, central delivery total, cargo count, reason, and a preformatted text block matching the EDAPGui-ANKe Colonisation summary.

`contributions` aggregates received `ColonisationContribution` journal events. Use `group_by=construction` for Construction > Cmdr or `group_by=cmdr` for Cmdr > Construction. Filters: `cmdr` or comma-separated `cmdrs`, `market_id`/`market_ids`, `construction` or comma-separated `constructions`, `period` (`ct`, `lt`, `cd`, `ld`, `cw`, `lw`, `cm`, `lm`, `2m`, `y`, `all`), or custom dates with `from=YYYY-MM-DD&to=YYYY-MM-DD`. The response includes totals, flat records, grouped rows, commodity totals, and a preformatted `text` report for Discord-style clients.

`constructions` returns the latest `ColonisationConstructionDepot` snapshot per market and evaluates `status=open|finished|failed|all`. It combines required/provided/remaining commodity state with recorded `ColonisationContribution` history, including contributor Cmdrs and first/last delivery timestamps. Contribution filters (`cmdr`, `market_ids`, `constructions`, `period`, `from`, `to`) only affect recorded contributor rows; the open/finished status is based on the latest construction snapshot.

Clients can post delivery records to `/api/colonisation/deliveries` with EDAPGui-compatible field names such as `DeliveryId`, `SessionId`, `CmdrName`, `TargetName`, `TargetSystem`, `TargetStation`, `ConstructionMarketID`, `CommodityKey`, `Name_Localised`, `Quantity`, and `VerificationSource`. `DeliveryId` is idempotent, so retrying the same delivery does not duplicate totals.

Clients can post their latest assist state to `/api/colonisation/status` with `StatusId`, `SessionId`, `ClientId`, `CmdrName`, `TargetName`, `TargetSystem`, `TargetStation`, `ConstructionMarketID`, `Phase`, `Reason`, and `CargoCount`. The latest matching status is used by `summary` for `Reason`, `SessionDelivered`, and `cargo`.

**Summary APIs**

- `GET /api/summary/market-events`
- `GET /api/summary/missions-completed`
- `GET /api/summary/missions-failed`
- `GET /api/summary/bounty-vouchers`
- `GET /api/summary/combat-bonds`
- `GET /api/summary/influence-by-faction`
- `GET /api/summary/influence-eic`
- `GET /api/summary/exploration-sales`
- `GET /api/summary/bounty-fines`
- `GET /api/bounty-vouchers`
- `GET /api/syntheticcz-summary`
- `GET /api/syntheticgroundcz-summary`

**Top 5 APIs**

- `GET /api/summary/top5/market-events`
- `GET /api/summary/top5/missions-completed`
- `GET /api/summary/top5/bounty-vouchers`
- `GET /api/summary/top5/combat-bonds`
- `GET /api/summary/top5/influence-eic`
- `GET /api/summary/top5/exploration-sales`

**Discord Integration**

- `POST /api/summary/discord/top5all`
- `POST /api/summary/discord/tick`
- `POST /api/summary/discord/syntheticcz`
- `POST /api/summary/discord/syntheticgroundcz`

**Database Tables**

- `GET /api/table/event`
- `GET /api/table/market_buy_event`
- `GET /api/table/activity`
- `GET /api/table/cmdr`
- `GET /api/table/objective`
- `GET /api/table/objective_target`
- `GET /api/table/objective_target_settlement`

**Leaderboard & Recruits**

- `GET /api/summary/leaderboard`
- `GET /api/summary/recruits`

**Objectives**

- `POST /api/objectives`
- `GET /api/objectives`
- `GET /api/objectives?system=Sol`
- `GET /api/objectives?faction=Federal Navy`
- `GET /api/objectives?active=true`
- `DELETE /api/objectives/<id>`
- `GET /objectives`
- `POST /objectives`
- `DELETE /objectives/<id>`

**Authentication**

- `POST /api/login`

**Debug & Sync**

- `POST /api/debug/tick-change`
- `POST /api/sync/cmdrs`

**Discovery & Health**

- `GET /discovery`

**EDDN System & Faction Data**

- `GET /api/system-summary/` : Query system info, factions, conflicts, powerplay (with filters)
- `GET /api/protected-faction` : List protected factions
- `POST /api/protected-faction` : Create protected faction
- `PUT /api/protected-faction/<id>` : Update protected faction
- `DELETE /api/protected-faction/<id>` : Delete protected faction
- `GET /api/protected-faction/systems` : List all system names

**Conflict Reporting**

- `GET /api/fac-in-conflict-current-tick` : Get current/previous tick conflicts for tenant's faction
- `POST /api/discord/fac-in-conflict-current-tick` : Send conflict summary to Discord

---

## Background Services & Schedulers

- **Tick Monitor**: Monitors the official BGS tick and notifies tenants via Discord
- **Shoutout Scheduler**: Sends daily/periodic summaries to Discord
- **Conflict Scheduler**: Triggers conflict reporting for each tenant
- **Cmdr Sync Scheduler**: Syncs commander profiles from Inara
- **EDDN Client**: Ingests real-time system/faction data from EDDN

---

## Configuration Files

- `.env` : Global environment variables (API version, global service settings, etc.)
- `tenant.json` : Per-tenant configuration (API keys, DB URIs, Discord webhooks, including `discord_webhooks.manual_activity`, etc.)

---

## Documentation

Extensive module documentation is available in the `docs/` folder:

- `docs/app_module.md` : Main Flask app and API
- `docs/models_module.md` : Main database models
- `docs/models_eddn_module.md` : EDDN data models
- `docs/eddn_client_module.md` : EDDN client
- `docs/fdev_tick_monitor_module.md` : Tick monitor
- `docs/fac_shoutout_scheduler_module.md` : Shoutout scheduler
- `docs/fac_conflict_scheduler_module.md` : Conflict scheduler
- `docs/fac_in_conflict_module.md` : Conflict API endpoints
- `docs/cmdr_sync_inara_module.md` : Cmdr sync with Inara
- `docs/tenant_json_module.md` : Tenant configuration
- `docs/.env_module.md` : Environment configuration

---

## Installation

### Option 1: Docker Compose (Recommended)

The easiest way to run both the Flask API and Streamlit dashboard together is using Docker Compose.

**Prerequisites:**

- Docker and Docker Compose installed
- Both `VALKFlaskServer` and `VALKStreamlitDashboard` repositories cloned as sibling directories

**Directory structure should be:**

```
parent-folder/
├── VALKFlaskServer/          # This repository
│   ├── docker-compose.yml   # Main Docker Compose file
│   ├── Dockerfile
│   └── ...
└── VALKStreamlitDashboard/   # Sibling repository
    ├── Dockerfile
    └── ...
```

**Setup:**

1. **Create environment files**:

   ```bash
   # In VALKFlaskServer directory
   cp .env-template .env
   # In VALKStreamlitDashboard directory
   cp ../VALKStreamlitDashboard/.env-template ../VALKStreamlitDashboard/.env
   ```

   Edit both `.env` files with your configuration.

2. **Start the services**:

   ```bash
   # From VALKFlaskServer directory
   docker-compose up -d
   ```

3. **Access the services**:
   - Flask API: <http://localhost:5000>
   - Streamlit Dashboard: <http://localhost:8501>

**First Run Setup:**
On the first execution, the system automatically:

- Creates the SQLite database in a persistent Docker volume
- Sets up database tables (`setup_db.py`)
- Creates the admin user (`setup_users.py`) - username: `admin`, password: `passAdmin`

**Useful Commands:**

```bash
# View logs
docker-compose logs -f

# Stop services
docker-compose down

# Rebuild containers
docker-compose up --build

# Test setup
./test_setup.ps1  # Windows
./test_setup.sh   # Linux/macOS
```

### Option 2: Manual Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/yourusername/VALKFlaskServer.git
   cd VALKFlaskServer
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Set up the database**

   ```bash
   python setup_db.py
   python setup_users.py
   ```

4. **Create a `.env` file for production**
   You can use the provided `.env-template` file as a starting point. Copy it and rename to `.env`:

   ```bash
   cp .env-template .env
   ```

   Then edit the values in `.env` to match your production environment.

5. **Run the server**

   ```bash
   python app.py
   ```

### Dashboard BGS rules and AI reports

The Flask process evaluates enabled dashboard BGS rules against settled snapshots every ten minutes and dispatches its Discord outbox every minute. Personal Discord webhooks require `VALK_WEBHOOK_ENCRYPTION_KEY`; use one long, deployment-specific random secret and keep it stable so existing values remain decryptable. Tenant-wide alerts reuse `discord_webhooks.bgs` from `tenant.json`.

Manual BGS risk and takeover reports use `OPENAI_API_KEY` and `OPENAI_MODEL`. System facilities, ownership metadata, faction counts and coordinates are read through the existing persistent Spansh cache; Inara is not used for this feature.

## Discord

Further informations you'll find on the VALK Discord Server https://discord.gg/JdRBJnNS

## Screenshots

[Dashboard](screenshots/Dashboard.MD)

## Credits

This project was developed by Cmdr JanJonTheo.

Docker Compose setup by daniele-liprandi

## Disclaimer

This project is not affiliated with or endorsed by Frontier Developments Inc., the creators of Elite Dangerous.

## Special Thanks

Special thanks to Aussi and Cmdr NavlGazr from BGS-Tally for their support and assistance.
