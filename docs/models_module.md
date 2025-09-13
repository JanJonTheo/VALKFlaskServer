# Documentation: models.py

## Overview

`models.py` defines the SQLAlchemy ORM models for the BGS Data backend. These models represent the core data structures for events, activities, factions, commanders, objectives, and more. The models are used by Flask and SQLAlchemy to interact with the underlying database(s) in a multi-tenant environment.

---

## Main Components

### 1. SQLAlchemy Initialization
- `db = SQLAlchemy()`: Initializes the SQLAlchemy extension for Flask.

### 2. Event and Related Models
- **Event**: Central event log for all incoming game events. Stores event type, timestamp, tick information, commander, system, and raw JSON data.
- **MarketBuyEvent / MarketSellEvent**: Details for market buy/sell transactions, linked to an Event.
- **MissionCompletedEvent / MissionCompletedInfluence**: Mission completion and influence changes, linked to an Event and MissionCompletedEvent.
- **FactionKillBondEvent**: Bounty bond events for faction kills.
- **MissionFailedEvent**: Records failed missions.
- **MultiSellExplorationDataEvent / SellExplorationDataEvent**: Exploration data sales, both single and multi-sell.
- **RedeemVoucherEvent**: Voucher redemptions (bounty, combat bond, etc.).
- **CommitCrimeEvent**: Crime events, including type, faction, victim, and bounty.

### 3. Activity and System Models
- **Activity**: Represents a tick-based activity, with a list of systems and associated factions.
- **System**: A star system, with a list of factions present.
- **Faction**: Faction state and statistics within a system (BGS, missions, trade, etc.).

### 4. Commander Model
- **Cmdr**: Represents a player commander, including ranks, credits, assets, squadron, and Inara URL.

### 5. Objective Models
- **Objective**: BGS or player objectives, with title, priority, type, system, faction, description, and date range.
- **ObjectiveTarget**: Targets for an objective, such as stations, systems, or factions, with progress tracking.
- **ObjectiveTargetSettlement**: Settlements associated with an objective target, with individual and overall progress.

### 6. Conflict Zone Models
- **SyntheticCZ / SyntheticGroundCZ**: Synthetic (simulated) conflict zone events, for both space and ground, including type, faction, commander, and station/settlement info.

### 7. Protected Faction Model
- **ProtectedFaction**: Factions that are protected from certain operations, with webhook URL, description, and protection flag.

---

## Relationships
- Many models use `db.ForeignKey` and `db.relationship` to establish parent-child relationships (e.g., Activity → System → Faction, Objective → ObjectiveTarget → ObjectiveTargetSettlement).
- Cascade rules ensure that deleting a parent will also delete all associated children (e.g., deleting an Activity deletes all its Systems and Factions).

---

## Usage
- These models are used throughout the Flask app for CRUD operations, queries, and reporting.
- The `from_dict` classmethod in `Event` allows easy creation from incoming JSON data.
- All models inherit from `db.Model` and are compatible with Flask-Migrate and Alembic for migrations.

---

## Extensibility
- New event types or data structures can be added by defining new models or extending existing ones.
- Relationships can be expanded to support new gameplay or reporting features.

---

## See Also
- `app.py`: Main Flask application and API endpoints
- `tenant.json`: Tenant configuration for multi-tenant support
- `fac_shoutout_scheduler.py`, `fdev_tick_monitor.py`, `fac_conflict_scheduler.py`: Schedulers and background jobs

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

