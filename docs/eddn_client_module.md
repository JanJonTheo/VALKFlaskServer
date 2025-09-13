# Documentation: eddn_client.py

## Overview

`eddn_client.py` is a standalone Python module that acts as a client for the Elite Dangerous Data Network (EDDN). It listens for real-time game event messages (such as system jumps and locations) via ZeroMQ, processes and decompresses them, and stores relevant data in a local SQLite database. The module is designed for robust, continuous operation and includes logging, data cleanup, and error handling.

---

## Main Components

### 1. Logging Setup
- Ensures a `logs/` directory exists.
- Configures a rotating file logger (`eddn_client.log`) with a maximum size of 128 MB and up to 10 backup files.
- Logs all important actions and errors for later review.

### 2. EDDN Connection
- Connects to the EDDN relay at `tcp://eddn.edcd.io:9500` using ZeroMQ (ZMQ) as a subscriber.
- Subscribes to all incoming messages (no topic filter).

### 3. Database Setup
- Uses SQLAlchemy ORM with a SQLite database (`db/bgs_data_eddn.db`).
- Imports models from `models_eddn.py` (including `EDDNMessage`, `Faction`, `Conflict`, `SystemInfo`, `Powerplay`).
- Ensures all tables are created at startup.

### 4. Message Processing Loop
- Continuously receives and decompresses EDDN messages (zlib-compressed JSON).
- Filters for relevant event types: only `Location` and `FSDJump` events are processed and stored.
- For each message:
  - Creates and stores an `EDDNMessage` record.
  - Extracts and stores system-related data (system info, factions, conflicts, powerplay) using helper functions.
  - Commits all changes to the database.
- Periodically cleans up old messages (older than 24 hours) to keep the database size manageable.

### 5. Data Extraction and Storage
- **save_system_related_data**: Handles extraction and storage of system, faction, conflict, and powerplay data for each message. Old records for the same system are deleted before new ones are inserted.
- **cleanup_old_entries**: Deletes all `EDDNMessage` records older than 24 hours and logs the number of deleted entries.

### 6. Error Handling
- All exceptions during message processing are caught and logged. The session is rolled back on error to prevent partial/inconsistent data.
- Graceful shutdown on `KeyboardInterrupt` (Ctrl+C), with proper cleanup of resources.

---

## Usage
- Run as a standalone process: `python eddn_client.py`
- Designed to be started as a background process or service alongside the main Flask app.
- The database can be queried by other modules (e.g., for system summaries or faction data).

---

## Extensibility
- Additional EDDN event types can be supported by extending the event filter and data extraction logic.
- The database schema can be expanded by modifying `models_eddn.py`.
- Logging and error handling can be further customized as needed.

---

## See Also
- `models_eddn.py`: SQLAlchemy models for EDDN data
- `app.py`: Main Flask application (queries the EDDN database)
- `fac_shoutout_scheduler.py`, `fdev_tick_monitor.py`: Use EDDN data for reporting and automation

---

*Last update: 2025-09-13*

**Author: CMDR JanJonTheo**

