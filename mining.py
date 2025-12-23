import os
import requests
import logging
from math import sqrt
from flask import Blueprint, request, jsonify
from sqlalchemy import text, create_engine, event
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# Mining-DB
MINING_DB_URI = "sqlite:///db/bgs_data_eddn_mining.db"
mining_engine = create_engine(MINING_DB_URI)

def _sqlite_register_math_functions(dbapi_conn, conn_record):
    import math
    dbapi_conn.create_function("log", 1, math.log)
    dbapi_conn.create_function("exp", 1, math.exp)

if "sqlite" in str(mining_engine.url):
    event.listen(mining_engine, "connect", _sqlite_register_math_functions)

mining_bp = Blueprint("mining", __name__)

# -----------------------------------------------------------------------------
# Koordinaten-Cache & Provider
# -----------------------------------------------------------------------------

_COORDS_DDL = """
CREATE TABLE IF NOT EXISTS system_coords (
    system_name TEXT PRIMARY KEY,
    x REAL NOT NULL,
    y REAL NOT NULL,
    z REAL NOT NULL,
    updated_at TEXT NOT NULL
);
"""

def _ensure_coords_table(conn):
    conn.execute(text(_COORDS_DDL))

def _fetch_coords_ardent(system: str):
    try:
        url = f"https://api.ardent-insight.com/v2/system/name/{requests.utils.quote(system)}"
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        coords = data.get("coords") or {}
        x, y, z = coords.get("x"), coords.get("y"), coords.get("z")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and isinstance(z, (int, float)):
            return (float(x), float(y), float(z))
        return None
    except Exception:
        return None

def _fetch_coords_edsm(system: str):
    try:
        url = "https://www.edsm.net/api-v1/system"
        params = {"systemName": system, "showCoordinates": 1}
        headers = {"User-Agent": "VALK-Mining/1.0"}
        r = requests.get(url, params=params, headers=headers, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        coords = data.get("coords") or {}
        x, y, z = coords.get("x"), coords.get("y"), coords.get("z")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and isinstance(z, (int, float)):
            return (float(x), float(y), float(z))
        return None
    except Exception:
        return None

def _get_system_coords(conn, system: str, source: str = "auto"):
    """
    source: local | ardent | edsm | auto (Cache -> Ardent -> EDSM)
    """
    _ensure_coords_table(conn)

    def _cache_lookup():
        row = conn.execute(text(
            "SELECT x,y,z FROM system_coords WHERE system_name=:n"
        ), {"n": system}).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def _cache_store(coords):
        import sqlite3
        import time
        db_path = mining_engine.url.database
        max_attempts = 10
        for attempt in range(max_attempts):
            try:
                conn = sqlite3.connect(db_path, isolation_level=None)
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO system_coords (system_name,x,y,z,updated_at) VALUES (?,?,?,?,datetime('now'))",
                        (system, coords[0], coords[1], coords[2])
                    )
                    break
                finally:
                    conn.close()
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_attempts - 1:
                    time.sleep(1.0)
                    continue
                else:
                    raise

    source = (source or "auto").lower()
    if source == "local":
        return _cache_lookup()
    if source == "ardent":
        c = _fetch_coords_ardent(system)
        if c: _cache_store(c)
        return c
    if source == "edsm":
        c = _fetch_coords_edsm(system)
        if c: _cache_store(c)
        return c

    # auto
    c = _cache_lookup()
    if c: return c
    c = _fetch_coords_ardent(system)
    if c: _cache_store(c); return c
    c = _fetch_coords_edsm(system)
    if c: _cache_store(c); return c
    return None

def _distance(p, q):
    return sqrt((p[0]-q[0])**2 + (p[1]-q[1])**2 + (p[2]-q[2])**2)

# -----------------------------------------------------------------------------
# Gemeinsame Score-Parameter:
#   score = w_count * log(1+count) + w_sightings * log(1+sightings) + w_rec * exp(-age_days * ln(2)/half_life)
#   (Bei Hotspots wird 'count' als Hotspot-Überlappung interpretiert.)
# -----------------------------------------------------------------------------

# =============================================================================
# 1) Interessante Mining-Punkte (SAA -> group='Mining')
# =============================================================================

@mining_bp.route("/api/mining/points", methods=["GET"])
def api_mining_points():
    """
    Query:
      - type=Painite,Tritium,... (optional, CSV; match auf LOWER(signal_type))
      - min_count=0 (optional)
      - min_sightings=1 (optional)
      - max_age_days=365 (optional)
      - half_life_days=45 (optional)
      - w_count=1.0, w_sightings=1.0, w_recency=0.5 (optional)
      - limit=100
    """
    try:
        types = request.args.get("type")
        types = [t.strip() for t in types.split(",")] if types else None

        min_count = int(request.args.get("min_count", 0))
        min_sight = int(request.args.get("min_sightings", 1))
        max_age = int(request.args.get("max_age_days", 365))
        half_life = float(request.args.get("half_life_days", 45))
        w_count = float(request.args.get("w_count", 1.0))
        w_sig = float(request.args.get("w_sightings", 1.0))
        w_rec = float(request.args.get("w_recency", 0.5))
        limit = int(request.args.get("limit", 100))

        decay = 0.69314718056 / max(half_life, 1.0)

        with mining_engine.connect() as conn:
            filters = [
                "s.signal_group = 'Mining'",
                "s.last_seen_at >= datetime('now', :age)",
                "s.sightings >= :min_sight",
                "s.count >= :min_count"
            ]
            params = {
                "age": f"-{max_age} days",
                "min_sight": min_sight,
                "min_count": min_count,
                "limit": limit,
                "w_count": w_count,
                "w_sig": w_sig,
                "w_rec": w_rec,
                "decay": decay
            }

            if types:
                in_params = {f"t{i}": v for i, v in enumerate(types)}
                filters.append("LOWER(s.signal_type) IN (" + ", ".join([f"LOWER(:{k})" for k in in_params]) + ")")
                params.update(in_params)

            where_sql = " AND ".join(filters)
            sql = f"""
                SELECT
                    s.system_name, s.body_name, s.latitude, s.longitude,
                    s.signal_type AS type, s.count, s.sightings, s.first_seen_at, s.last_seen_at,
                    (:w_count*log(1+CAST(s.count AS REAL)))
                  + (:w_sig*log(1+CAST(s.sightings AS REAL)))
                  + (:w_rec*EXP(-(julianday('now') - julianday(s.last_seen_at)) * :decay)) AS score
                FROM eddn_saa_signal s
                WHERE {where_sql}
                ORDER BY score DESC, s.last_seen_at DESC
                LIMIT :limit
            """
            rows = conn.execute(text(sql), params).mappings().all()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@mining_bp.route("/api/mining/points/near", methods=["GET"])
def api_mining_points_near():
    """
    Wie /api/mining/points, plus:
      - ref_system=<Name> (required)
      - radius_ly=50
      - coords_source=auto|local|ardent|edsm
    """
    try:
        ref_system = request.args.get("ref_system")
        if not ref_system:
            return jsonify({"error": "ref_system is required"}), 400
        radius = float(request.args.get("radius_ly", 50))
        coords_source = (request.args.get("coords_source") or "auto").lower()

        types = request.args.get("type")
        types = [t.strip() for t in types.split(",")] if types else None

        min_count = int(request.args.get("min_count", 0))
        min_sight = int(request.args.get("min_sightings", 1))
        max_age = int(request.args.get("max_age_days", 365))
        half_life = float(request.args.get("half_life_days", 45))
        w_count = float(request.args.get("w_count", 1.0))
        w_sig = float(request.args.get("w_sightings", 1.0))
        w_rec = float(request.args.get("w_recency", 0.5))

        # Neu: Endlimit NACH Radius-Filter
        end_limit = request.args.get("limit")
        end_limit = int(end_limit) if end_limit is not None else None
        # Neu: Seed-Limit für SQL (Preselection)
        seed_limit = int(request.args.get("seed_limit", max((end_limit or 200) * 10, 1000)))

        decay = 0.69314718056 / max(half_life, 1.0)

        with mining_engine.connect() as conn:
            ref_coords = _get_system_coords(conn, ref_system, source=coords_source)
            if not ref_coords:
                return jsonify({"error": f"No coordinates found for ref_system '{ref_system}'"}), 404

            filters = [
                "s.signal_group = 'Mining'",
                "s.last_seen_at >= datetime('now', :age)",
                "s.sightings >= :min_sight",
                "s.count >= :min_count"
            ]
            params = {
                "age": f"-{max_age} days",
                "min_sight": min_sight,
                "min_count": min_count,
                "seed_limit": seed_limit,
                "w_count": w_count,
                "w_sig": w_sig,
                "w_rec": w_rec,
                "decay": decay
            }
            if types:
                in_params = {f"t{i}": v for i, v in enumerate(types)}
                filters.append("LOWER(s.signal_type) IN (" + ", ".join([f"LOWER(:{k})" for k in in_params]) + ")")
                params.update(in_params)

            where_sql = " AND ".join(filters)
            sql = f"""
                SELECT
                    s.system_name, s.body_name, s.latitude, s.longitude,
                    s.signal_type AS type, s.count, s.sightings, s.first_seen_at, s.last_seen_at,
                    (:w_count*log(1+CAST(s.count AS REAL)))
                  + (:w_sig*log(1+CAST(s.sightings AS REAL)))
                  + (:w_rec*EXP(-(julianday('now') - julianday(s.last_seen_at)) * :decay)) AS score
                FROM eddn_saa_signal s
                WHERE {where_sql}
                ORDER BY score DESC, s.last_seen_at DESC
                LIMIT :seed_limit
            """
            rows = conn.execute(text(sql), params).mappings().all()

            out, cache = [], {}
            for r in rows:
                sysname = r["system_name"]
                d = cache.get(sysname)
                if d is None:
                    coords = _get_system_coords(conn, sysname, source=coords_source)
                    if not coords:
                        continue
                    d = _distance(ref_coords, coords)
                    cache[sysname] = d
                if d <= radius:
                    item = dict(r)
                    item["distance_ly"] = round(d, 3)
                    out.append(item)

            out.sort(key=lambda x: (-float(x["score"]), float(x["distance_ly"])))
            if end_limit:
                out = out[:end_limit]
            return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================================================
# 2) Interessante Mining-Hotspots (aus eddn_mining_hotspot)
# =============================================================================

@mining_bp.route("/api/mining/hotspots", methods=["GET"])
def api_mining_hotspots():
    """
    Query:
      - type=Painite,Tritium,... (optional; CSV, match auf LOWER(hs.commodity))
      - min_count=0, min_sightings=1
      - max_age_days=365
      - half_life_days=45
      - w_count=1.0, w_sightings=1.0, w_recency=0.5
      - limit=100
    """
    try:
        types = request.args.get("type")
        types = [t.strip() for t in types.split(",")] if types else None

        min_count = int(request.args.get("min_count", 0))
        min_sight = int(request.args.get("min_sightings", 1))
        max_age = int(request.args.get("max_age_days", 365))
        half_life = float(request.args.get("half_life_days", 45))
        w_count = float(request.args.get("w_count", 1.0))
        w_sig = float(request.args.get("w_sightings", 1.0))
        w_rec = float(request.args.get("w_recency", 0.5))
        limit = int(request.args.get("limit", 100))
        decay = 0.69314718056 / max(half_life, 1.0)

        with mining_engine.connect() as conn:
            filters = [
                "hs.active = 1",
                "hs.last_seen_at >= datetime('now', :age)",
                "hs.sightings >= :min_sight",
                "hs.count >= :min_count"
            ]
            params = {
                "age": f"-{max_age} days",
                "min_sight": min_sight,
                "min_count": min_count,
                "limit": limit,
                "w_count": w_count,
                "w_sig": w_sig,
                "w_rec": w_rec,
                "decay": decay
            }
            if types:
                in_params = {f"t{i}": v for i, v in enumerate(types)}
                filters.append("LOWER(hs.commodity) IN (" + ", ".join([f"LOWER(:{k})" for k in in_params]) + ")")
                params.update(in_params)

            where_sql = " AND ".join(filters)
            sql = f"""
                SELECT
                    r.system_name, r.body_name, r.ring_name, r.ring_type, r.reserve_level,
                    hs.commodity AS type, hs.count, hs.sightings, hs.first_seen_at, hs.last_seen_at,
                    (:w_count*log(1+CAST(hs.count AS REAL)))
                  + (:w_sig*log(1+CAST(hs.sightings AS REAL)))
                  + (:w_rec*EXP(-(julianday('now') - julianday(hs.last_seen_at)) * :decay)) AS score
                FROM eddn_mining_hotspot hs
                JOIN eddn_mining_ring r ON r.id = hs.ring_id
                WHERE {where_sql}
                ORDER BY score DESC, hs.last_seen_at DESC
                LIMIT :limit
            """
            rows = conn.execute(text(sql), params).mappings().all()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@mining_bp.route("/api/mining/hotspots/near", methods=["GET"])
def api_mining_hotspots_near():
    """
    Wie /api/mining/hotspots, plus:
      - ref_system=<Name> (required)
      - radius_ly=50
      - coords_source=auto|local|ardent|edsm
    """
    try:
        ref_system = request.args.get("ref_system")
        if not ref_system:
            return jsonify({"error": "ref_system is required"}), 400
        radius = float(request.args.get("radius_ly", 50))
        coords_source = (request.args.get("coords_source") or "auto").lower()

        types = request.args.get("type")
        types = [t.strip() for t in types.split(",")] if types else None

        min_count = int(request.args.get("min_count", 0))
        min_sight = int(request.args.get("min_sightings", 1))
        max_age = int(request.args.get("max_age_days", 365))
        half_life = float(request.args.get("half_life_days", 45))
        w_count = float(request.args.get("w_count", 1.0))
        w_sig = float(request.args.get("w_sightings", 1.0))
        w_rec = float(request.args.get("w_recency", 0.5))

        # Neu: Endlimit NACH Radius-Filter
        end_limit = request.args.get("limit")
        end_limit = int(end_limit) if end_limit is not None else None
        # Neu: Seed-Limit für SQL
        seed_limit = int(request.args.get("seed_limit", max((end_limit or 200) * 10, 1000)))

        decay = 0.69314718056 / max(half_life, 1.0)

        with mining_engine.connect() as conn:
            ref_coords = _get_system_coords(conn, ref_system, source=coords_source)
            if not ref_coords:
                return jsonify({"error": f"No coordinates found for ref_system '{ref_system}'"}), 404

            filters = [
                "hs.active = 1",
                "hs.last_seen_at >= datetime('now', :age)",
                "hs.sightings >= :min_sight",
                "hs.count >= :min_count"
            ]
            params = {
                "age": f"-{max_age} days",
                "min_sight": min_sight,
                "min_count": min_count,
                "seed_limit": seed_limit,
                "w_count": w_count,
                "w_sig": w_sig,
                "w_rec": w_rec,
                "decay": decay
            }
            if types:
                in_params = {f"t{i}": v for i, v in enumerate(types)}
                filters.append("LOWER(hs.commodity) IN (" + ", ".join([f"LOWER(:{k})" for k in in_params]) + ")")
                params.update(in_params)

            where_sql = " AND ".join(filters)
            sql = f"""
                SELECT
                    r.system_name, r.body_name, r.ring_name, r.ring_type, r.reserve_level,
                    hs.commodity AS type, hs.count, hs.sightings, hs.first_seen_at, hs.last_seen_at,
                    (:w_count*log(1+CAST(hs.count AS REAL)))
                  + (:w_sig*log(1+CAST(hs.sightings AS REAL)))
                  + (:w_rec*EXP(-(julianday('now') - julianday(hs.last_seen_at)) * :decay)) AS score
                FROM eddn_mining_hotspot hs
                JOIN eddn_mining_ring r ON r.id = hs.ring_id
                WHERE {where_sql}
                ORDER BY score DESC, hs.last_seen_at DESC
                LIMIT :seed_limit
            """
            rows = conn.execute(text(sql), params).mappings().all()

            out, cache = [], {}
            for r in rows:
                sysname = r["system_name"]
                d = cache.get(sysname)
                if d is None:
                    coords = _get_system_coords(conn, sysname, source=coords_source)
                    if not coords:
                        continue
                    d = _distance(ref_coords, coords)
                    cache[sysname] = d
                if d <= radius:
                    item = dict(r)
                    item["distance_ly"] = round(d, 3)
                    out.append(item)

            out.sort(key=lambda x: (-float(x["score"]), float(x["distance_ly"])))
            if end_limit:
                out = out[:end_limit]
            return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================================================
# 3) Interessante Exobiology-Punkte (SAA -> group='Biological', optional genus)
# =============================================================================

@mining_bp.route("/api/mining/exo", methods=["GET"])
def api_exo_points():
    """
    Query:
      - genus=Tussocks,Fungoid,Shrubs (optional; match auf LOWER(b.genus))
      - type=... (optional, CSV; match auf LOWER(s.signal_type))
      - min_count=0, min_sightings=1
      - max_age_days=365
      - half_life_days=45
      - w_count=1.0, w_sightings=1.0, w_recency=0.5
      - limit=100
    """
    try:
        genus = request.args.get("genus")
        genuses = [g.strip() for g in genus.split(",")] if genus else None

        types = request.args.get("type")
        types = [t.strip() for t in types.split(",")] if types else None

        min_count = int(request.args.get("min_count", 0))
        min_sight = int(request.args.get("min_sightings", 1))
        max_age = int(request.args.get("max_age_days", 365))
        half_life = float(request.args.get("half_life_days", 45))
        w_count = float(request.args.get("w_count", 1.0))
        w_sig = float(request.args.get("w_sightings", 1.0))
        w_rec = float(request.args.get("w_recency", 0.5))
        limit = int(request.args.get("limit", 100))

        decay = 0.69314718056 / max(half_life, 1.0)

        with mining_engine.connect() as conn:
            filters = [
                "s.signal_group = 'Biological'",
                "s.last_seen_at >= datetime('now', :age)",
                "s.sightings >= :min_sight",
                "s.count >= :min_count"
            ]
            params = {
                "age": f"-{max_age} days",
                "min_sight": min_sight,
                "min_count": min_count,
                "limit": limit,
                "w_count": w_count,
                "w_sig": w_sig,
                "w_rec": w_rec,
                "decay": decay
            }

            join = "LEFT JOIN eddn_biological_saa_signal b ON b.saa_signal_id = s.id"

            if genuses:
                in_params = {f"g{i}": v for i, v in enumerate(genuses)}
                filters.append("LOWER(b.genus) IN (" + ", ".join([f"LOWER(:{k})" for k in in_params]) + ")")
                params.update(in_params)

            if types:
                in_params = {f"t{i}": v for i, v in enumerate(types)}
                filters.append("LOWER(s.signal_type) IN (" + ", ".join([f"LOWER(:{k})" for k in in_params]) + ")")
                params.update(in_params)

            where_sql = " AND ".join(filters)
            sql = f"""
                SELECT
                    s.system_name, s.body_name, s.latitude, s.longitude,
                    s.signal_type AS type, b.genus, b.species, b.variant,
                    s.count, s.sightings, s.first_seen_at, s.last_seen_at,
                    (:w_count*log(1+CAST(s.count AS REAL)))
                  + (:w_sig*log(1+CAST(s.sightings AS REAL)))
                  + (:w_rec*EXP(-(julianday('now') - julianday(s.last_seen_at)) * :decay)) AS score
                FROM eddn_saa_signal s
                {join}
                WHERE {where_sql}
                ORDER BY score DESC, s.last_seen_at DESC
                LIMIT :limit
            """
            rows = conn.execute(text(sql), params).mappings().all()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@mining_bp.route("/api/mining/exo/near", methods=["GET"])
def api_exo_points_near():
    """
    Wie /api/mining/exo, plus:
      - ref_system=<Name> (required)
      - radius_ly=50
      - coords_source=auto|local|ardent|edsm
    """
    try:
        ref_system = request.args.get("ref_system")
        if not ref_system:
            return jsonify({"error": "ref_system is required"}), 400
        radius = float(request.args.get("radius_ly", 50))
        coords_source = (request.args.get("coords_source") or "auto").lower()

        genus = request.args.get("genus")
        genuses = [g.strip() for g in genus.split(",")] if genus else None

        types = request.args.get("type")
        types = [t.strip() for t in types.split(",")] if types else None

        min_count = int(request.args.get("min_count", 0))
        min_sight = int(request.args.get("min_sightings", 1))
        max_age = int(request.args.get("max_age_days", 365))
        half_life = float(request.args.get("half_life_days", 45))
        w_count = float(request.args.get("w_count", 1.0))
        w_sig = float(request.args.get("w_sightings", 1.0))
        w_rec = float(request.args.get("w_recency", 0.5))

        # Neu: Endlimit NACH Radius-Filter
        end_limit = request.args.get("limit")
        end_limit = int(end_limit) if end_limit is not None else None
        # Neu: Seed-Limit für SQL
        seed_limit = int(request.args.get("seed_limit", max((end_limit or 200) * 10, 1000)))

        decay = 0.69314718056 / max(half_life, 1.0)

        with mining_engine.connect() as conn:
            ref_coords = _get_system_coords(conn, ref_system, source=coords_source)
            if not ref_coords:
                return jsonify({"error": f"No coordinates found for ref_system '{ref_system}'"}), 404

            filters = [
                "s.signal_group = 'Biological'",
                "s.last_seen_at >= datetime('now', :age)",
                "s.sightings >= :min_sight",
                "s.count >= :min_count"
            ]
            params = {
                "age": f"-{max_age} days",
                "min_sight": min_sight,
                "min_count": min_count,
                "seed_limit": seed_limit,
                "w_count": w_count,
                "w_sig": w_sig,
                "w_rec": w_rec,
                "decay": decay
            }

            join = "LEFT JOIN eddn_biological_saa_signal b ON b.saa_signal_id = s.id"

            if genuses:
                in_params = {f"g{i}": v for i, v in enumerate(genuses)}
                filters.append("LOWER(b.genus) IN (" + ", ".join([f"LOWER(:{k})" for k in in_params]) + ")")
                params.update(in_params)

            if types:
                in_params = {f"t{i}": v for i, v in enumerate(types)}
                filters.append("LOWER(s.signal_type) IN (" + ", ".join([f"LOWER(:{k})" for k in in_params]) + ")")
                params.update(in_params)

            where_sql = " AND ".join(filters)
            sql = f"""
                SELECT
                    s.system_name, s.body_name, s.latitude, s.longitude,
                    s.signal_type AS type, b.genus, b.species, b.variant,
                    s.count, s.sightings, s.first_seen_at, s.last_seen_at,
                    (:w_count*log(1+CAST(s.count AS REAL)))
                  + (:w_sig*log(1+CAST(s.sightings AS REAL)))
                  + (:w_rec*EXP(-(julianday('now') - julianday(s.last_seen_at)) * :decay)) AS score
                FROM eddn_saa_signal s
                {join}
                WHERE {where_sql}
                ORDER BY score DESC, s.last_seen_at DESC
                LIMIT :seed_limit
            """
            rows = conn.execute(text(sql), params).mappings().all()

            out, cache = [], {}
            for r in rows:
                sysname = r["system_name"]
                d = cache.get(sysname)
                if d is None:
                    coords = _get_system_coords(conn, sysname, source=coords_source)
                    if not coords:
                        continue
                    d = _distance(ref_coords, coords)
                    cache[sysname] = d
                if d <= radius:
                    item = dict(r)
                    item["distance_ly"] = round(d, 3)
                    out.append(item)

            out.sort(key=lambda x: (-float(x["score"]), float(x["distance_ly"])))
            if end_limit:
                out = out[:end_limit]
            return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================================================
# 4) Systemkoordinaten abrufen und speichern
# =============================================================================

# Logger für Systemkoordinaten (wie in eddn_client.py)
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

coords_logger = _make_logger("system_coords", "system_coords.log")

@mining_bp.route("/api/mining/fetch-system-coords", methods=["POST"])
def api_fetch_system_coords():
    """
    Fragt für alle Systeme ohne Koordinaten die Koordinaten ab und speichert sie.
    Loggt alle Aktionen in logs/system_coords.log.
    Optionaler Parameter: source=auto|ardent|edsm|local
    """
    source = (request.args.get("source") or "auto").lower()
    return _fetch_system_coords_worker(source, as_json=True)

def _fetch_system_coords_worker(source="auto", as_json=False):
    """
    Führt die eigentliche Koordinatenabfrage durch.
    Kann sowohl vom API-Endpoint als auch vom Scheduler aufgerufen werden.
    as_json: True → gibt jsonify()-Response zurück (nur im Request-Kontext)
             False → gibt dict zurück (für Scheduler)
    """
    added, failed = 0, 0
    try:
        with mining_engine.connect() as conn:
            # Hole alle Systeme aus eddn_saa_signal, die nicht im Cache sind
            systems = conn.execute(text("""
                SELECT DISTINCT system_name
                FROM eddn_saa_signal
                WHERE system_name NOT IN (SELECT system_name FROM system_coords)
            """)).fetchall()
            coords_logger.info(f"Starte Koordinatenabfrage für {len(systems)} Systeme (Quelle: {source})")
            for row in systems:
                sysname = row[0]
                try:
                    coords = _get_system_coords(conn, sysname, source=source)
                    if coords:
                        coords_logger.info(f"Koordinaten für '{sysname}' gefunden: {coords}")
                        added += 1
                    else:
                        coords_logger.warning(f"Koordinaten für '{sysname}' NICHT gefunden")
                        failed += 1
                except Exception as e:
                    coords_logger.error(f"Fehler bei '{sysname}': {e}")
                    failed += 1
            coords_logger.info(f"Fertig: {added} Systeme ergänzt, {failed} fehlgeschlagen")
        result = {
            "added": added,
            "failed": failed,
            "total": len(systems)
        }
        if as_json:
            return jsonify(result)
        else:
            return result
    except Exception as e:
        coords_logger.error(f"Abbruch: {e}")
        if as_json:
            return jsonify({"error": str(e)}), 500
        else:
            return {"error": str(e)}

def start_system_coords_scheduler(app=None):
    """
    Startet einen BackgroundScheduler, der stündlich die Systemkoordinaten aktualisiert.
    Die Startmeldung wird im app-Logger ausgegeben.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: _fetch_system_coords_worker("auto", as_json=False),
        trigger="cron",
        minute=0,
        id="system_coords_update_hourly",
        replace_existing=True
    )
    scheduler.start()
    logger.info("[SchedulerSystemCoords] System-Coords-Update scheduled hourly.")
