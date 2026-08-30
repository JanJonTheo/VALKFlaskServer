"""Persistent Spansh facility cache for dashboard system watchlists.

Spansh is used as the complete bootstrap/reconciliation source.  The cache is
global because systems and facilities are identical for every tenant; the
tenant-local watchlists remain in ``dashboard_view_preference``.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable
from urllib.parse import quote

import requests
from sqlalchemy.engine import make_url


BASE_DIR = Path(__file__).resolve().parent
SPANSH_API_BASE = os.getenv("SPANSH_API_BASE", "https://spansh.co.uk/api").rstrip("/")
SPANSH_CACHE_TTL_SECONDS = max(
    900, int(os.getenv("SPANSH_CACHE_TTL_SECONDS", str(6 * 60 * 60)))
)
SPANSH_REQUEST_TIMEOUT_SECONDS = max(
    3, int(os.getenv("SPANSH_REQUEST_TIMEOUT_SECONDS", "12"))
)
WATCHLIST_VIEW_KEY = "bgs-system-watchlist"

_refresh_lock = threading.RLock()


class SpanshFacilityError(RuntimeError):
    """Raised when no current or cached facility data can be returned."""


def _cache_path() -> Path:
    configured = os.getenv("SPANSH_CACHE_DB")
    path = Path(configured) if configured else BASE_DIR / "db" / "spansh_facilities.db"
    if not path.is_absolute():
        path = BASE_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_cache_path(), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS spansh_system_cache (
            system_key TEXT PRIMARY KEY,
            system_name TEXT NOT NULL,
            id64 TEXT NOT NULL,
            coord_x REAL,
            coord_y REAL,
            coord_z REAL,
            faction_count INTEGER NOT NULL DEFAULT 0,
            factions_json TEXT NOT NULL DEFAULT '[]',
            source_updated_at TEXT,
            fetched_at REAL NOT NULL,
            source_url TEXT NOT NULL,
            last_status TEXT NOT NULL DEFAULT 'ok',
            last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS spansh_facility (
            facility_id TEXT PRIMARY KEY,
            system_key TEXT NOT NULL,
            system_id64 TEXT NOT NULL,
            market_id TEXT,
            name TEXT NOT NULL,
            facility_type TEXT NOT NULL,
            is_settlement INTEGER NOT NULL DEFAULT 0,
            body TEXT,
            body_id64 TEXT,
            distance_to_arrival REAL,
            latitude REAL,
            longitude REAL,
            controlling_faction TEXT,
            allegiance TEXT,
            government TEXT,
            primary_economy TEXT,
            secondary_economy TEXT,
            services_json TEXT NOT NULL DEFAULT '[]',
            have_market INTEGER NOT NULL DEFAULT 0,
            have_shipyard INTEGER NOT NULL DEFAULT 0,
            have_outfitting INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            FOREIGN KEY(system_key) REFERENCES spansh_system_cache(system_key)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_spansh_facility_system
            ON spansh_facility(system_key);
        CREATE INDEX IF NOT EXISTS ix_spansh_facility_type
            ON spansh_facility(facility_type);
        CREATE INDEX IF NOT EXISTS ix_spansh_facility_market
            ON spansh_facility(market_id);
        """
    )
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(spansh_system_cache)")
    }
    cache_columns = {
        "coord_x": "REAL",
        "coord_y": "REAL",
        "coord_z": "REAL",
        "faction_count": "INTEGER NOT NULL DEFAULT 0",
        "factions_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, definition in cache_columns.items():
        if name not in existing_columns:
            connection.execute(
                f"ALTER TABLE spansh_system_cache ADD COLUMN {name} {definition}"
            )
    connection.commit()
    return connection


@contextmanager
def _connection():
    connection = _connect()
    try:
        yield connection
    finally:
        connection.close()


def _request_json(path: str) -> dict[str, Any]:
    response = requests.get(
        f"{SPANSH_API_BASE}/{path.lstrip('/')}",
        headers={"Accept": "application/json", "User-Agent": "VALKDashboardV2/0.1"},
        timeout=SPANSH_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise SpanshFacilityError("Spansh returned an invalid JSON response")
    return payload


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _resolve_id64(system_name: str, cached_id64: str | None = None) -> str:
    if cached_id64:
        return cached_id64
    payload = _request_json(f"search/systems?q={quote(system_name, safe='')}")
    results = payload.get("results")
    if not isinstance(results, list):
        results = []
    exact = next(
        (
            item
            for item in results
            if isinstance(item, dict)
            and _clean_text(item.get("name")).casefold() == system_name.casefold()
        ),
        None,
    )
    if not exact or not exact.get("id64"):
        raise SpanshFacilityError(f"Spansh could not resolve system '{system_name}'")
    return _clean_text(exact["id64"])


def _economies(station: dict[str, Any]) -> tuple[str, str]:
    primary = _clean_text(station.get("primaryEconomy"))
    secondary = _clean_text(station.get("secondaryEconomy"))
    values = station.get("economies")
    if isinstance(values, dict):
        ordered = [
            _clean_text(name)
            for name, _ in sorted(
                values.items(), key=lambda item: _number(item[1]) or 0, reverse=True
            )
            if _clean_text(name)
        ]
        if not primary and ordered:
            primary = ordered[0]
        if not secondary:
            secondary = next((name for name in ordered if name != primary), "")
    return primary, secondary


def _normalise_facility(
    station: dict[str, Any],
    system_key: str,
    system_id64: str,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    facility_type = _clean_text(station.get("type")) or "Unknown facility"
    services = [
        _clean_text(value)
        for value in station.get("services", [])
        if _clean_text(value)
    ] if isinstance(station.get("services"), list) else []
    primary_economy, secondary_economy = _economies(station)
    market_id = _clean_text(station.get("id") or station.get("marketId"))
    name = _clean_text(station.get("name")) or "Unnamed facility"
    body_name = _clean_text((body or {}).get("name"))
    body_id64 = _clean_text((body or {}).get("id64"))
    facility_id = market_id or f"{system_id64}:{body_id64}:{name.casefold()}"
    market = station.get("market")
    shipyard = station.get("shipyard")
    outfitting = station.get("outfitting")
    return {
        "facility_id": facility_id,
        "system_key": system_key,
        "system_id64": system_id64,
        "market_id": market_id,
        "name": name,
        "facility_type": facility_type,
        "is_settlement": int("settlement" in facility_type.casefold()),
        "body": body_name,
        "body_id64": body_id64,
        "distance_to_arrival": _number(station.get("distanceToArrival")),
        "latitude": _number(station.get("latitude")),
        "longitude": _number(station.get("longitude")),
        "controlling_faction": _clean_text(station.get("controllingFaction")),
        "allegiance": _clean_text(station.get("allegiance")),
        "government": _clean_text(station.get("government")),
        "primary_economy": primary_economy,
        "secondary_economy": secondary_economy,
        "services_json": json.dumps(services, separators=(",", ":")),
        "have_market": int(isinstance(market, dict) or "Market" in services),
        "have_shipyard": int(isinstance(shipyard, dict) or "Shipyard" in services),
        "have_outfitting": int(isinstance(outfitting, dict) or "Outfitting" in services),
        "updated_at": _clean_text(station.get("updateTime")),
    }


def _normalise_dump(system: dict[str, Any]) -> list[dict[str, Any]]:
    system_id64 = _clean_text(system.get("id64"))
    system_key = _clean_text(system.get("name")).casefold()
    facilities: list[dict[str, Any]] = []
    for station in system.get("stations", []) or []:
        if isinstance(station, dict):
            facilities.append(_normalise_facility(station, system_key, system_id64, None))
    for body in system.get("bodies", []) or []:
        if not isinstance(body, dict):
            continue
        for station in body.get("stations", []) or []:
            if isinstance(station, dict):
                facilities.append(_normalise_facility(station, system_key, system_id64, body))
    return facilities


def _normalise_factions(system: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for faction in system.get("factions", []) or []:
        if not isinstance(faction, dict):
            continue
        name = _clean_text(faction.get("name") or faction.get("Name"))
        if not name:
            continue
        result.append(
            {
                "name": name,
                "influence": _number(
                    faction.get("influence") or faction.get("Influence")
                ),
                "government": _clean_text(
                    faction.get("government") or faction.get("Government")
                ),
                "allegiance": _clean_text(
                    faction.get("allegiance") or faction.get("Allegiance")
                ),
                "state": _clean_text(faction.get("state") or faction.get("FactionState")),
            }
        )
    return result


def _coordinates(system: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    coordinates = system.get("coordinates")
    if not isinstance(coordinates, dict):
        coordinates = system.get("coords")
    if not isinstance(coordinates, dict):
        coordinates = {}
    return (
        _number(system.get("x") if system.get("x") is not None else coordinates.get("x")),
        _number(system.get("y") if system.get("y") is not None else coordinates.get("y")),
        _number(system.get("z") if system.get("z") is not None else coordinates.get("z")),
    )


def _cached_payload(connection: sqlite3.Connection, system_key: str) -> dict[str, Any] | None:
    system = connection.execute(
        "SELECT * FROM spansh_system_cache WHERE system_key = ?", (system_key,)
    ).fetchone()
    if not system:
        return None
    rows = connection.execute(
        "SELECT * FROM spansh_facility WHERE system_key = ? "
        "ORDER BY is_settlement, facility_type COLLATE NOCASE, name COLLATE NOCASE",
        (system_key,),
    ).fetchall()
    stations = []
    for row in rows:
        stations.append(
            {
                "id": row["facility_id"],
                "market_id": row["market_id"] or "",
                "name": row["name"],
                "type": row["facility_type"],
                "is_settlement": bool(row["is_settlement"]),
                "distance_to_arrival": row["distance_to_arrival"],
                "body": row["body"] or "",
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "controlling_faction": row["controlling_faction"] or "",
                "allegiance": row["allegiance"] or "",
                "government": row["government"] or "",
                "economy": row["primary_economy"] or "",
                "second_economy": row["secondary_economy"] or "",
                "services": json.loads(row["services_json"] or "[]"),
                "have_market": bool(row["have_market"]),
                "have_shipyard": bool(row["have_shipyard"]),
                "have_outfitting": bool(row["have_outfitting"]),
                "updated_at": row["updated_at"] or "",
            }
        )
    type_counts = Counter(item["type"] for item in stations)
    fetched_at = datetime.fromtimestamp(system["fetched_at"], timezone.utc).isoformat()
    factions = json.loads(system["factions_json"] or "[]")
    return {
        "system": system["system_name"],
        "system_id64": system["id64"],
        "source": "Spansh",
        "source_url": system["source_url"],
        "source_updated_at": system["source_updated_at"] or "",
        "cached_at": fetched_at,
        "coordinates": {
            "x": system["coord_x"],
            "y": system["coord_y"],
            "z": system["coord_z"],
        },
        "faction_count": int(system["faction_count"] or len(factions)),
        "factions": factions,
        "facility_type_counts": dict(sorted(type_counts.items())),
        "stations": stations,
    }


def _store_dump(
    connection: sqlite3.Connection,
    requested_name: str,
    system: dict[str, Any],
    facilities: list[dict[str, Any]],
) -> None:
    actual_name = _clean_text(system.get("name")) or requested_name
    system_key = actual_name.casefold()
    system_id64 = _clean_text(system.get("id64"))
    source_url = f"https://spansh.co.uk/system/{system_id64}"
    source_updated_at = _clean_text(system.get("date") or system.get("updateTime"))
    coord_x, coord_y, coord_z = _coordinates(system)
    factions = _normalise_factions(system)
    fetched_at = time.time()
    with connection:
        connection.execute(
            "INSERT INTO spansh_system_cache(system_key, system_name, id64, "
            "coord_x, coord_y, coord_z, faction_count, factions_json, "
            "source_updated_at, fetched_at, source_url, last_status, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', NULL) "
            "ON CONFLICT(system_key) DO UPDATE SET system_name=excluded.system_name, "
            "id64=excluded.id64, coord_x=excluded.coord_x, coord_y=excluded.coord_y, "
            "coord_z=excluded.coord_z, faction_count=excluded.faction_count, "
            "factions_json=excluded.factions_json, source_updated_at=excluded.source_updated_at, "
            "fetched_at=excluded.fetched_at, source_url=excluded.source_url, "
            "last_status='ok', last_error=NULL",
            (
                system_key,
                actual_name,
                system_id64,
                coord_x,
                coord_y,
                coord_z,
                len(factions),
                json.dumps(factions, separators=(",", ":"), ensure_ascii=False),
                source_updated_at,
                fetched_at,
                source_url,
            ),
        )
        connection.execute("DELETE FROM spansh_facility WHERE system_key = ?", (system_key,))
        connection.executemany(
            "INSERT INTO spansh_facility(facility_id, system_key, system_id64, "
            "market_id, name, facility_type, is_settlement, body, body_id64, "
            "distance_to_arrival, latitude, longitude, controlling_faction, "
            "allegiance, government, primary_economy, secondary_economy, "
            "services_json, have_market, have_shipyard, have_outfitting, updated_at) "
            "VALUES (:facility_id, :system_key, :system_id64, :market_id, :name, "
            ":facility_type, :is_settlement, :body, :body_id64, :distance_to_arrival, "
            ":latitude, :longitude, :controlling_faction, :allegiance, :government, "
            ":primary_economy, :secondary_economy, :services_json, :have_market, "
            ":have_shipyard, :have_outfitting, :updated_at)",
            facilities,
        )
        if requested_name.casefold() != system_key:
            connection.execute(
                "DELETE FROM spansh_system_cache WHERE system_key = ?",
                (requested_name.casefold(),),
            )


def get_system_facilities(system_name: str, force: bool = False) -> dict[str, Any]:
    """Return cached facilities and refresh from Spansh when the cache is stale."""

    requested_name = _clean_text(system_name)
    if len(requested_name) < 2 or len(requested_name) > 255:
        raise ValueError("A valid system name is required")
    requested_key = requested_name.casefold()

    with _connection() as connection:
        cached = _cached_payload(connection, requested_key)
        row = connection.execute(
            "SELECT id64, fetched_at FROM spansh_system_cache WHERE system_key = ?",
            (requested_key,),
        ).fetchone()
        if (
            not force
            and cached
            and row
            and time.time() - float(row["fetched_at"]) < SPANSH_CACHE_TTL_SECONDS
        ):
            return {**cached, "cache_status": "HIT", "stale": False}

    with _refresh_lock:
        with _connection() as connection:
            cached = _cached_payload(connection, requested_key)
            row = connection.execute(
                "SELECT id64, fetched_at FROM spansh_system_cache WHERE system_key = ?",
                (requested_key,),
            ).fetchone()
            if (
                not force
                and cached
                and row
                and time.time() - float(row["fetched_at"]) < SPANSH_CACHE_TTL_SECONDS
            ):
                return {**cached, "cache_status": "HIT", "stale": False}
            cached_id64 = _clean_text(row["id64"]) if row else ""

        try:
            system_id64 = _resolve_id64(requested_name, cached_id64)
            envelope = _request_json(f"dump/{quote(system_id64, safe='')}")
            system = envelope.get("system")
            if not isinstance(system, dict):
                raise SpanshFacilityError("Spansh returned no system dump")
            actual_name = _clean_text(system.get("name"))
            if actual_name.casefold() != requested_key:
                raise SpanshFacilityError(
                    f"Spansh returned '{actual_name}' for '{requested_name}'"
                )
            facilities = _normalise_dump(system)
            with _connection() as connection:
                _store_dump(connection, requested_name, system, facilities)
                payload = _cached_payload(connection, actual_name.casefold())
            if payload is None:
                raise SpanshFacilityError("The refreshed Spansh cache is unavailable")
            return {**payload, "cache_status": "MISS", "stale": False}
        except Exception as exc:
            with _connection() as connection:
                if row:
                    with connection:
                        connection.execute(
                            "UPDATE spansh_system_cache SET last_status='error', last_error=? "
                            "WHERE system_key=?",
                            (str(exc)[:1000], requested_key),
                        )
                stale = _cached_payload(connection, requested_key)
            if stale:
                return {
                    **stale,
                    "cache_status": "STALE",
                    "stale": True,
                    "warning": str(exc),
                }
            if isinstance(exc, SpanshFacilityError):
                raise
            raise SpanshFacilityError(f"Spansh facility lookup failed: {exc}") from exc


def get_facility_type_statistics(system_names: Iterable[str]) -> dict[str, Any]:
    """Aggregate selected starport families from the shared local cache only."""

    systems: dict[str, str] = {}
    for value in system_names:
        name = _clean_text(value)
        if name:
            systems.setdefault(name.casefold(), name)
    categories = {
        "dodec": 0,
        "orbis": 0,
        "ocellus": 0,
        "coriolis": 0,
    }
    if not systems:
        return {
            "types": categories,
            "cached_systems": 0,
            "requested_systems": 0,
        }

    keys = list(systems)
    placeholders = ", ".join("?" for _ in keys)
    with _connection() as connection:
        cached_systems = connection.execute(
            "SELECT COUNT(*) FROM spansh_system_cache "
            f"WHERE system_key IN ({placeholders})",
            keys,
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT facility_type, COUNT(*) AS facility_count "
            "FROM spansh_facility "
            f"WHERE system_key IN ({placeholders}) AND is_settlement = 0 "
            "GROUP BY facility_type",
            keys,
        ).fetchall()

    for row in rows:
        facility_type = _clean_text(row["facility_type"]).casefold()
        count = int(row["facility_count"] or 0)
        for category in categories:
            if category in facility_type:
                categories[category] += count
                break
    return {
        "types": categories,
        "cached_systems": int(cached_systems or 0),
        "requested_systems": len(keys),
    }


def _tenant_database_candidates(db_uri: str) -> list[Path]:
    url = make_url(db_uri)
    if not url.drivername.startswith("sqlite") or not url.database:
        return []
    configured_path = Path(url.database)
    if configured_path.is_absolute():
        return [configured_path]
    configured_base = os.getenv("TENANT_DATABASE_BASE_DIR")
    bases = [
        Path(configured_base) if configured_base else None,
        Path.cwd(),
        BASE_DIR,
        BASE_DIR.parent,
    ]
    candidates: list[Path] = []
    for base in bases:
        if base is None:
            continue
        candidate = (base / configured_path).resolve()
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def collect_tenant_watchlist_systems(tenants: Iterable[dict[str, Any]]) -> list[str]:
    """Collect the distinct systems stored in all tenant-local user watchlists."""

    systems: dict[str, str] = {}
    for tenant in tenants:
        db_uri = _clean_text(tenant.get("db_uri"))
        if not db_uri:
            continue
        try:
            candidates = _tenant_database_candidates(db_uri)

            rows = None
            for path in candidates:
                if not path.exists():
                    continue
                try:
                    uri = f"file:{path.as_posix()}?mode=ro"
                    with closing(
                        sqlite3.connect(uri, uri=True, timeout=5)
                    ) as connection:
                        rows = connection.execute(
                            "SELECT payload_json FROM dashboard_view_preference WHERE view_key = ?",
                            (WATCHLIST_VIEW_KEY,),
                        ).fetchall()
                    break
                except sqlite3.Error:
                    continue
            if rows is None:
                continue
            for (raw_payload,) in rows:
                payload = json.loads(raw_payload or "{}")
                for entry in payload.get("systems", []) if isinstance(payload, dict) else []:
                    name = _clean_text(entry.get("system")) if isinstance(entry, dict) else ""
                    if name:
                        systems.setdefault(name.casefold(), name)
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
            continue
    return sorted(systems.values(), key=str.casefold)


def refresh_tenant_watchlists(
    tenants: Iterable[dict[str, Any]], force: bool = True
) -> dict[str, Any]:
    """Refresh the shared cache for every distinct system watched by any user."""

    systems = collect_tenant_watchlist_systems(tenants)
    refreshed = 0
    stale = 0
    errors: list[dict[str, str]] = []
    for system in systems[:500]:
        try:
            payload = get_system_facilities(system, force=force)
            if payload.get("stale"):
                stale += 1
            else:
                refreshed += 1
        except Exception as exc:
            errors.append({"system": system, "error": str(exc)[:300]})
        time.sleep(0.1)
    return {
        "systems": len(systems),
        "refreshed": refreshed,
        "stale": stale,
        "errors": errors,
    }
