# Documentation: models_eddn.py

## Overview

`models_eddn.py` defines the SQLAlchemy ORM models for storing and managing data received from the Elite Dangerous Data Network (EDDN). These models are used by the EDDN client and related modules to persist system, faction, conflict, and powerplay data extracted from real-time EDDN messages. The schema is optimized for efficient querying and supports regular updates as new messages arrive.

---

## Main Components

### 1. SQLAlchemy Base
- `Base = declarative_base()`: Sets up the declarative base for all ORM models in this module.

### 2. EDDNMessage
- **Purpose**: Stores the raw EDDN message, schema reference, event type, and timestamps.
- **Fields**:
  - `id`: UUID primary key.
  - `schema_ref`: Reference to the EDDN schema used.
  - `header_gateway_timestamp`: Timestamp from the EDDN message header.
  - `message_type`: The event type (e.g., `FSDJump`, `Location`).
  - `message_json`: The full message as a string.
  - `timestamp`: Time the message was received and stored.
- **Methods**:
  - `from_eddn(data)`: Static method to create an EDDNMessage from a raw EDDN message dict.

### 3. SystemInfo
- **Purpose**: Stores information about a star system as reported by EDDN.
- **Fields**:
  - `id`: UUID primary key.
  - `eddn_message_id`: Reference to the EDDN message this record was extracted from.
  - `system_name`: Name of the star system.
  - `controlling_faction`: Name of the controlling faction.
  - `controlling_power`: Name of the controlling power (if any).
  - `population`: System population.
  - `security`, `government`, `allegiance`: System attributes.
  - `updated_at`: Timestamp of last update.

### 4. Faction
- **Purpose**: Stores data about a faction present in a system.
- **Fields**:
  - `id`: UUID primary key.
  - `eddn_message_id`: Reference to the EDDN message this record was extracted from.
  - `system_name`: Name of the star system.
  - `name`: Faction name.
  - `influence`: Faction influence in the system.
  - `state`: Current state of the faction.
  - `recovering_states`, `active_states`, `pending_states`: JSON fields for BGS states.
  - `updated_at`: Timestamp of last update.

### 5. Conflict
- **Purpose**: Stores information about ongoing conflicts in a system.
- **Fields**:
  - `id`: UUID primary key.
  - `eddn_message_id`: Reference to the EDDN message this record was extracted from.
  - `system_name`: Name of the star system.
  - `faction1`, `faction2`: Names of the factions in conflict.
  - `stake1`, `stake2`: Stakes for each faction.
  - `won_days1`, `won_days2`: Number of days each faction has won.
  - `status`: Status of the conflict (e.g., active, pending).
  - `war_type`: Type of war (e.g., civil, election).
  - `updated_at`: Timestamp of last update.

### 6. Powerplay
- **Purpose**: Stores powerplay data for a system.
- **Fields**:
  - `id`: UUID primary key.
  - `eddn_message_id`: Reference to the EDDN message this record was extracted from.
  - `system_name`: Name of the star system.
  - `power`: JSON field with powerplay powers present in the system.
  - `powerplay_state`: State of the powerplay (e.g., control, turmoil).
  - `control_progress`, `reinforcement`, `undermining`: Numeric fields for powerplay progress.
  - `updated_at`: Timestamp of last update.

---

## Relationships
- All models are independent and linked only by the `eddn_message_id` field, which allows for efficient deletion and update of all data related to a specific EDDN message.
- No explicit SQLAlchemy relationships are defined, as the data is typically queried by system or message.

---

## Usage
- These models are used by the EDDN client to store and update data as new messages are received.
- The database can be queried by other modules (e.g., for system summaries, faction status, or powerplay analysis).
- The schema is designed for regular cleanup and replacement of old data to keep the database current and performant.

---

## Extensibility
- New fields or models can be added as EDDN evolves or as new data requirements emerge.
- The use of JSON fields allows for flexible storage of complex or nested data structures.

---

## See Also
- `eddn_client.py`: Main EDDN client that uses these models
- `app.py`: Flask app that queries the EDDN database
- `models.py`: Main BGS data models for tenant-specific data

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

