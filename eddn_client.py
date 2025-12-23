import os
import re
import zmq
import zlib
import json
import logging
import logging.handlers
import hashlib
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models_eddn import Base, EDDNMessage, Faction, Conflict, SystemInfo, Powerplay
from models_eddn_snapshot import (
    SystemTickSnapshot,
    init_snapshot_models,
    engine as snapshot_engine,
    SessionLocal as SnapshotSessionLocal,
)
from models_eddn_mining import (
    BaseMining, MiningRing, MiningHotspot,
    MiningSession, ProspectedAsteroid, MiningRefinedEvent, MaterialCollectedEvent, SAASignalFound,
    SAASignal, BiologicalSAASignal, BiologicalTaxonomyMap
)

# =============================================================================
# Pfade & Logging
# =============================================================================
BASE_DIR = os.path.dirname(__file__)
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _make_logger(name, filename):
    lg = logging.getLogger(name)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, filename),
        maxBytes=4 * 1024 * 1024,
        backupCount=10
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s")
    handler.setFormatter(fmt)
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg


logger_core = _make_logger("eddn_core", "eddn_client.log")
logger_bgs = _make_logger("eddn_bgs", "eddn_client_bgs.log")
logger_mining = _make_logger("eddn_mining", "eddn_client_mining.log")
logger_snapshot = _make_logger("eddn_snapshot", "eddn_client_snapshot.log")

# =============================================================================
# Konfiguration
# =============================================================================
EDDN_URL = "tcp://eddn.edcd.io:9500"

# Kern-/BGS-DB
DB_URI = "sqlite:///db/bgs_data_eddn.db"
# Mining-DB
MINING_DB_URI = "sqlite:///db/bgs_data_eddn_mining.db"
# Snapshot-DB (Engine kommt aus models_eddn_snapshot.py; Env nur zur Dokumentation)
SNAPSHOT_DB_URI = os.getenv("SNAPSHOT_DB_URL", "sqlite:///db/eddn_snapshots.db")

# Persisted tick state (written by app.py / fdev_tick_monitor.py)
TICK_STATE_PATH = os.path.join(os.path.dirname(__file__), "last_tick.json")

# Hotspot-Erkennung: extrahiert "Platinum" aus "Platinum Hotspot"
HOTSPOT_RX = re.compile(r"(?i)\b([A-Za-z ]+?)\s+Hotspot\b")

# Generische SAA-Typen, die NICHT in eddn_mining_saa_signal landen sollen
SKIP_SAA_GENERIC = {
    "$SAA_SignalType_Geological;",
    "$SAA_SignalType_Human;",
    "$SAA_SignalType_Biological;",
}

# =============================================================================
# Housekeeping
# =============================================================================
def cleanup_old_entries(session):
    """Löscht EDDNMessage-Einträge älter als 24h in der BGS-DB."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    deleted = session.query(EDDNMessage).filter(EDDNMessage.timestamp < cutoff).delete()
    if deleted:
        logger_bgs.info("[BGS] %s alte eddn_message-Einträge gelöscht (älter als 24h).", deleted)


def defragment_database(engine, label=""):
    """Defragmentiert die SQLite-Datenbank für optimale Performance."""
    try:
        if "sqlite" in str(engine.url):
            logger_core.info("[DB] SQLite VACUUM %s gestartet ...", label or "")
            with engine.connect() as conn:
                conn.exec_driver_sql("VACUUM")
            logger_core.info("[DB] SQLite VACUUM %s abgeschlossen.", label or "")
    except Exception as ex:
        logger_core.warning("[DB] VACUUM fehlgeschlagen (%s): %s", label, ex)


# =============================================================================
# Snapshots (Phase 1): ticktime-string from shared file (no HTTP, no scheduler)
# =============================================================================
_last_tick_missing_log_at = None
_last_tick_available_logged = False


def _read_ticktime_from_file():
    try:
        if not os.path.exists(TICK_STATE_PATH):
            return None
        with open(TICK_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        val = data.get("value")
        if isinstance(val, str) and val.strip():
            return val.strip()
    except Exception as e:
        logger_snapshot.warning("[SNAPSHOT] Failed to read tick state file '%s': %s", TICK_STATE_PATH, e)
    return None


def get_current_ticktime_string():
    """
    Read ticktime string from the shared tick state file written by fdev_tick_monitor.py.
    No HTTP fallback (per requirement). No local scheduler here.
    """
    global _last_tick_missing_log_at, _last_tick_available_logged

    ticktime_str = _read_ticktime_from_file()
    if ticktime_str:
        if not _last_tick_available_logged:
            logger_snapshot.info("[SNAPSHOT] Ticktime available: '%s' — snapshot writing enabled.", ticktime_str)
            _last_tick_available_logged = True
        return ticktime_str

    now = datetime.utcnow()
    if _last_tick_missing_log_at is None or (now - _last_tick_missing_log_at).total_seconds() >= 60:
        _last_tick_missing_log_at = now
        logger_snapshot.info(
            "[SNAPSHOT] Ticktime not available yet (file missing/empty): '%s' -> skip snapshot writes.",
            TICK_STATE_PATH
        )
    return None


def _snapshot_signature(payload: dict) -> str:
    """Stable hash for Influence/States comparison."""
    msg = (payload or {}).get("message", {}) or {}
    factions = msg.get("Factions", []) or []
    normalized = []
    for f in factions:
        if not isinstance(f, dict):
            continue
        normalized.append({
            "Name": f.get("Name"),
            "Influence": f.get("Influence"),
            "FactionState": f.get("FactionState"),
            "ActiveStates": f.get("ActiveStates"),
            "PendingStates": f.get("PendingStates"),
            "RecoveringStates": f.get("RecoveringStates"),
        })
    normalized.sort(key=lambda x: (x.get("Name") or ""))
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _build_snapshot_payload(data: dict) -> dict:
    """Minimal-but-sufficient snapshot payload."""
    msg = data.get("message", {}) or {}

    keep_factions = []
    for f in (msg.get("Factions", []) or []):
        if not isinstance(f, dict):
            continue
        keep_factions.append({
            "Name": f.get("Name"),
            "Influence": f.get("Influence"),
            "FactionState": f.get("FactionState"),
            "ActiveStates": f.get("ActiveStates"),
            "PendingStates": f.get("PendingStates"),
            "RecoveringStates": f.get("RecoveringStates"),
        })

    return {
        "StarSystem": msg.get("StarSystem"),
        "SystemAddress": msg.get("SystemAddress"),
        "SystemFaction": msg.get("SystemFaction"),
        "Factions": keep_factions,
        "Conflicts": msg.get("Conflicts"),
        "timestamp": msg.get("timestamp"),
        "schemaRef": data.get("$schemaRef"),
    }


def cleanup_old_snapshots(snapshot_session, days: int = 30):
    cutoff = datetime.utcnow() - timedelta(days=days)
    deleted = (snapshot_session.query(SystemTickSnapshot)
               .filter(SystemTickSnapshot.received_at < cutoff)
               .delete())
    if deleted:
        logger_snapshot.info("[SNAPSHOT] %s snapshot row(s) deleted (older than %sd).", deleted, days)


def save_system_tick_snapshot(snapshot_session, data: dict):
    """
    Snapshot settlement logic (Option 1) using *ticktime string only* (no tickid).

    Key idea:
      - EDDN updates can lag behind the real tick by hours.
      - We decide whether an incoming EDDN update belongs to the *previous* tick or the *current* tick
        by comparing Influence/States vs. the last snapshot of the previous tick.

    Storage:
      - One row per (system_name, ticktime_str)
      - UNIQUE key: (system_name, ticktime)
      - Fields:
          - ticktime: ticktime string (shared file)
          - system_name
          - system_address (optional)
          - payload_json (minimal)
          - received_at / updated_at
          - is_settled: indicates that we are confident the system has "arrived" in the new tick

    Settlement workflow:
      1) Ensure a placeholder row exists for (system_name, current_ticktime) with is_settled=False
      2) If the placeholder is already settled:
           -> always write updates into the current tick row
      3) Else (not settled yet):
           - find previous tick row (latest snapshot for system with ticktime != current_ticktime)
           - compare incoming signature vs previous signature
             a) unchanged -> update previous tick row (update still belongs to old tick)
             b) changed   -> write into current tick row and set is_settled=True
    """
    msg = data.get("message", {}) or {}
    system_name = msg.get("StarSystem")
    if not system_name:
        logger_snapshot.debug("[SNAPSHOT] Skip: no StarSystem in message.")
        return

    # Ticktime is shared cross-process via db/last_tick.json written by fdev_tick_monitor.py (app.py process).
    ticktime_str = get_current_ticktime_string()
    if not ticktime_str:
        # get_current_ticktime_string already logs rate-limited
        return

    now = datetime.utcnow()
    system_address = msg.get("SystemAddress")

    # Build normalized payload once (avoid recomputing multiple times)
    incoming_payload = _build_snapshot_payload(data)

    # Compute signature once
    incoming_sig = _snapshot_signature(data)

    logger_snapshot.debug(
        "[SNAPSHOT] Begin: system='%s' ticktime='%s' addr='%s' event='%s' schema='%s'",
        system_name,
        ticktime_str,
        system_address,
        msg.get("event"),
        data.get("$schemaRef"),
    )

    # Latest snapshot for this system (any ticktime)
    latest = (snapshot_session.query(SystemTickSnapshot)
              .filter(SystemTickSnapshot.system_name == system_name)
              .order_by(SystemTickSnapshot.ticktime.desc(),
                        SystemTickSnapshot.received_at.desc())
              .first())

    if latest:
        logger_snapshot.debug(
            "[SNAPSHOT] Latest row: system='%s' ticktime='%s' settled=%s received_at=%s updated_at=%s",
            system_name,
            latest.ticktime,
            bool(latest.is_settled),
            latest.received_at,
            latest.updated_at,
        )
    else:
        logger_snapshot.debug("[SNAPSHOT] Latest row: system='%s' none (DB empty for system).", system_name)

    # Ensure placeholder for current ticktime exists
    current_row = (snapshot_session.query(SystemTickSnapshot)
                   .filter(SystemTickSnapshot.system_name == system_name,
                           SystemTickSnapshot.ticktime == ticktime_str)
                   .one_or_none())

    if not current_row:
        current_row = SystemTickSnapshot(
            ticktime=ticktime_str,
            system_name=system_name,
            system_address=str(system_address) if system_address is not None else None,
            payload_json=incoming_payload,
            received_at=now,
            updated_at=now,
            is_settled=False,
        )
        snapshot_session.add(current_row)
        snapshot_session.flush()

        logger_snapshot.info(
            "[SNAPSHOT] INSERT current placeholder: system='%s' ticktime='%s' settled=%s",
            system_name, ticktime_str, bool(current_row.is_settled)
        )
    else:
        logger_snapshot.debug(
            "[SNAPSHOT] Current row exists: system='%s' ticktime='%s' settled=%s received_at=%s updated_at=%s",
            system_name,
            ticktime_str,
            bool(current_row.is_settled),
            current_row.received_at,
            current_row.updated_at,
        )

    # If current tick is already settled: always write to it.
    if current_row.is_settled:
        current_row.payload_json = incoming_payload
        current_row.updated_at = now
        snapshot_session.flush()

        logger_snapshot.info(
            "[SNAPSHOT] UPDATE current (already settled): system='%s' ticktime='%s'",
            system_name, ticktime_str
        )
        return

    # Find previous ticktime row (latest snapshot with ticktime != current ticktime)
    prev_row = None
    if latest and latest.ticktime != ticktime_str:
        prev_row = latest
    else:
        prev_row = (snapshot_session.query(SystemTickSnapshot)
                    .filter(SystemTickSnapshot.system_name == system_name,
                            SystemTickSnapshot.ticktime != ticktime_str)
                    .order_by(SystemTickSnapshot.ticktime.desc(),
                              SystemTickSnapshot.received_at.desc())
                    .first())

    if not prev_row:
        # First ever row for this system, or only current tick exists.
        # With no previous signature, we cannot classify as "unchanged", so we treat as "changed" and settle.
        current_row.payload_json = incoming_payload
        current_row.updated_at = now
        current_row.is_settled = True
        snapshot_session.flush()

        logger_snapshot.info(
            "[SNAPSHOT] SETTLE current (no previous row): system='%s' ticktime='%s' reason='no_prev_row'",
            system_name, ticktime_str
        )
        return

    # Compute previous signature from stored payload_json (which is stored in snapshot format)
    prev_sig = None
    try:
        prev_sig = _snapshot_signature({"message": prev_row.payload_json})
    except Exception as ex:
        logger_snapshot.warning(
            "[SNAPSHOT] Prev signature compute failed: system='%s' prev_ticktime='%s' err=%s",
            system_name, prev_row.ticktime, ex
        )
        prev_sig = None

    logger_snapshot.debug(
        "[SNAPSHOT] Compare: system='%s' prev_ticktime='%s' curr_ticktime='%s' prev_sig=%s incoming_sig=%s",
        system_name,
        prev_row.ticktime,
        ticktime_str,
        (prev_sig[:8] if prev_sig else None),
        (incoming_sig[:8] if incoming_sig else None),
    )

    # If unchanged: update previous row (still belongs to old tick)
    if prev_sig and incoming_sig == prev_sig:
        prev_row.payload_json = incoming_payload
        prev_row.updated_at = now
        snapshot_session.flush()

        logger_snapshot.info(
            "[SNAPSHOT] UPDATE prev (unchanged -> still old tick): system='%s' prev_ticktime='%s' curr_ticktime='%s'",
            system_name, prev_row.ticktime, ticktime_str
        )
        return

    # Change detected (or prev_sig missing): settle current tick and write there
    current_row.payload_json = incoming_payload
    current_row.updated_at = now
    current_row.is_settled = True
    snapshot_session.flush()

    reason = "sig_changed" if prev_sig else "prev_sig_missing"
    logger_snapshot.info(
        "[SNAPSHOT] SETTLE current (change detected): system='%s' prev_ticktime='%s' curr_ticktime='%s' reason='%s'",
        system_name, prev_row.ticktime, ticktime_str, reason
    )


# =============================================================================
# Mining: Upsert-Helfer
# =============================================================================
def _upsert_ring(mining_session,
                 system_name: str,
                 body_name: str,
                 ring_name: str,
                 ring_type: str = None,
                 inner_km: float = None,
                 outer_km: float = None,
                 reserve_level: str = None) -> MiningRing:
    if not system_name or not ring_name:
        logger_mining.debug("[MINING] Ring-Upsert übersprungen: system='%s', ring='%s'", system_name, ring_name)
        return None

    ring = (mining_session.query(MiningRing)
            .filter(MiningRing.system_name == system_name,
                    MiningRing.ring_name == ring_name)
            .one_or_none())

    if ring:
        changed = False
        if ring_type and ring.ring_type != ring_type:
            ring.ring_type = ring_type
            changed = True
        if inner_km is not None and ring.inner_radius_km != inner_km:
            ring.inner_radius_km = inner_km
            changed = True
        if outer_km is not None and ring.outer_radius_km != outer_km:
            ring.outer_radius_km = outer_km
            changed = True
        if reserve_level and ring.reserve_level != reserve_level:
            ring.reserve_level = reserve_level
            changed = True
        if changed:
            ring.updated_at = datetime.utcnow()
            logger_mining.info(
                "[MINING] Ring aktualisiert: system='%s' ring='%s' type='%s' inner_km=%s outer_km=%s reserve='%s'",
                system_name, ring_name, ring_type, inner_km, outer_km, reserve_level
            )
        return ring

    ring = MiningRing(
        system_name=system_name,
        body_name=body_name or "",
        ring_name=ring_name,
        ring_type=ring_type,
        inner_radius_km=inner_km,
        outer_radius_km=outer_km,
        reserve_level=reserve_level,
        updated_at=datetime.utcnow(),
    )
    mining_session.add(ring)
    mining_session.flush()
    logger_mining.info("[MINING] Neuer Ring gespeichert: system='%s' ring='%s' type='%s'",
                       system_name, ring_name, ring_type)
    return ring


def _upsert_hotspot(mining_session,
                    ring_id: str,
                    commodity: str,
                    count: int = None,
                    source: str = "eddn_journal") -> MiningHotspot:
    if not ring_id or not commodity:
        logger_mining.debug("[MINING] Hotspot-Upsert übersprungen: ring_id='%s' commodity='%s'", ring_id, commodity)
        return None

    hs = (mining_session.query(MiningHotspot)
          .filter(MiningHotspot.ring_id == ring_id,
                  MiningHotspot.commodity == commodity)
          .one_or_none())

    now = datetime.utcnow()
    if hs:
        hs.last_seen_at = now
        if hasattr(hs, "confidence") and (hs.confidence or 0) < 95:
            hs.confidence = min(100, (hs.confidence or 0) + 5)
        if isinstance(count, int):
            hs.count = (hs.count or 0) + max(count, 1)
        hs.active = 1
        hs.updated_at = now
        logger_mining.info("[MINING] Hotspot aktualisiert: ring_id='%s' commodity='%s' count=%s",
                           ring_id, commodity, hs.count)
        return hs

    hs = MiningHotspot(
        ring_id=ring_id,
        commodity=commodity,
        count=(count or 1),
        source=source if "source" in MiningHotspot.__table__.columns else None,
        first_seen_at=now,
        last_seen_at=now,
        confidence=60 if "confidence" in MiningHotspot.__table__.columns else None,
        active=1,
        updated_at=now,
    )
    mining_session.add(hs)
    logger_mining.info("[MINING] Neuer Hotspot gespeichert: ring_id='%s' commodity='%s' count=%s",
                       ring_id, commodity, count or 1)
    return hs

# =============================================================================
# Mining Sessions & Parser
# =============================================================================
def _guess_ring_name_from_body(body_name: str) -> str:
    return body_name or ""


def _ensure_mining_session(mining_session, cmdr: str, system_name: str, body_name: str = None,
                           ring_name: str = None, ring_type: str = None, reserve_level: str = None) -> MiningSession:
    now = datetime.utcnow()
    q = (mining_session.query(MiningSession)
         .filter(MiningSession.cmdr == (cmdr or "Unknown"),
                 MiningSession.system_name == (system_name or "Unknown"),
                 MiningSession.body_name == (body_name or ""),
                 MiningSession.ring_name == (ring_name or ""))
         .order_by(MiningSession.started_at.desc()))
    sess = q.first()

    if sess:
        changed = False
        if ring_type and not sess.ring_type and "ring_type" in MiningSession.__table__.columns:
            sess.ring_type = ring_type
            changed = True
        if reserve_level and not sess.reserve_level and "reserve_level" in MiningSession.__table__.columns:
            sess.reserve_level = reserve_level
            changed = True
        sess.ended_at = now
        sess.updated_at = now
        if changed:
            logger_mining.debug("[MINING] Session-Kontext aktualisiert: %s", sess.id)
        return sess

    ring_id = None
    if ring_name:
        ring = (mining_session.query(MiningRing)
                .filter(MiningRing.system_name == system_name,
                        MiningRing.ring_name == ring_name)
                .one_or_none())
        if ring:
            ring_id = ring.id

    sess = MiningSession(
        cmdr=cmdr or "Unknown",
        system_name=system_name or "Unknown",
        body_name=body_name or "",
        ring_name=ring_name or "",
        ring_id=ring_id,
        ring_type=ring_type if "ring_type" in MiningSession.__table__.columns else None,
        reserve_level=reserve_level if "reserve_level" in MiningSession.__table__.columns else None,
        started_at=now,
        ended_at=now,
        updated_at=now
    )
    mining_session.add(sess)
    mining_session.flush()
    logger_mining.info("[MINING] Session gestartet: cmdr='%s' system='%s' ring='%s'",
                       cmdr, system_name, ring_name or body_name)
    return sess


def _parse_scan_for_rings(msg: dict, mining_session):
    system_name = msg.get("StarSystem")
    if not system_name:
        logger_mining.debug("[MINING] Scan ohne StarSystem, ignoriert.")
        return
    body_name = msg.get("BodyName") or msg.get("Body") or ""
    rings = msg.get("Rings") or []
    reserve = msg.get("ReserveLevel")

    for r in rings:
        ring_name = r.get("Name") or ""
        if not ring_name:
            continue
        ring_type = r.get("RingClass") or r.get("Type")
        inner = r.get("InnerRad")
        outer = r.get("OuterRad")
        inner_km = (inner / 1000.0) if isinstance(inner, (int, float)) else None
        outer_km = (outer / 1000.0) if isinstance(outer, (int, float)) else None
        _upsert_ring(mining_session, system_name, body_name, ring_name, ring_type, inner_km, outer_km, reserve)


def _sig_fingerprint(system_addr, body_id, ring_name, s_type, s_loc, s_count):
    base = f"{system_addr}|{body_id}|{ring_name or ''}|{s_type or ''}|{s_loc or ''}|{int(s_count or 1)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def _parse_saa_signals_for_hotspots(msg: dict, mining_session):
    system_name = msg.get("StarSystem")
    if not system_name:
        logger_mining.debug("[MINING] SAASignalsFound ohne StarSystem, ignoriert.")
        return

    body_name = msg.get("BodyName") or msg.get("Body") or ""
    body_id = msg.get("BodyID")
    sys_addr = msg.get("SystemAddress")
    ring_name_guess = _guess_ring_name_from_body(body_name)

    ring = _upsert_ring(mining_session, system_name, body_name, ring_name_guess, ring_type=None)

    try:
        ts = datetime.fromisoformat(msg.get("timestamp").replace("Z", "+00:00")) if msg.get("timestamp") else datetime.utcnow()
    except Exception:
        ts = datetime.utcnow()

    uploader_id = None

    signals = msg.get("Signals") or []
    if not isinstance(signals, list):
        logger_mining.debug("[MINING] SAASignalsFound: 'Signals' hat kein Listenformat, übersprungen.")
        return

    new_cnt = 0
    exist_cnt = 0

    for sig in signals:
        s_type = sig.get("Type") or ""
        s_loc = sig.get("Type_Localised") or ""
        s_count = int(sig.get("Count") or 1)

        if s_type in SKIP_SAA_GENERIC:
            logger_mining.debug("[MINING] SAASignalsFound SKIPPED generic: type='%s' loc='%s'", s_type, s_loc)
            continue

        fp = _sig_fingerprint(sys_addr, body_id, ring_name_guess, s_type, s_loc, s_count)

        existing = (mining_session.query(SAASignalFound)
                    .filter(SAASignalFound.system_name == system_name,
                            SAASignalFound.body_id == body_id,
                            SAASignalFound.ring_name == (ring_name_guess or None),
                            SAASignalFound.signal_type == s_type,
                            SAASignalFound.signal_type_localised == s_loc,
                            SAASignalFound.count == s_count)
                    .one_or_none())

        if existing:
            prev_sightings = existing.sightings or 1
            existing.last_seen_at = datetime.utcnow()
            existing.sightings = prev_sightings + 1
            existing.timestamp = ts
            existing.raw_json = {"message": msg, "signal": sig}
            existing.fingerprint = fp
            if uploader_id and "uploader_id" in SAASignalFound.__table__.columns:
                existing.uploader_id = uploader_id

            exist_cnt += 1
            logger_mining.info(
                "[MINING] UPDATE: system='%s' body='%s' ring='%s' type='%s' localised='%s' count=%d sightings=%d",
                system_name, body_name, ring_name_guess or "", s_type, s_loc or "", s_count, existing.sightings
            )
        else:
            saa = SAASignalFound(
                system_name=system_name,
                body_name=body_name or None,
                body_id=body_id,
                ring_name=ring_name_guess or None,
                signal_type=s_type,
                signal_type_localised=s_loc,
                count=s_count,
                timestamp=ts,
                raw_json={"message": msg, "signal": sig},
                uploader_id=uploader_id if "uploader_id" in SAASignalFound.__table__.columns else None,
                fingerprint=fp,
                first_seen_at=datetime.utcnow(),
                last_seen_at=datetime.utcnow(),
                sightings=1
            )
            mining_session.add(saa)
            new_cnt += 1
            logger_mining.info(
                "[MINING] INSERT: system='%s' body='%s' ring='%s' type='%s' localised='%s' count=%d",
                system_name, body_name, ring_name_guess or "", s_type, s_loc or "", s_count
            )

        m = HOTSPOT_RX.search(s_type or "")
        if m and ring:
            commodity = m.group(1).strip().title()
            _upsert_hotspot(mining_session, ring.id, commodity, count=s_count, source="eddn_journal")

    logger_mining.info(
        "[MINING] SAASignalsFound processed: system='%s' body='%s' total=%d new=%d existing=%d",
        system_name, body_name, len(signals), new_cnt, exist_cnt
    )


def _parse_prospected_asteroid(msg: dict, mining_session):
    system_name = msg.get("StarSystem")
    if not system_name:
        logger_mining.debug("[MINING] ProspectedAsteroid ohne StarSystem, ignoriert.")
        return

    body_name = msg.get("BodyName") or msg.get("Body") or ""
    ring_name = _guess_ring_name_from_body(body_name)
    cmdr = msg.get("Commander") or msg.get("cmdr") or "Unknown"

    try:
        ts = datetime.fromisoformat(msg.get("timestamp").replace("Z", "+00:00")) if msg.get("timestamp") else datetime.utcnow()
    except Exception:
        ts = datetime.utcnow()

    sess = _ensure_mining_session(mining_session, cmdr, system_name, body_name, ring_name)

    ev = ProspectedAsteroid(
        session_id=sess.id if sess else None,
        system_name=system_name,
        body_name=body_name,
        ring_name=ring_name,
        content_raw=msg,
        timestamp=ts,
    )
    mining_session.add(ev)
    logger_mining.info("[MINING] ProspectedAsteroid gespeichert: system='%s' body='%s' ring='%s'",
                       system_name, body_name, ring_name)


def _parse_mining_refined(msg: dict, mining_session):
    system_name = msg.get("StarSystem")
    if not system_name:
        logger_mining.debug("[MINING] MiningRefined ohne StarSystem, ignoriert.")
        return

    body_name = msg.get("BodyName") or msg.get("Body") or ""
    ring_name = _guess_ring_name_from_body(body_name)
    cmdr = msg.get("Commander") or msg.get("cmdr") or "Unknown"

    try:
        ts = datetime.fromisoformat(msg.get("timestamp").replace("Z", "+00:00")) if msg.get("timestamp") else datetime.utcnow()
    except Exception:
        ts = datetime.utcnow()

    sess = _ensure_mining_session(mining_session, cmdr, system_name, body_name, ring_name)

    commodity = msg.get("Type_Localised") or msg.get("Type") or "Unknown"
    amount = None
    for k in ("Count", "Quantity", "Amount"):
        if isinstance(msg.get(k), (int, float)):
            amount = float(msg.get(k))
            break
    if amount is None:
        amount = 1.0

    ev = MiningRefinedEvent(
        session_id=sess.id if sess else None,
        system_name=system_name,
        body_name=body_name,
        ring_name=ring_name,
        commodity=commodity,
        amount=amount,
        timestamp=ts,
        raw_json=msg
    )
    mining_session.add(ev)
    logger_mining.info("[MINING] MiningRefined gespeichert: system='%s' body='%s' ring='%s' commodity='%s' amount=%s",
                       system_name, body_name, ring_name, commodity, amount)


def _parse_material_collected(msg: dict, mining_session):
    system_name = msg.get("StarSystem")
    if not system_name:
        logger_mining.debug("[MINING] MaterialCollected ohne StarSystem, ignoriert.")
        return

    body_name = msg.get("BodyName") or msg.get("Body") or ""
    ring_name = _guess_ring_name_from_body(body_name)
    cmdr = msg.get("Commander") or msg.get("cmdr") or "Unknown"

    try:
        ts = datetime.fromisoformat(msg.get("timestamp").replace("Z", "+00:00")) if msg.get("timestamp") else datetime.utcnow()
    except Exception:
        ts = datetime.utcnow()

    sess = _ensure_mining_session(mining_session, cmdr, system_name, body_name, ring_name)

    category = msg.get("Category") or ""
    name = msg.get("Name_Localised") or msg.get("Name") or "Unknown"
    try:
        count = int(msg.get("Count") or 1)
    except Exception:
        count = 1

    ev = MaterialCollectedEvent(
        session_id=sess.id if sess else None,
        system_name=system_name,
        body_name=body_name,
        ring_name=ring_name,
        category=category,
        name=name,
        count=count,
        timestamp=ts,
        raw_json=msg
    )
    mining_session.add(ev)
    logger_mining.info("[MINING] MaterialCollected gespeichert: system='%s' body='%s' ring='%s' category='%s' name='%s' count=%d",
                       system_name, body_name, ring_name, category, name, count)

# =============================================================================
# Biological: Upsert-Helfer (strukturierte SAA-Signale)
# =============================================================================
def _pretty_from_map(session, raw_key: str, kind: str) -> str:
    if not raw_key:
        return None
    m = session.query(BiologicalTaxonomyMap).filter_by(key_raw=raw_key, kind=kind).first()
    return m.pretty if m else None


def _normalize_codex_label(raw_key: str) -> str:
    if not raw_key:
        return None
    s = raw_key.strip()
    if s.startswith("$"):
        s = s[1:]
    if s.endswith(";"):
        s = s[:-1]
    s = s.replace("_Name", "")
    return s.replace("_", " ").strip() or raw_key


def _classify_signal_group(obj: dict) -> tuple[str, str, str]:
    raw_code = obj.get("Type") or obj.get("SignalName") or obj.get("USSType") or ""
    type_local = obj.get("Type_Localised") or obj.get("SignalName_Localised") or ""

    import re as _re

    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    t_norm = _norm(raw_code)
    l_norm = _norm(type_local)

    group = "Human"
    if any(k in t_norm or k in l_norm for k in ("biological", "saasignaltypebiological")):
        group = "Biological"
    elif any(k in t_norm or k in l_norm for k in ("geological", "saasignaltypegeological", "fumarole", "geyser")):
        group = "Geological"
    elif any(k in t_norm or k in l_norm for k in ("human", "saasignaltypehuman")):
        group = "Human"

    mining_keys = (
        "tritium", "painite", "monazite", "alexandrite", "lowtemperaturediamond",
        "ltd", "benitoite", "musgravite", "rhodplumsite", "serendibite", "opal", "platinum"
    )
    if any(k in t_norm or k in l_norm for k in mining_keys):
        group = "Mining"

    pretty = type_local or _normalize_codex_label(raw_code) or "Unknown"
    return group, pretty, raw_code


def _expand_bio_signals_from_genuses(msg: dict) -> list[dict]:
    signals = msg.get("Signals") or []
    if not signals or not isinstance(signals, list):
        return []

    genuses = msg.get("Genuses") or []
    if not genuses:
        return []

    bio_sig = None
    for s in signals:
        t = s.get("Type") or ""
        tl = (s.get("Type_Localised") or "").strip().lower()
        if t == "$SAA_SignalType_Biological;" or "saa signaltype biological" in tl:
            bio_sig = s
            break
    if not bio_sig:
        return []

    total = int(bio_sig.get("Count") or len(genuses) or 1)
    per = max(1, total // max(1, len(genuses)))

    out = []
    for g in genuses:
        graw = g.get("Genus")
        if not graw:
            continue
        sg = dict(bio_sig)
        sg["Genus"] = graw
        sg["Count"] = per
        out.append(sg)
    return out


def _upsert_saa_signal(session, eddn_message_id, base_fields: dict, signal_obj: dict, seen_at: datetime):
    group, pretty, raw_code = _classify_signal_group(signal_obj)

    key = dict(
        system_name=base_fields["system_name"],
        body_name=base_fields["body_name"],
        latitude=base_fields.get("latitude"),
        longitude=base_fields.get("longitude"),
        signal_group=group,
        signal_type=raw_code
    )

    count = int(signal_obj.get("Count") or 0)

    row = session.query(SAASignal).filter_by(**key).first()
    if row:
        row.sightings = (row.sightings or 0) + 1
        if count:
            row.count = count
        row.last_seen_at = seen_at
        row.eddn_message_id = eddn_message_id
        row.raw_json = base_fields.get("raw_json")
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.flush()
    else:
        row = SAASignal(
            **key,
            count=count,
            sightings=1,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            eddn_message_id=eddn_message_id,
            raw_json=base_fields.get("raw_json"),
            updated_at=datetime.utcnow()
        )
        session.add(row)
        session.flush()

    if group == "Biological":
        genus_raw = signal_obj.get("Genus") or signal_obj.get("genus")
        species_raw = signal_obj.get("Species") or signal_obj.get("species")
        variant_raw = signal_obj.get("Variant") or signal_obj.get("variant")

        genus_local = signal_obj.get("Genus_Localised")
        species_local = signal_obj.get("Species_Localised")
        variant_local = signal_obj.get("Variant_Localised")

        genus = _pretty_from_map(session, genus_raw, "genus") or genus_local or _normalize_codex_label(genus_raw)
        species = _pretty_from_map(session, species_raw, "species") or species_local or _normalize_codex_label(species_raw)
        variant = _pretty_from_map(session, variant_raw, "variant") or variant_local or _normalize_codex_label(variant_raw)

        bio = session.query(BiologicalSAASignal).filter_by(saa_signal_id=row.id).first()
        if bio:
            bio.genus_raw, bio.species_raw, bio.variant_raw = genus_raw, species_raw, variant_raw
            bio.genus, bio.species, bio.variant = genus, species, variant
            bio.updated_at = datetime.utcnow()
            session.add(bio)
            logger_mining.info(
                "[EXOBIO] UPDATE: system='%s' body='%s' raw_type='%s' genus_raw='%s' species_raw='%s' variant_raw='%s'",
                base_fields.get("system_name"), base_fields.get("body_name"), raw_code,
                genus_raw, species_raw, variant_raw
            )
        else:
            bio = BiologicalSAASignal(
                saa_signal_id=row.id,
                genus_raw=genus_raw, species_raw=species_raw, variant_raw=variant_raw,
                genus=genus, species=species, variant=variant,
                updated_at=datetime.utcnow()
            )
            session.add(bio)
            logger_mining.info(
                "[EXOBIO] INSERT: system='%s' body='%s' raw_type='%s' genus_raw='%s' species_raw='%s' variant_raw='%s'",
                base_fields.get("system_name"), base_fields.get("body_name"), raw_code,
                genus_raw, species_raw, variant_raw
            )
    return row


def _handle_saa_signals_found(session, msg: dict, eddn_message_id: str):
    sysname = msg.get("SystemName") or msg.get("StarSystem") or ""
    body = msg.get("BodyName") or ""
    lat = msg.get("Latitude")
    lon = msg.get("Longitude")
    seen_at = datetime.utcnow()

    base_fields = {
        "system_name": sysname,
        "body_name": body,
        "latitude": lat, "longitude": lon,
        "raw_json": msg
    }

    signals = msg.get("Signals") or []
    if not signals:
        s = msg.get("Signal")
        if isinstance(s, dict):
            signals = [s]

    expanded = _expand_bio_signals_from_genuses(msg)
    if expanded:
        signals = [s for s in signals if (s.get("Type") != "$SAA_SignalType_Biological;" and
                                          (s.get("Type_Localised") or "").strip().lower() != "saa signaltype biological")]
        signals.extend(expanded)

    types_seen = []
    for s in signals:
        try:
            grp, typ_pretty, raw_code = _classify_signal_group(s)
        except Exception:
            grp, typ_pretty, raw_code = "Unknown", (s.get("Type_Localised") or s.get("Type") or "Unknown"), (s.get("Type") or "")

        types_seen.append(raw_code or typ_pretty)

        try:
            _upsert_saa_signal(session, eddn_message_id, base_fields, s, seen_at)
            logger_mining.debug(
                "[SIGNAL] Upsert OK: system='%s' body='%s' group='%s' raw_type='%s' pretty='%s' count=%s genus=%s",
                sysname, body, grp, raw_code, typ_pretty, s.get("Count") or 0, s.get("Genus")
            )
        except Exception as ex:
            logger_mining.exception(
                "[SIGNAL] Upsert FAILED: system='%s' body='%s' group='%s' raw_type='%s' err=%s",
                sysname, body, grp, raw_code, ex
            )

    unique_types = ", ".join(sorted(set(t for t in types_seen if t)))
    logger_mining.info(
        "[SIGNAL] Parsed SAASignalsFound (structured): %s | %s (%.5f, %.5f) | %d signals | raw_types=[%s]",
        sysname, body, (lat or 0.0), (lon or 0.0), len(signals), unique_types or "n/a"
    )

# =============================================================================
# BGS: bestehende System-bezogene Speicherung (mit Logging)
# =============================================================================
def save_system_related_data(session, data, eddn_message_id):
    msg = data.get("message", {})
    system_name = msg.get("StarSystem")
    now = datetime.utcnow()

    if not system_name:
        logger_bgs.debug("[BGS] System-bezogene Speicherung übersprungen (kein StarSystem).")
        return

    session.query(SystemInfo).filter_by(system_name=system_name).delete()
    sysinfo = SystemInfo(
        eddn_message_id=eddn_message_id,
        system_name=system_name,
        controlling_faction=msg.get("SystemFaction", {}).get("Name"),
        controlling_power=msg.get("ControllingPower"),
        population=msg.get("Population"),
        security=msg.get("SystemSecurity"),
        government=msg.get("SystemGovernment"),
        allegiance=msg.get("SystemAllegiance"),
        updated_at=now
    )
    session.add(sysinfo)
    logger_bgs.info("[BGS] SystemInfo gespeichert: system='%s' faction='%s' power='%s' pop=%s",
                    system_name, sysinfo.controlling_faction, sysinfo.controlling_power, sysinfo.population)

    session.query(Faction).filter_by(system_name=system_name).delete()
    factions = msg.get("Factions", [])
    if factions:
        for faction in factions:
            f = Faction(
                eddn_message_id=eddn_message_id,
                system_name=system_name,
                name=faction.get("Name"),
                influence=faction.get("Influence"),
                state=faction.get("FactionState"),
                recovering_states=faction.get("RecoveringStates"),
                active_states=faction.get("ActiveStates"),
                pending_states=faction.get("PendingStates"),
                updated_at=now
            )
            session.add(f)
        logger_bgs.info("[BGS] %d Faction(s) gespeichert für system='%s'.", len(factions), system_name)

    session.query(Conflict).filter_by(system_name=system_name).delete()
    conflicts = msg.get("Conflicts", [])
    if conflicts:
        for conflict in conflicts:
            c = Conflict(
                eddn_message_id=eddn_message_id,
                system_name=system_name,
                faction1=conflict.get("Faction1", {}).get("Name"),
                faction2=conflict.get("Faction2", {}).get("Name"),
                stake1=conflict.get("Faction1", {}).get("Stake"),
                stake2=conflict.get("Faction2", {}).get("Stake"),
                won_days1=conflict.get("Faction1", {}).get("WonDays"),
                won_days2=conflict.get("Faction2", {}).get("WonDays"),
                status=conflict.get("Status"),
                war_type=conflict.get("WarType"),
                updated_at=now
            )
            session.add(c)
        logger_bgs.info("[BGS] %d Conflict(s) gespeichert für system='%s'.", len(conflicts), system_name)

    session.query(Powerplay).filter_by(system_name=system_name).delete()
    has_powerplay = "Powers" in msg or "PowerplayState" in msg
    if has_powerplay and (msg.get("Powers") or msg.get("PowerplayState")):
        p = Powerplay(
            eddn_message_id=eddn_message_id,
            system_name=system_name,
            power=msg.get("Powers") if isinstance(msg.get("Powers"), list)
            else [msg.get("Powers")] if msg.get("Powers") else [],
            powerplay_state=msg.get("PowerplayState"),
            control_progress=msg.get("PowerplayStateControlProgress"),
            reinforcement=msg.get("PowerplayStateReinforcement"),
            undermining=msg.get("PowerplayStateUndermining"),
            updated_at=now
        )
        session.add(p)
        logger_bgs.info("[BGS] Powerplay gespeichert: system='%s' state='%s' powers=%s",
                        system_name, p.powerplay_state, p.power)

# =============================================================================
# Main-Loop
# =============================================================================
def main():
    engine = create_engine(DB_URI, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    init_snapshot_models()
    snapshot_session = SnapshotSessionLocal()

    mining_engine = create_engine(MINING_DB_URI, connect_args={"check_same_thread": False})
    BaseMining.metadata.create_all(mining_engine)
    MiningSessionMaker = sessionmaker(bind=mining_engine)
    mining_session = MiningSessionMaker()

    defragment_database(engine, "BGS")
    defragment_database(mining_engine, "MINING")
    defragment_database(snapshot_engine, "SNAPSHOT")

    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(EDDN_URL)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")

    logger_core.info("[CORE] EDDN Client gestartet, wartet auf Nachrichten...]")

    last_cleanup = datetime.utcnow()
    last_vacuum_bgs = datetime.utcnow()
    last_vacuum_mining = datetime.utcnow()
    last_vacuum_snapshot = datetime.utcnow()

    try:
        while True:
            raw = socket.recv()
            try:
                data = json.loads(zlib.decompress(raw).decode("utf-8"))
                msg = data.get("message", {}) or {}
                event = msg.get("event")
                schema_ref = data.get("$schemaRef", "")
                header_uploader = (data.get("header") or {}).get("uploaderID")
                logger_core.info("[CORE] EDDN Nachricht empfangen: event='%s' schema='%s'", event, schema_ref)

                # --- MINING: alle relevanten Events zuerst verarbeiten ---
                if event == "Scan":
                    _parse_scan_for_rings(msg, mining_session)

                if event == "SAASignalsFound":
                    _parse_saa_signals_for_hotspots(msg, mining_session)
                    _handle_saa_signals_found(mining_session, msg, header_uploader)
                    mining_session.commit()
                    logger_core.info("[CORE] SAASignalsFound verarbeitet (mining-db committed).")
                    continue

                if event == "ProspectedAsteroid":
                    _parse_prospected_asteroid(msg, mining_session)
                    mining_session.commit()
                    continue

                if event == "MiningRefined":
                    _parse_mining_refined(msg, mining_session)
                    mining_session.commit()
                    continue

                if event == "MaterialCollected":
                    _parse_material_collected(msg, mining_session)
                    mining_session.commit()
                    continue

                # --- BGS: nur Location/FSDJump persistieren ---
                if event not in ("Location", "FSDJump"):
                    logger_core.info("[CORE] Event (non-BGS persist): '%s' | System='%s' | Cmdr='%s'",
                                     event, msg.get("StarSystem"), msg.get("Commander") or msg.get("cmdr"))
                    continue

                eddn_msg = EDDNMessage.from_eddn(data)
                session.add(eddn_msg)
                session.flush()

                save_system_related_data(session, data, eddn_msg.id)

                # Snapshot
                try:
                    save_system_tick_snapshot(snapshot_session, data)
                    snapshot_session.commit()
                except Exception as sx:
                    logger_snapshot.exception("[SNAPSHOT] Failed to save snapshot: %s", sx)
                    try:
                        snapshot_session.rollback()
                    except Exception:
                        pass

                session.commit()

                if (datetime.utcnow() - last_cleanup).total_seconds() > 600 \
                        or session.query(EDDNMessage).count() % 100 == 0:
                    cleanup_old_entries(session)
                    last_cleanup = datetime.utcnow()

                if (datetime.utcnow() - last_vacuum_bgs).total_seconds() > 43200:
                    defragment_database(engine, "BGS")
                    last_vacuum_bgs = datetime.utcnow()

                if (datetime.utcnow() - last_vacuum_mining).total_seconds() > 43200:
                    defragment_database(mining_engine, "MINING")
                    last_vacuum_mining = datetime.utcnow()

                if (datetime.utcnow() - last_vacuum_snapshot).total_seconds() > 43200:
                    try:
                        cleanup_old_snapshots(snapshot_session, days=30)
                        snapshot_session.commit()
                    except Exception:
                        try:
                            snapshot_session.rollback()
                        except Exception:
                            pass
                    defragment_database(snapshot_engine, "SNAPSHOT")
                    last_vacuum_snapshot = datetime.utcnow()

            except Exception as ex:
                logger_core.exception("[CORE] Fehler beim Verarbeiten/Speichern einer Nachricht: %s", ex)
                try:
                    session.rollback()
                except Exception:
                    pass
                try:
                    mining_session.rollback()
                except Exception:
                    pass
                try:
                    snapshot_session.rollback()
                except Exception:
                    pass

    except KeyboardInterrupt:
        logger_core.info("[CORE] EDDN Client beendet (KeyboardInterrupt).")
    finally:
        for obj in (session, mining_session, snapshot_session):
            try:
                obj.close()
            except Exception:
                pass
        try:
            socket.close()
            context.term()
        except Exception:
            pass


if __name__ == "__main__":
    main()
