from datetime import datetime, timedelta
from functools import wraps
import ast
import json
import logging
import re
from uuid import uuid4

from flask import Blueprint, Response, g, jsonify, request
from sqlalchemy import desc, func, or_

from models import (
    ColonisationAssistStatus,
    ColonisationDelivery,
    Event,
    db,
)

colonisation_bp = Blueprint("colonisation", __name__)

logger = logging.getLogger(__name__)

COLONISATION_EVENTS = {
    "ColonisationSystemClaim",
    "ColonisationBeaconDeployed",
    "ColonisationConstructionDepot",
    "ColonisationContribution",
}
CONSTRUCTION_EVENT = "ColonisationConstructionDepot"
CONTRIBUTION_EVENT = "ColonisationContribution"
LOCATION_EVENTS = {"Docked", "Location"}

PERIOD_LABELS = {
    "ct": "Current Tick",
    "lt": "Last Tick",
    "cd": "Current Day",
    "ld": "Last Day",
    "cw": "Current Week",
    "lw": "Last Week",
    "cm": "Current Month",
    "lm": "Last Month",
    "2m": "Last 2 Months",
    "y": "Current Year",
    "all": "All Time",
    "custom": "Custom Range",
}

COMMODITY_NAMES = {
    "agriculturalmedicines": "Agri-Medicines",
    "aluminium": "Aluminium",
    "basicmedicines": "Basic Medicines",
    "ceramiccomposites": "Ceramic Composites",
    "computercomponents": "Computer Components",
    "copper": "Copper",
    "cropharvesters": "Crop Harvesters",
    "foodcartridges": "Food Cartridges",
    "fruitandvegetables": "Fruit and Vegetables",
    "insulatingmembrane": "Insulating Membrane",
    "liquidoxygen": "Liquid oxygen",
    "medicaldiagnosticequipment": "Medical Diagnostic Equipment",
    "nonlethalweapons": "Non-Lethal Weapons",
    "polymers": "Polymers",
    "powergenerators": "Power Generators",
    "semiconductors": "Semiconductors",
    "steel": "Steel",
    "superconductors": "Superconductors",
    "titanium": "Titanium",
    "water": "Water",
    "waterpurifiers": "Water Purifiers",
}


class ColonisationApiError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_response_error(message, status_code=400):
    return jsonify({"error": message}), status_code


def _auth_and_set_tenant():
    from app import API_VERSION, TENANTS, get_tenant_by_apikey, set_tenant_db_config

    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        from dashboard_auth import DashboardTokenError, validate_dashboard_token
        from dashboard_users import validate_dashboard_identity

        try:
            tenant, claims = validate_dashboard_token(
                authorization.split(" ", 1)[1], TENANTS
            )
        except DashboardTokenError as exc:
            logger.warning("Rejected dashboard bearer token for colonisation: %s", exc)
            return jsonify({
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": str(exc),
                    "correlation_id": request.headers.get("x-correlation-id"),
                }
            }), 401

        g.tenant = tenant
        g.dashboard_identity = claims
        set_tenant_db_config(tenant)
        if hasattr(g, "tenant_db_error"):
            logger.error("Tenant database error for colonisation endpoint")
            return jsonify({
                "error": {
                    "code": "TENANT_DATABASE_ERROR",
                    "message": g.tenant_db_error,
                    "correlation_id": request.headers.get("x-correlation-id"),
                }
            }), 500
        try:
            validate_dashboard_identity(db.session, claims, require_session=False)
        except PermissionError as exc:
            logger.warning("Rejected dashboard identity for colonisation: %s", exc)
            return jsonify({
                "error": {
                    "code": "SESSION_REVOKED",
                    "message": str(exc),
                    "correlation_id": request.headers.get("x-correlation-id"),
                }
            }), 401

        required_capability = (
            "dashboard:read" if request.method in {"GET", "HEAD"}
            else "colonisation:write"
        )
        if required_capability not in claims.get("capabilities", []):
            return jsonify({
                "error": {
                    "code": "FORBIDDEN",
                    "message": "Missing dashboard capability",
                    "correlation_id": request.headers.get("x-correlation-id"),
                }
            }), 403
        return None

    apikey = request.headers.get("apikey")
    tenant = get_tenant_by_apikey(apikey)
    if not tenant:
        logger.warning("Invalid API-Key received for colonisation endpoint")
        return _json_response_error("Unauthorized: Invalid API key", 401)

    g.tenant = tenant
    set_tenant_db_config(tenant)
    if hasattr(g, "tenant_db_error"):
        logger.error("Tenant database error for colonisation endpoint")
        return _json_response_error("Tenant database not found or not reachable", 500)

    api_version = request.headers.get("apiversion")
    if not api_version:
        return _json_response_error("Missing required header: apiversion", 400)
    if not re.match(r"^\d+\.\d+\.\d+$", api_version):
        return _json_response_error("Invalid apiversion format. Expected x.y.z notation", 400)
    if api_version != tenant.get("api_version", API_VERSION):
        logger.warning(
            "Client using different API version for colonisation endpoint: %s (tenant expected: %s)",
            api_version,
            tenant.get("api_version", API_VERSION),
        )
    return None


def require_colonisation_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_error = _auth_and_set_tenant()
        if auth_error:
            return auth_error
        return f(*args, **kwargs)

    return decorated


def _int_value(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", ""}:
            return False
    return bool(value)


def _normalize_commodity_key(name: str) -> str:
    normalized = str(name or "").strip().lower()
    if normalized.startswith("$"):
        normalized = normalized[1:]
    if normalized.endswith("_name;"):
        normalized = normalized[:-6]
    return "".join(ch for ch in normalized if ch.isalnum())


def _display_commodity(raw_name: str, localized: str = "") -> str:
    localized = str(localized or "").strip()
    if localized and not localized.startswith("$"):
        return localized
    key = _normalize_commodity_key(raw_name or localized)
    if key in COMMODITY_NAMES:
        return COMMODITY_NAMES[key]
    if not key:
        return str(raw_name or localized or "").strip()
    words = re.findall(r"[a-z]+|[0-9]+", key)
    return " ".join(word.capitalize() for word in words) if words else key


def _raw_event_payload(event: Event) -> dict:
    raw = event.raw_json or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            logger.debug("Unable to parse raw_json for event id %s", getattr(event, "id", None))
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_string(*names, max_len=256):
    for name in names:
        value = request.args.get(name)
        if value is not None:
            value = value.strip()
            return value[:max_len] if value else ""
    return ""


def _request_int_arg(*names, default=None):
    for name in names:
        value = request.args.get(name)
        if value not in (None, ""):
            return _int_value(value, default)
    return default


def _split_csv_arg(*names, max_items=25, max_len=64):
    values = []
    seen = set()
    for name in names:
        raw_values = request.args.getlist(name) or [request.args.get(name)]
        for raw_value in raw_values:
            if raw_value in (None, ""):
                continue
            for item in str(raw_value).split(","):
                value = item.strip()
                key = value.lower()
                if not value or key in seen:
                    continue
                seen.add(key)
                values.append(value[:max_len])
                if len(values) >= max_items:
                    return values
    return values


def _split_int_arg(*names, max_items=50):
    values = []
    seen = set()
    for name in names:
        raw_values = request.args.getlist(name) or [request.args.get(name)]
        for raw_value in raw_values:
            if raw_value in (None, ""):
                continue
            for item in str(raw_value).split(","):
                item = item.strip()
                if not item:
                    continue
                value = _int_value(item, None)
                if value is None:
                    raise ColonisationApiError(f"{name} must contain integer values")
                if value in seen:
                    continue
                seen.add(value)
                values.append(value)
                if len(values) >= max_items:
                    return values
    return values


def _month_start(day):
    return day.replace(day=1)


def _add_months(day, months):
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return day.replace(year=year, month=month, day=1)


def _date_arg(*names):
    for name in names:
        value = request.args.get(name)
        if value in (None, ""):
            continue
        text_value = str(value).strip()
        try:
            return datetime.strptime(text_value[:10], "%Y-%m-%d").date()
        except ValueError as exc:
            raise ColonisationApiError(f"{name} must use YYYY-MM-DD") from exc
    return None


def _day_start_iso(day):
    return datetime.combine(day, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest_tick_rows(limit=2):
    latest_timestamp = func.max(Event.timestamp)
    latest_ticktime = func.max(Event.ticktime)
    rows = (
        db.session.query(
            Event.tickid.label("tickid"),
            latest_ticktime.label("ticktime"),
            latest_timestamp.label("latest_timestamp"),
        )
        .filter(Event.tickid.isnot(None), Event.tickid != "")
        .group_by(Event.tickid)
        .order_by(desc(latest_timestamp))
        .limit(limit)
        .all()
    )
    return [
        {
            "tickid": row.tickid,
            "ticktime": row.ticktime or "",
            "latest_timestamp": row.latest_timestamp or "",
        }
        for row in rows
    ]


def _contribution_timeframe_from_request():
    period = (_optional_string("period", max_len=32) or "all").lower()
    from_date = _date_arg("from", "from_date", "start", "start_date")
    to_date = _date_arg("to", "to_date", "end", "end_date")
    if from_date or to_date:
        period = "custom"
    if period not in PERIOD_LABELS:
        raise ColonisationApiError(
            "period must be one of " + ", ".join(sorted(PERIOD_LABELS.keys()))
        )

    timeframe = {
        "period": period,
        "label": PERIOD_LABELS[period],
        "start": None,
        "end": None,
        "tickid": None,
        "ticktime": None,
        "latest_tick_timestamp": None,
    }

    if period in {"ct", "lt"}:
        rows = _latest_tick_rows(2)
        if rows:
            selected = rows[1] if period == "lt" and len(rows) > 1 else rows[0]
            timeframe["tickid"] = selected["tickid"]
            timeframe["ticktime"] = selected.get("ticktime") or ""
            timeframe["latest_tick_timestamp"] = selected.get("latest_timestamp") or ""
            timeframe["label"] = f"{PERIOD_LABELS[period]} ({selected['tickid']})"
        else:
            timeframe["label"] = f"{PERIOD_LABELS[period]} (no tick)"
        return timeframe

    today = datetime.utcnow().date()
    start = None
    end = None
    if period == "cd":
        start = today
        end = today + timedelta(days=1)
    elif period == "ld":
        start = today - timedelta(days=1)
        end = today
    elif period == "cw":
        start = today - timedelta(days=today.weekday())
        end = today + timedelta(days=1)
    elif period == "lw":
        this_week = today - timedelta(days=today.weekday())
        start = this_week - timedelta(days=7)
        end = this_week
    elif period == "cm":
        start = _month_start(today)
        end = today + timedelta(days=1)
    elif period == "lm":
        this_month = _month_start(today)
        start = _add_months(this_month, -1)
        end = this_month
    elif period == "2m":
        this_month = _month_start(today)
        start = _add_months(this_month, -2)
        end = this_month
    elif period == "y":
        start = today.replace(month=1, day=1)
        end = today + timedelta(days=1)
    elif period == "custom":
        start = from_date
        end = to_date + timedelta(days=1) if to_date else None

    if start:
        timeframe["start"] = _day_start_iso(start)
    if end:
        timeframe["end"] = _day_start_iso(end)
    if period == "custom":
        from_label = from_date.isoformat() if from_date else "..."
        to_label = to_date.isoformat() if to_date else "..."
        timeframe["label"] = f"Custom Range ({from_label} - {to_label})"
    return timeframe


def _payload_value(data: dict, *names, default=None):
    for name in names:
        if name in data and data.get(name) is not None:
            return data.get(name)
    return default


def _string_value(data: dict, *names, max_len=256, default=""):
    value = _payload_value(data, *names, default=default)
    value = str(value or "").strip()
    if value and len(value) > max_len:
        raise ColonisationApiError(f"{names[0]} must not exceed {max_len} characters")
    return value


def _positive_int_value(data: dict, *names, required=False, default=0):
    value = _payload_value(data, *names)
    if value is None:
        if required:
            raise ColonisationApiError(f"{names[0]} is required")
        return default
    if isinstance(value, bool):
        raise ColonisationApiError(f"{names[0]} must be an integer")
    parsed = _int_value(value, None)
    if parsed is None:
        raise ColonisationApiError(f"{names[0]} must be an integer")
    if parsed < 0:
        raise ColonisationApiError(f"{names[0]} must not be negative")
    return parsed


def _candidate_events(event_name, cmdr="", system="", market_id=None, limit=400):
    query = db.session.query(Event).filter(Event.event == event_name)
    if cmdr:
        query = query.filter(func.lower(Event.cmdr) == cmdr.lower())
    if system:
        query = query.filter(func.lower(Event.starsystem) == system.lower())
    if market_id:
        query = query.filter(Event.raw_json.like(f"%{market_id}%"))
    return query.order_by(desc(Event.timestamp), desc(Event.id)).limit(limit).all()


def _payload_market_id(payload: dict):
    return _int_value(payload.get("MarketID"), None)


def _payload_matches(payload: dict, market_id=None, system="") -> bool:
    if market_id is not None and _payload_market_id(payload) != market_id:
        return False
    if system and str(payload.get("StarSystem") or "").lower() != system.lower():
        return False
    return True


def _latest_construction(cmdr="", market_id=None, system=""):
    for event in _candidate_events(CONSTRUCTION_EVENT, cmdr, system, market_id):
        payload = _raw_event_payload(event)
        if payload.get("ResourcesRequired") and _payload_matches(payload, market_id, system):
            return event, payload
    return None, None


def _latest_station_name(cmdr="", market_id=None, system=""):
    for event_name in LOCATION_EVENTS:
        for event in _candidate_events(event_name, cmdr, system, market_id):
            payload = _raw_event_payload(event)
            if not _payload_matches(payload, market_id, system):
                continue
            station = str(payload.get("StationName") or "").strip()
            if station:
                return station
    return ""


def _target_name_from_station(station: str) -> str:
    original = str(station or "").strip()
    target = original
    prefixes = (
        "Orbital Construction Site",
        "Planetary Construction Site",
        "System Colonisation Ship",
        "System Colonization Ship",
    )
    for prefix in prefixes:
        if target.lower().startswith(prefix.lower()):
            target = target[len(prefix):].lstrip(": -").strip()
            break
    return target or "Colonisation Target"


def _latest_status(cmdr="", market_id=None, session_id=""):
    query = db.session.query(ColonisationAssistStatus)
    if market_id is not None:
        query = query.filter(ColonisationAssistStatus.market_id == market_id)
    if cmdr:
        query = query.filter(func.lower(ColonisationAssistStatus.cmdr) == cmdr.lower())
    if session_id:
        query = query.filter(ColonisationAssistStatus.session_id == session_id)
    return query.order_by(desc(ColonisationAssistStatus.updated_at), desc(ColonisationAssistStatus.id)).first()


def _contribution_records(cmdr="", market_id=None, system=""):
    records = []
    for event in _candidate_events(CONTRIBUTION_EVENT, cmdr, system, market_id, limit=1000):
        payload = _raw_event_payload(event)
        if not _payload_matches(payload, market_id, system):
            continue
        total = 0
        commodities = []
        for item in payload.get("Contributions") or []:
            if not isinstance(item, dict):
                continue
            amount = max(0, _int_value(item.get("Amount")))
            if amount <= 0:
                continue
            raw_name = str(item.get("Name") or item.get("Name_Localised") or "").strip()
            commodities.append({
                "commodity": _display_commodity(raw_name, item.get("Name_Localised")),
                "commodity_key": _normalize_commodity_key(raw_name),
                "quantity": amount,
            })
            total += amount
        if total > 0:
            records.append({
                "event_id": event.id,
                "timestamp": event.timestamp,
                "cmdr": event.cmdr,
                "market_id": _payload_market_id(payload),
                "target_system": payload.get("StarSystem") or event.starsystem or "",
                "commodities": commodities,
                "quantity": total,
            })
    return records


def _contribution_events(timeframe, cmdrs=None, market_ids=None, max_events=2000):
    if timeframe.get("period") in {"ct", "lt"} and not timeframe.get("tickid"):
        return []

    market_ids = market_ids or []
    query = db.session.query(Event).filter(Event.event == CONTRIBUTION_EVENT)
    if timeframe.get("tickid"):
        query = query.filter(Event.tickid == timeframe["tickid"])
    if timeframe.get("start"):
        query = query.filter(Event.timestamp >= timeframe["start"])
    if timeframe.get("end"):
        query = query.filter(Event.timestamp < timeframe["end"])
    if cmdrs:
        query = query.filter(func.lower(Event.cmdr).in_([cmdr.lower() for cmdr in cmdrs]))
    if market_ids:
        query = query.filter(or_(*(Event.raw_json.like(f"%{market_id}%") for market_id in market_ids)))
    return query.order_by(desc(Event.timestamp), desc(Event.id)).limit(max_events).all()


def _matches_construction_filter(context: dict, constructions=None) -> bool:
    if not constructions:
        return True
    fields = [
        context.get("label"),
        context.get("construction"),
        context.get("target_name"),
        context.get("target_station"),
        context.get("target_system"),
        context.get("market_id"),
    ]
    haystack = [str(value or "").strip().lower() for value in fields if value not in (None, "")]
    for construction in constructions:
        needle = str(construction or "").strip().lower()
        if not needle:
            continue
        if any(needle == value or needle in value for value in haystack):
            return True
    return False


def _construction_context_for_payload(event: Event, payload: dict, station_cache: dict, status_cache: dict) -> dict:
    market_id = _payload_market_id(payload)
    target_system = str(payload.get("StarSystem") or event.starsystem or "").strip()
    system_address = payload.get("SystemAddress") or event.systemaddress
    station = ""
    status = None

    if market_id:
        station_key = (market_id, target_system.lower())
        if station_key not in station_cache:
            station_cache[station_key] = _latest_station_name("", market_id, target_system)
        station = station_cache.get(station_key) or ""

        if market_id not in status_cache:
            status_cache[market_id] = _latest_status("", market_id)
        status = status_cache.get(market_id)

    if not station and status:
        station = status.target_station or ""

    target_name = _target_name_from_station(station) if station else ""
    if not target_name and status and (status.target_name or "").strip():
        target_name = _target_name_from_station(status.target_name)

    construction_label = (
        target_name
        or (f"{target_system} / Market {market_id}" if target_system and market_id else "")
        or (f"Market {market_id}" if market_id else "")
        or target_system
        or "Unknown Construction"
    )
    construction_key = str(market_id) if market_id else construction_label.lower()

    return {
        "construction_key": construction_key,
        "construction": construction_label,
        "target_name": target_name or construction_label,
        "target_station": station,
        "target_system": target_system,
        "system_address": system_address,
        "market_id": market_id,
    }


def _contribution_line_records(timeframe, cmdrs=None, market_ids=None, constructions=None, max_events=2000):
    records = []
    station_cache = {}
    status_cache = {}
    market_ids = market_ids or []
    market_id_set = set(market_ids)
    for event in _contribution_events(timeframe, cmdrs, market_ids, max_events):
        payload = _raw_event_payload(event)
        if market_id_set and _payload_market_id(payload) not in market_id_set:
            continue
        context = _construction_context_for_payload(event, payload, station_cache, status_cache)
        if not _matches_construction_filter(context, constructions):
            continue
        event_cmdr = str(event.cmdr or payload.get("Cmdr") or "").strip() or "Unknown Cmdr"
        for item in payload.get("Contributions") or []:
            if not isinstance(item, dict):
                continue
            amount = max(0, _int_value(item.get("Amount")))
            if amount <= 0:
                continue
            raw_name = str(item.get("Name") or item.get("Name_Localised") or "").strip()
            commodity = _display_commodity(raw_name, item.get("Name_Localised"))
            record = {
                "event_id": event.id,
                "timestamp": event.timestamp,
                "tickid": event.tickid,
                "ticktime": event.ticktime,
                "cmdr": event_cmdr,
                "cmdr_key": event_cmdr.lower(),
                "commodity": commodity,
                "commodity_key": _normalize_commodity_key(raw_name or commodity),
                "quantity": amount,
            }
            record.update(context)
            records.append(record)
    return records


def _add_commodity_total(bucket: dict, record: dict):
    commodity_key = record.get("commodity_key") or record.get("commodity") or "unknown"
    commodity_map = bucket.setdefault("_commodity_map", {})
    commodity = commodity_map.setdefault(
        commodity_key,
        {
            "commodity_key": commodity_key,
            "commodity": record.get("commodity") or commodity_key,
            "quantity": 0,
            "_event_ids": set(),
        },
    )
    commodity["quantity"] += _int_value(record.get("quantity"))
    commodity["_event_ids"].add(record.get("event_id"))


def _add_contribution_record(bucket: dict, record: dict):
    bucket["quantity"] += _int_value(record.get("quantity"))
    bucket["_event_ids"].add(record.get("event_id"))
    bucket["_cmdrs"].add(record.get("cmdr_key"))
    bucket["_constructions"].add(record.get("construction_key"))
    _add_commodity_total(bucket, record)


def _new_contribution_bucket(key, label, extra=None):
    bucket = {
        "key": str(key or ""),
        "label": str(label or ""),
        "quantity": 0,
        "event_count": 0,
        "cmdr_count": 0,
        "construction_count": 0,
        "commodities": [],
        "_event_ids": set(),
        "_cmdrs": set(),
        "_constructions": set(),
        "_commodity_map": {},
    }
    if extra:
        bucket.update(extra)
    return bucket


def _finalize_contribution_bucket(bucket: dict, *, include_subgroups=False):
    result = {
        key: value
        for key, value in bucket.items()
        if not key.startswith("_") and key != "subgroups"
    }
    result["event_count"] = len(bucket.get("_event_ids") or set())
    result["cmdr_count"] = len(bucket.get("_cmdrs") or set())
    result["construction_count"] = len(bucket.get("_constructions") or set())
    commodities = []
    for item in (bucket.get("_commodity_map") or {}).values():
        commodities.append({
            "commodity_key": item.get("commodity_key"),
            "commodity": item.get("commodity"),
            "quantity": item.get("quantity", 0),
            "event_count": len(item.get("_event_ids") or set()),
        })
    result["commodities"] = sorted(
        commodities,
        key=lambda item: (-_int_value(item.get("quantity")), str(item.get("commodity") or "").lower()),
    )
    if include_subgroups:
        result["subgroups"] = sorted(
            (
                _finalize_contribution_bucket(subgroup, include_subgroups=False)
                for subgroup in (bucket.get("subgroups") or {}).values()
            ),
            key=lambda item: (-_int_value(item.get("quantity")), str(item.get("label") or "").lower()),
        )
    return result


def _aggregate_contribution_records(records: list[dict], group_by: str) -> list[dict]:
    groups = {}
    subgroup_kind = "cmdr" if group_by == "construction" else "construction"

    for record in records:
        if group_by == "construction":
            group_key = record.get("construction_key")
            group_label = record.get("construction")
            group_extra = {
                "market_id": record.get("market_id"),
                "target_name": record.get("target_name"),
                "target_station": record.get("target_station"),
                "target_system": record.get("target_system"),
                "system_address": record.get("system_address"),
            }
            subgroup_key = record.get("cmdr_key")
            subgroup_label = record.get("cmdr")
            subgroup_extra = {"cmdr": record.get("cmdr")}
        else:
            group_key = record.get("cmdr_key")
            group_label = record.get("cmdr")
            group_extra = {"cmdr": record.get("cmdr")}
            subgroup_key = record.get("construction_key")
            subgroup_label = record.get("construction")
            subgroup_extra = {
                "market_id": record.get("market_id"),
                "target_name": record.get("target_name"),
                "target_station": record.get("target_station"),
                "target_system": record.get("target_system"),
                "system_address": record.get("system_address"),
            }

        group = groups.setdefault(
            group_key,
            {
                **_new_contribution_bucket(group_key, group_label, group_extra),
                "subgroup_by": subgroup_kind,
                "subgroups": {},
            },
        )
        _add_contribution_record(group, record)

        subgroup = group["subgroups"].setdefault(
            subgroup_key,
            _new_contribution_bucket(subgroup_key, subgroup_label, subgroup_extra),
        )
        _add_contribution_record(subgroup, record)

    return sorted(
        (
            _finalize_contribution_bucket(group, include_subgroups=True)
            for group in groups.values()
        ),
        key=lambda item: (-_int_value(item.get("quantity")), str(item.get("label") or "").lower()),
    )


def _format_contributions_text(payload: dict) -> str:
    def fit(value, width):
        text = str(value or "")
        if len(text) <= width:
            return text
        return text[:max(0, width - 2)] + ".."

    def qty(value):
        return f"{max(0, _int_value(value))}t"

    filters = payload.get("filters") or {}
    totals = payload.get("totals") or {}
    group_by = filters.get("group_by") or "construction"
    top_title = "Construction" if group_by == "construction" else "Cmdr"
    sub_title = "Cmdr" if group_by == "construction" else "Construction"

    lines = [
        "Colonisation Contributions",
        f"Period : {filters.get('label') or filters.get('period') or 'all'}",
        f"Group  : {top_title} / {sub_title}",
        f"Total  : {qty(totals.get('quantity'))} in {totals.get('event_count', 0)} events",
    ]
    cmdrs = filters.get("cmdrs") or []
    if cmdrs:
        lines.append(f"Cmdr   : {', '.join(cmdrs)}")
    if filters.get("tickid"):
        lines.append(f"Tick   : {filters.get('tickid')}")

    header = f"{top_title:<30} {sub_title:<18} {'Qty':>8} {'Ev':>4}"
    separator = "-" * len(header)
    lines.extend(["", header, separator])
    groups = payload.get("groups") or []
    if not groups:
        lines.append("No contribution data found.")
    for group in groups[:20]:
        lines.append(
            f"{fit(group.get('label'), 30):<30} "
            f"{'TOTAL':<18} "
            f"{qty(group.get('quantity')):>8} "
            f"{_int_value(group.get('event_count')):>4}"
        )
        for subgroup in (group.get("subgroups") or [])[:12]:
            lines.append(
                f"{'':<30} "
                f"{fit(subgroup.get('label'), 18):<18} "
                f"{qty(subgroup.get('quantity')):>8} "
                f"{_int_value(subgroup.get('event_count')):>4}"
            )
            for commodity in (subgroup.get("commodities") or [])[:10]:
                lines.append(
                    f"{'':<30} "
                    f"{'  - ' + fit(commodity.get('commodity'), 14):<18} "
                    f"{qty(commodity.get('quantity')):>8} "
                    f"{_int_value(commodity.get('event_count')):>4}"
                )
    lines.append(separator)
    return "\n".join(lines)


@colonisation_bp.route("/api/colonisation/contributions", methods=["GET"])
@require_colonisation_api_key
def get_colonisation_contributions():
    try:
        group_by = (_optional_string("group_by", "groupBy", max_len=32) or "construction").lower()
        group_by = group_by.replace("-", "_")
        if group_by in {"construction_cmdr", "construction"}:
            group_by = "construction"
        elif group_by in {"cmdr_construction", "cmdr"}:
            group_by = "cmdr"
        else:
            raise ColonisationApiError("group_by must be construction or cmdr")

        timeframe = _contribution_timeframe_from_request()
        cmdrs = _split_csv_arg("cmdr", "cmdrs", "commander")
        market_ids = _split_int_arg("market_id", "market_ids", "marketId", "marketIds")
        construction_filters = _split_csv_arg("construction", "constructions", max_len=128)
        max_events = max(1, min(_request_int_arg("max_events", "limit", default=2000) or 2000, 10000))
        records = _contribution_line_records(timeframe, cmdrs, market_ids, construction_filters, max_events)
        groups = _aggregate_contribution_records(records, group_by)

        unique_event_ids = {record.get("event_id") for record in records}
        cmdr_map = {}
        construction_map = {}
        commodity_map = {}
        for record in records:
            cmdr_map.setdefault(record.get("cmdr_key"), record.get("cmdr"))
            construction_map.setdefault(record.get("construction_key"), record.get("construction"))
            key = record.get("commodity_key")
            commodity = commodity_map.setdefault(
                key,
                {"commodity_key": key, "commodity": record.get("commodity"), "quantity": 0, "_event_ids": set()},
            )
            commodity["quantity"] += _int_value(record.get("quantity"))
            commodity["_event_ids"].add(record.get("event_id"))

        response = {
            "tenant": g.tenant.get("name") if getattr(g, "tenant", None) else "",
            "filters": {
                "period": timeframe.get("period"),
                "label": timeframe.get("label"),
                "start": timeframe.get("start"),
                "end": timeframe.get("end"),
                "tickid": timeframe.get("tickid"),
                "ticktime": timeframe.get("ticktime"),
                "latest_tick_timestamp": timeframe.get("latest_tick_timestamp"),
                "group_by": group_by,
                "subgroup_by": "cmdr" if group_by == "construction" else "construction",
                "cmdrs": cmdrs,
                "market_id": market_ids[0] if len(market_ids) == 1 else None,
                "market_ids": market_ids,
                "constructions": construction_filters,
                "max_events": max_events,
            },
            "totals": {
                "quantity": sum(_int_value(record.get("quantity")) for record in records),
                "event_count": len(unique_event_ids),
                "cmdr_count": len([value for value in cmdr_map.values() if value]),
                "construction_count": len([value for value in construction_map.values() if value]),
                "commodity_count": len([value for value in commodity_map.values() if value]),
                "record_count": len(records),
            },
            "cmdrs": sorted((value for value in cmdr_map.values() if value), key=str.lower),
            "constructions": sorted((value for value in construction_map.values() if value), key=str.lower),
            "commodities": sorted(
                (
                    {
                        "commodity_key": item.get("commodity_key"),
                        "commodity": item.get("commodity"),
                        "quantity": item.get("quantity", 0),
                        "event_count": len(item.get("_event_ids") or set()),
                    }
                    for item in commodity_map.values()
                ),
                key=lambda item: (-_int_value(item.get("quantity")), str(item.get("commodity") or "").lower()),
            ),
            "groups": groups,
            "records": records,
            "updated_at": _utc_now(),
        }
        response["text"] = _format_contributions_text(response)
        return jsonify(response)
    except ColonisationApiError as e:
        return _json_response_error(e.message, e.status_code)
    except Exception as e:
        logger.exception("Colonisation contributions error")
        return _json_response_error(str(e), 500)


def _construction_resource_rows(payload: dict) -> list[dict]:
    rows = []
    for item in payload.get("ResourcesRequired") or []:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("Name") or item.get("Name_Localised") or "").strip()
        if not raw_name:
            continue
        required = max(0, _int_value(item.get("RequiredAmount")))
        provided = max(0, _int_value(item.get("ProvidedAmount")))
        remaining = max(0, required - provided)
        rows.append({
            "commodity": _display_commodity(raw_name, item.get("Name_Localised")),
            "commodity_key": _normalize_commodity_key(raw_name),
            "journal_name": raw_name,
            "need": required,
            "provided": provided,
            "remaining": remaining,
            "state": "done" if remaining <= 0 else "open",
            "payment": max(0, _int_value(item.get("Payment"))),
        })
    rows.sort(key=lambda item: str(item.get("commodity") or "").lower())
    return rows


def _latest_construction_snapshots(limit=5000):
    station_cache = {}
    status_cache = {}
    snapshots = []
    seen = set()
    events = (
        db.session.query(Event)
        .filter(Event.event == CONSTRUCTION_EVENT)
        .order_by(desc(Event.timestamp), desc(Event.id))
        .limit(max(1, min(limit, 20000)))
        .all()
    )
    for event in events:
        payload = _raw_event_payload(event)
        if not payload.get("ResourcesRequired"):
            continue
        market_id = _payload_market_id(payload)
        construction_key = str(market_id) if market_id else f"{event.starsystem}:{event.id}"
        if construction_key in seen:
            continue
        seen.add(construction_key)
        context = _construction_context_for_payload(event, payload, station_cache, status_cache)
        rows = _construction_resource_rows(payload)
        total_need = sum(_int_value(row.get("need")) for row in rows)
        total_provided = sum(_int_value(row.get("provided")) for row in rows)
        total_remaining = sum(_int_value(row.get("remaining")) for row in rows)
        is_finished = _bool_value(payload.get("ConstructionComplete")) or (bool(rows) and total_remaining <= 0)
        is_failed = _bool_value(payload.get("ConstructionFailed"))
        status = "finished" if is_finished else ("failed" if is_failed else "open")
        snapshots.append({
            "key": context.get("construction_key"),
            "label": context.get("construction"),
            "status": status,
            "target_name": context.get("target_name"),
            "target_station": context.get("target_station"),
            "target_system": context.get("target_system"),
            "system_address": context.get("system_address"),
            "market_id": context.get("market_id"),
            "construction_progress": float(payload.get("ConstructionProgress") or 0.0),
            "construction_complete": is_finished,
            "construction_failed": is_failed,
            "latest_event_id": event.id,
            "latest_event_timestamp": event.timestamp,
            "latest_event_cmdr": event.cmdr or "",
            "commodities": rows,
            "total_need": total_need,
            "total_provided": total_provided,
            "total_remaining": total_remaining,
        })
    return snapshots


def _update_first_last(bucket: dict, timestamp: str):
    if not timestamp:
        return
    if not bucket.get("first_delivery_at") or timestamp < bucket["first_delivery_at"]:
        bucket["first_delivery_at"] = timestamp
    if not bucket.get("last_delivery_at") or timestamp > bucket["last_delivery_at"]:
        bucket["last_delivery_at"] = timestamp


def _contribution_stats_for_records(records: list[dict]) -> dict:
    stats = {}
    for record in records or []:
        commodity_key = record.get("commodity_key") or record.get("commodity") or "unknown"
        commodity = stats.setdefault(
            commodity_key,
            {
                "commodity_key": commodity_key,
                "commodity": record.get("commodity") or commodity_key,
                "recorded_quantity": 0,
                "event_count": 0,
                "first_delivery_at": "",
                "last_delivery_at": "",
                "_event_ids": set(),
                "_contributors": {},
            },
        )
        amount = _int_value(record.get("quantity"))
        commodity["recorded_quantity"] += amount
        commodity["_event_ids"].add(record.get("event_id"))
        _update_first_last(commodity, record.get("timestamp") or "")

        cmdr_key = record.get("cmdr_key") or record.get("cmdr") or "unknown"
        contributor = commodity["_contributors"].setdefault(
            cmdr_key,
            {
                "cmdr": record.get("cmdr") or "Unknown Cmdr",
                "quantity": 0,
                "event_count": 0,
                "first_delivery_at": "",
                "last_delivery_at": "",
                "_event_ids": set(),
            },
        )
        contributor["quantity"] += amount
        contributor["_event_ids"].add(record.get("event_id"))
        _update_first_last(contributor, record.get("timestamp") or "")

    for commodity in stats.values():
        commodity["event_count"] = len(commodity.get("_event_ids") or set())
        contributors = []
        for contributor in (commodity.get("_contributors") or {}).values():
            contributors.append({
                "cmdr": contributor.get("cmdr"),
                "quantity": contributor.get("quantity", 0),
                "event_count": len(contributor.get("_event_ids") or set()),
                "first_delivery_at": contributor.get("first_delivery_at") or "",
                "last_delivery_at": contributor.get("last_delivery_at") or "",
            })
        commodity["contributors"] = sorted(
            contributors,
            key=lambda item: (-_int_value(item.get("quantity")), str(item.get("cmdr") or "").lower()),
        )
    return stats


def _construction_status_payload(snapshot: dict, records: list[dict]) -> dict:
    stats = _contribution_stats_for_records(records)
    commodities = []
    delivered_by = {}
    rows = []

    for item in snapshot.get("commodities") or []:
        commodity_key = item.get("commodity_key")
        contribution = stats.get(commodity_key, {})
        recorded_quantity = _int_value(contribution.get("recorded_quantity"))
        provided = _int_value(item.get("provided"))
        commodity = {
            **item,
            "recorded_quantity": recorded_quantity,
            "unrecorded_quantity": max(0, provided - recorded_quantity),
            "first_delivery_at": contribution.get("first_delivery_at") or "",
            "last_delivery_at": contribution.get("last_delivery_at") or "",
            "event_count": _int_value(contribution.get("event_count")),
            "contributors": contribution.get("contributors") or [],
        }
        commodities.append(commodity)

        if not commodity["contributors"]:
            rows.append({
                "construction": snapshot.get("label"),
                "construction_status": snapshot.get("status"),
                "target_system": snapshot.get("target_system"),
                "market_id": snapshot.get("market_id"),
                "commodity": commodity.get("commodity"),
                "need": commodity.get("need"),
                "provided": commodity.get("provided"),
                "remaining": commodity.get("remaining"),
                "state": commodity.get("state"),
                "recorded_quantity": recorded_quantity,
                "unrecorded_quantity": commodity.get("unrecorded_quantity"),
                "cmdr": "",
                "cmdr_quantity": 0,
                "first_delivery_at": "",
                "last_delivery_at": "",
                "event_count": 0,
            })
        for contributor in commodity["contributors"]:
            cmdr = contributor.get("cmdr") or "Unknown Cmdr"
            cmdr_bucket = delivered_by.setdefault(
                cmdr.lower(),
                {
                    "cmdr": cmdr,
                    "quantity": 0,
                    "event_count": 0,
                    "first_delivery_at": "",
                    "last_delivery_at": "",
                },
            )
            cmdr_bucket["quantity"] += _int_value(contributor.get("quantity"))
            cmdr_bucket["event_count"] += _int_value(contributor.get("event_count"))
            _update_first_last(cmdr_bucket, contributor.get("first_delivery_at") or "")
            _update_first_last(cmdr_bucket, contributor.get("last_delivery_at") or "")
            rows.append({
                "construction": snapshot.get("label"),
                "construction_status": snapshot.get("status"),
                "target_system": snapshot.get("target_system"),
                "market_id": snapshot.get("market_id"),
                "commodity": commodity.get("commodity"),
                "need": commodity.get("need"),
                "provided": commodity.get("provided"),
                "remaining": commodity.get("remaining"),
                "state": commodity.get("state"),
                "recorded_quantity": recorded_quantity,
                "unrecorded_quantity": commodity.get("unrecorded_quantity"),
                "cmdr": cmdr,
                "cmdr_quantity": _int_value(contributor.get("quantity")),
                "first_delivery_at": contributor.get("first_delivery_at") or "",
                "last_delivery_at": contributor.get("last_delivery_at") or "",
                "event_count": _int_value(contributor.get("event_count")),
            })

    delivered_by_rows = []
    for item in delivered_by.values():
        delivered_by_rows.append({
            "cmdr": item.get("cmdr"),
            "quantity": item.get("quantity", 0),
            "event_count": item.get("event_count", 0),
            "first_delivery_at": item.get("first_delivery_at") or "",
            "last_delivery_at": item.get("last_delivery_at") or "",
        })

    return {
        **snapshot,
        "recorded_quantity": sum(_int_value(item.get("recorded_quantity")) for item in commodities),
        "unrecorded_quantity": sum(_int_value(item.get("unrecorded_quantity")) for item in commodities),
        "contributors": sorted(
            delivered_by_rows,
            key=lambda item: (-_int_value(item.get("quantity")), str(item.get("cmdr") or "").lower()),
        ),
        "commodities": commodities,
        "rows": rows,
    }


def _format_constructions_text(payload: dict) -> str:
    def fit(value, width):
        text = str(value or "")
        if len(text) <= width:
            return text
        return text[:max(0, width - 2)] + ".."

    filters = payload.get("filters") or {}
    status = filters.get("status") or "all"
    lines = [
        "Colonisation Constructions",
        f"Period : {filters.get('label') or filters.get('period') or 'all'}",
        f"Status : {status}",
    ]
    cmdrs = filters.get("cmdrs") or []
    if cmdrs:
        lines.append(f"Cmdr   : {', '.join(cmdrs)}")

    header = f"{'Construction':<30} {'Status':<8} {'Need':>6} {'Prov':>6} {'Rem':>6} {'Rec':>6}"
    separator = "-" * len(header)
    lines.extend(["", header, separator])
    constructions = payload.get("constructions") or []
    if not constructions:
        lines.append("No construction data found.")
    for construction in constructions[:20]:
        lines.append(
            f"{fit(construction.get('label'), 30):<30} "
            f"{fit(construction.get('status'), 8):<8} "
            f"{_int_value(construction.get('total_need')):>6} "
            f"{_int_value(construction.get('total_provided')):>6} "
            f"{_int_value(construction.get('total_remaining')):>6} "
            f"{_int_value(construction.get('recorded_quantity')):>6}"
        )
        for commodity in (construction.get("commodities") or [])[:12]:
            lines.append(
                f"{'':<30} "
                f"{fit(commodity.get('commodity'), 8):<8} "
                f"{_int_value(commodity.get('need')):>6} "
                f"{_int_value(commodity.get('provided')):>6} "
                f"{_int_value(commodity.get('remaining')):>6} "
                f"{_int_value(commodity.get('recorded_quantity')):>6}"
            )
            for contributor in (commodity.get("contributors") or [])[:6]:
                lines.append(
                    f"{'':<30} "
                    f"{'  ' + fit(contributor.get('cmdr'), 6):<8} "
                    f"{'':>6} {'':>6} {'':>6} "
                    f"{_int_value(contributor.get('quantity')):>6}"
                )
    lines.append(separator)
    return "\n".join(lines)


@colonisation_bp.route("/api/colonisation/constructions", methods=["GET"])
@require_colonisation_api_key
def get_colonisation_constructions():
    try:
        requested_status = (_optional_string("status", max_len=32) or "all").lower()
        if requested_status not in {"open", "finished", "failed", "all"}:
            raise ColonisationApiError("status must be open, finished, failed, or all")
        timeframe = _contribution_timeframe_from_request()
        cmdrs = _split_csv_arg("cmdr", "cmdrs", "commander")
        market_ids = _split_int_arg("market_id", "market_ids", "marketId", "marketIds")
        construction_filters = _split_csv_arg("construction", "constructions", max_len=128)
        max_events = max(1, min(_request_int_arg("max_events", "limit", default=5000) or 5000, 10000))
        construction_limit = max(1, min(_request_int_arg("construction_limit", default=5000) or 5000, 20000))

        contribution_records = _contribution_line_records(timeframe, cmdrs, market_ids, construction_filters, max_events)
        records_by_market = {}
        for record in contribution_records:
            records_by_market.setdefault(record.get("market_id"), []).append(record)

        construction_results = []
        for snapshot in _latest_construction_snapshots(construction_limit):
            snapshot_market_id = snapshot.get("market_id")
            if market_ids and snapshot_market_id not in set(market_ids):
                continue
            if not _matches_construction_filter(snapshot, construction_filters):
                continue
            if requested_status != "all" and snapshot.get("status") != requested_status:
                continue
            matching_records = records_by_market.get(snapshot_market_id, [])
            if cmdrs and not matching_records:
                continue
            construction_results.append(_construction_status_payload(snapshot, matching_records))

        construction_results.sort(
            key=lambda item: (
                0 if item.get("status") == "open" else 1,
                -_int_value(item.get("total_remaining")),
                str(item.get("label") or "").lower(),
            )
        )
        flat_rows = []
        for construction in construction_results:
            flat_rows.extend(construction.get("rows") or [])

        status_counts = {"open": 0, "finished": 0, "failed": 0}
        for construction in construction_results:
            if construction.get("status") in status_counts:
                status_counts[construction["status"]] += 1

        response = {
            "tenant": g.tenant.get("name") if getattr(g, "tenant", None) else "",
            "filters": {
                "period": timeframe.get("period"),
                "label": timeframe.get("label"),
                "start": timeframe.get("start"),
                "end": timeframe.get("end"),
                "tickid": timeframe.get("tickid"),
                "ticktime": timeframe.get("ticktime"),
                "latest_tick_timestamp": timeframe.get("latest_tick_timestamp"),
                "status": requested_status,
                "cmdrs": cmdrs,
                "market_id": market_ids[0] if len(market_ids) == 1 else None,
                "market_ids": market_ids,
                "constructions": construction_filters,
                "max_events": max_events,
                "construction_limit": construction_limit,
            },
            "totals": {
                "construction_count": len(construction_results),
                "open_count": status_counts["open"],
                "finished_count": status_counts["finished"],
                "failed_count": status_counts["failed"],
                "total_need": sum(_int_value(item.get("total_need")) for item in construction_results),
                "total_provided": sum(_int_value(item.get("total_provided")) for item in construction_results),
                "total_remaining": sum(_int_value(item.get("total_remaining")) for item in construction_results),
                "recorded_quantity": sum(_int_value(item.get("recorded_quantity")) for item in construction_results),
                "unrecorded_quantity": sum(_int_value(item.get("unrecorded_quantity")) for item in construction_results),
                "row_count": len(flat_rows),
            },
            "constructions": construction_results,
            "rows": flat_rows,
            "updated_at": _utc_now(),
        }
        response["text"] = _format_constructions_text(response)
        return jsonify(response)
    except ColonisationApiError as e:
        return _json_response_error(e.message, e.status_code)
    except Exception as e:
        logger.exception("Colonisation constructions error")
        return _json_response_error(str(e), 500)


def _delivery_query(cmdr="", market_id=None, session_id=""):
    query = db.session.query(ColonisationDelivery)
    if market_id is not None:
        query = query.filter(ColonisationDelivery.market_id == market_id)
    if cmdr:
        query = query.filter(func.lower(ColonisationDelivery.cmdr) == cmdr.lower())
    if session_id:
        query = query.filter(ColonisationDelivery.session_id == session_id)
    return query


def _delivery_snapshot(record: ColonisationDelivery) -> dict:
    return {
        "id": record.id,
        "delivery_id": record.delivery_id,
        "batch_id": record.batch_id or "",
        "session_id": record.session_id or "",
        "client_id": record.client_id or "",
        "client_name": record.client_name or "",
        "cmdr": record.cmdr,
        "target_name": record.target_name or "",
        "target_system": record.target_system or "",
        "target_station": record.target_station or "",
        "market_id": record.market_id,
        "commodity_key": record.commodity_key,
        "commodity": record.commodity,
        "quantity": record.quantity,
        "source": record.source or "",
        "verification_source": record.verification_source or "",
        "event_id": record.event_id,
        "note": record.note or "",
        "created_at": record.created_at,
        "received_at": record.received_at,
    }


def _status_snapshot(record: ColonisationAssistStatus | None) -> dict:
    if not record:
        return {}
    return {
        "id": record.id,
        "status_id": record.status_id,
        "session_id": record.session_id or "",
        "client_id": record.client_id or "",
        "client_name": record.client_name or "",
        "cmdr": record.cmdr,
        "target_name": record.target_name or "",
        "target_system": record.target_system or "",
        "target_station": record.target_station or "",
        "market_id": record.market_id,
        "phase": record.phase or "",
        "reason": record.reason or "",
        "cargo_count": max(0, _int_value(record.cargo_count)),
        "updated_at": record.updated_at,
        "received_at": record.received_at,
    }


def _state_label(row: dict) -> str:
    if row.get("unavailable"):
        return "unavail"
    if _int_value(row.get("remaining")) <= 0:
        return "done"
    return "open"


def _format_summary_text(summary: dict) -> str:
    def fit(value, width):
        text = str(value or "")
        if len(text) <= width:
            return text
        return text[:max(0, width - 2)] + ".."

    def qty(value):
        return str(max(0, _int_value(value)))

    lines = [
        "Colonisation Summary",
        f"System : {summary.get('target_system') or '-'}",
        f"Target : {summary.get('target_name') or summary.get('target_station') or '-'}",
    ]
    market_id = _int_value(summary.get("market_id"))
    if market_id:
        lines.append(f"Market : {market_id}")
    reason = str(summary.get("reason") or "").strip()
    if reason:
        lines.append(f"Reason : {reason}")

    column_header = f"{'Cmdty':<22} {'Need':>6} {'Prov':>6} {'Rem':>6} {'State':<7}"
    separator = "-" * len(column_header)
    lines.extend(["", column_header, separator])
    for row in summary.get("rows") or []:
        lines.append(
            f"{fit(row.get('commodity'), 22):<22} "
            f"{qty(row.get('need')):>6} "
            f"{qty(row.get('provided')):>6} "
            f"{qty(row.get('remaining')):>6} "
            f"{fit(_state_label(row), 7):<7}"
        )
    lines.append(separator)
    lines.append(
        f"{'TOTAL':<22} "
        f"{qty(summary.get('total_need')):>6} "
        f"{qty(summary.get('total_provided')):>6} "
        f"{qty(summary.get('total_remaining')):>6} "
        f"{str(summary.get('unavailable_count') or 0) + ' unav':<7}"
    )
    lines.append(
        f"Delivered session {qty(summary.get('session_delivered'))}t; "
        f"local total {qty(summary.get('total_delivered'))}t; "
        f"cargo {qty(summary.get('cargo_count'))}t."
    )
    return "\n".join(lines)


def _summary_from_latest_events(cmdr="", market_id=None, system="", session_id="", reason="", cargo_count=None):
    construction_event, construction = _latest_construction(cmdr, market_id, system)
    if not construction:
        raise ColonisationApiError("No ColonisationConstructionDepot event found", 404)

    resolved_market_id = _payload_market_id(construction)
    resolved_system = str(construction.get("StarSystem") or construction_event.starsystem or "").strip()
    status = _latest_status(cmdr, resolved_market_id, session_id)

    target_station = (
        _optional_string("target_station", "targetStation", max_len=128)
        or (status.target_station if status else "")
        or _latest_station_name(cmdr, resolved_market_id, resolved_system)
    )
    raw_target_name = (
        _optional_string("target_name", "targetName", max_len=128)
        or (status.target_name if status else "")
    )
    target_name = _target_name_from_station(raw_target_name) if raw_target_name else _target_name_from_station(target_station)
    if not target_station:
        target_station = target_name

    if not session_id and status:
        session_id = status.session_id or ""
    if not reason and status:
        reason = status.reason or ""
    if cargo_count is None and status:
        cargo_count = max(0, _int_value(status.cargo_count))
    cargo_count = max(0, _int_value(cargo_count))

    rows = []
    for item in construction.get("ResourcesRequired") or []:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("Name") or item.get("Name_Localised") or "").strip()
        if not raw_name:
            continue
        required = max(0, _int_value(item.get("RequiredAmount")))
        provided = max(0, _int_value(item.get("ProvidedAmount")))
        remaining = max(0, required - provided)
        rows.append({
            "commodity": _display_commodity(raw_name, item.get("Name_Localised")),
            "commodity_key": _normalize_commodity_key(raw_name),
            "journal_name": raw_name,
            "need": required,
            "provided": provided,
            "remaining": remaining,
            "state": "done" if remaining <= 0 else "open",
            "unavailable": False,
            "payment": max(0, _int_value(item.get("Payment"))),
        })
    rows.sort(key=lambda item: str(item.get("commodity") or "").lower())

    total_need = sum(_int_value(row.get("need")) for row in rows)
    total_provided = sum(_int_value(row.get("provided")) for row in rows)
    total_remaining = sum(_int_value(row.get("remaining")) for row in rows)

    delivery_query = _delivery_query(cmdr, resolved_market_id)
    central_delivery_count = delivery_query.count()
    if central_delivery_count:
        total_delivered = sum(_int_value(row.quantity) for row in delivery_query.all())
        if session_id:
            session_delivered = sum(
                _int_value(row.quantity)
                for row in _delivery_query(cmdr, resolved_market_id, session_id).all()
            )
        else:
            session_delivered = 0
    else:
        contributions = _contribution_records(cmdr, resolved_market_id, resolved_system)
        total_delivered = total_provided
        session_delivered = contributions[0]["quantity"] if contributions else 0

    summary = {
        "tenant": g.tenant.get("name") if getattr(g, "tenant", None) else "",
        "cmdr": cmdr or construction_event.cmdr or "",
        "session_id": session_id,
        "target_name": target_name,
        "target_system": resolved_system,
        "target_station": target_station,
        "market_id": resolved_market_id,
        "system_address": construction.get("SystemAddress") or construction_event.systemaddress,
        "construction_progress": float(construction.get("ConstructionProgress") or 0.0),
        "construction_complete": _bool_value(construction.get("ConstructionComplete")),
        "construction_failed": _bool_value(construction.get("ConstructionFailed")),
        "latest_event_id": construction_event.id,
        "latest_event_timestamp": construction_event.timestamp,
        "rows": rows,
        "total_need": total_need,
        "total_provided": total_provided,
        "total_remaining": total_remaining,
        "unavailable_count": 0,
        "session_delivered": session_delivered,
        "total_delivered": total_delivered,
        "cargo_count": cargo_count,
        "reason": reason,
        "central_delivery_count": central_delivery_count,
        "latest_status": _status_snapshot(status),
        "updated_at": _utc_now(),
    }
    summary["text"] = _format_summary_text(summary)
    return summary


@colonisation_bp.route("/api/colonisation/summary", methods=["GET"])
@require_colonisation_api_key
def get_colonisation_summary():
    try:
        summary = _summary_from_latest_events(
            cmdr=_optional_string("cmdr", max_len=64),
            market_id=_request_int_arg("market_id", "marketId"),
            system=_optional_string("system", "target_system", "targetSystem", max_len=128),
            session_id=_optional_string("session_id", "sessionId", max_len=128),
            reason=_optional_string("reason", max_len=512),
            cargo_count=_request_int_arg("cargo_count", "cargoCount"),
        )
        return jsonify(summary)
    except ColonisationApiError as e:
        return _json_response_error(e.message, e.status_code)
    except Exception as e:
        logger.exception("Colonisation summary error")
        return _json_response_error(str(e), 500)


@colonisation_bp.route("/api/colonisation/summary/text", methods=["GET"])
@require_colonisation_api_key
def get_colonisation_summary_text():
    try:
        summary = _summary_from_latest_events(
            cmdr=_optional_string("cmdr", max_len=64),
            market_id=_request_int_arg("market_id", "marketId"),
            system=_optional_string("system", "target_system", "targetSystem", max_len=128),
            session_id=_optional_string("session_id", "sessionId", max_len=128),
            reason=_optional_string("reason", max_len=512),
            cargo_count=_request_int_arg("cargo_count", "cargoCount"),
        )
        return Response(summary["text"], content_type="text/plain; charset=utf-8")
    except ColonisationApiError as e:
        return _json_response_error(e.message, e.status_code)
    except Exception as e:
        logger.exception("Colonisation summary text error")
        return _json_response_error(str(e), 500)


@colonisation_bp.route("/api/colonisation/targets", methods=["GET"])
@require_colonisation_api_key
def get_colonisation_targets():
    try:
        cmdr = _optional_string("cmdr", max_len=64)
        system = _optional_string("system", max_len=128)
        limit = max(1, min(_request_int_arg("limit", default=25) or 25, 100))
        seen = set()
        targets = []
        for event in _candidate_events(CONSTRUCTION_EVENT, cmdr, system, None, limit=1000):
            payload = _raw_event_payload(event)
            market_id = _payload_market_id(payload)
            if not market_id or market_id in seen:
                continue
            seen.add(market_id)
            event_system = str(payload.get("StarSystem") or event.starsystem or "").strip()
            station = _latest_station_name(cmdr, market_id, event_system)
            rows = payload.get("ResourcesRequired") or []
            total_need = sum(_int_value(item.get("RequiredAmount")) for item in rows if isinstance(item, dict))
            total_provided = sum(_int_value(item.get("ProvidedAmount")) for item in rows if isinstance(item, dict))
            targets.append({
                "market_id": market_id,
                "target_system": event_system,
                "target_station": station,
                "target_name": _target_name_from_station(station),
                "system_address": payload.get("SystemAddress") or event.systemaddress,
                "construction_progress": float(payload.get("ConstructionProgress") or 0.0),
                "construction_complete": _bool_value(payload.get("ConstructionComplete")),
                "construction_failed": _bool_value(payload.get("ConstructionFailed")),
                "total_need": total_need,
                "total_provided": total_provided,
                "total_remaining": max(0, total_need - total_provided),
                "latest_event_id": event.id,
                "latest_event_timestamp": event.timestamp,
                "cmdr": event.cmdr or "",
            })
            if len(targets) >= limit:
                break
        return jsonify({"targets": targets, "count": len(targets)})
    except Exception as e:
        logger.exception("Colonisation targets error")
        return _json_response_error(str(e), 500)


def _normalize_delivery_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ColonisationApiError("Delivery must be a JSON object")
    market_id = _positive_int_value(
        data,
        "market_id",
        "marketId",
        "TargetMarketID",
        "ConstructionMarketID",
        required=True,
    )
    quantity = _positive_int_value(data, "quantity", "Quantity", required=True)
    if quantity <= 0:
        raise ColonisationApiError("quantity must be greater than 0")

    cmdr = _string_value(data, "cmdr", "CmdrName", "Commander", "RcCmdrName", max_len=64)
    if not cmdr:
        raise ColonisationApiError("cmdr is required")
    commodity_raw = _string_value(data, "commodity", "Commodity", "Name_Localised", "Name", max_len=128)
    commodity_key = _string_value(data, "commodity_key", "CommodityKey", max_len=128)
    if not commodity_key:
        commodity_key = _normalize_commodity_key(commodity_raw)
    if not commodity_key:
        raise ColonisationApiError("commodity or commodity_key is required")

    return {
        "delivery_id": _string_value(data, "delivery_id", "DeliveryId", max_len=128) or str(uuid4()),
        "batch_id": _string_value(data, "batch_id", "BatchId", max_len=128),
        "session_id": _string_value(data, "session_id", "SessionId", max_len=128),
        "client_id": _string_value(data, "client_id", "ClientId", max_len=128),
        "client_name": _string_value(data, "client_name", "ClientName", max_len=128),
        "cmdr": cmdr,
        "target_name": _string_value(data, "target_name", "TargetName", "Name", max_len=128),
        "target_system": _string_value(data, "target_system", "TargetSystem", max_len=128),
        "target_station": _string_value(data, "target_station", "TargetStation", max_len=128),
        "market_id": market_id,
        "commodity_key": commodity_key,
        "commodity": _display_commodity(commodity_raw or commodity_key, commodity_raw),
        "quantity": quantity,
        "source": _string_value(data, "source", "Source", max_len=64) or "ColonisationContribution",
        "verification_source": _string_value(data, "verification_source", "VerificationSource", max_len=64),
        "event_id": _positive_int_value(data, "event_id", "EventId", default=None),
        "note": _string_value(data, "note", "Note", max_len=1000),
        "created_at": _string_value(data, "created_at", "CreatedAt", max_len=64) or _utc_now(),
        "received_at": _utc_now(),
        "payload_json": json.dumps(data, ensure_ascii=False, sort_keys=True),
    }


@colonisation_bp.route("/api/colonisation/deliveries", methods=["POST"])
@require_colonisation_api_key
def post_colonisation_deliveries():
    try:
        payload = request.get_json(force=True, silent=False)
        raw_deliveries = payload.get("deliveries") if isinstance(payload, dict) and "deliveries" in payload else payload
        if isinstance(raw_deliveries, dict):
            raw_deliveries = [raw_deliveries]
        if not isinstance(raw_deliveries, list) or not raw_deliveries:
            raise ColonisationApiError("Request body must be a delivery object or non-empty deliveries list")

        saved = []
        duplicates = []
        for item in raw_deliveries:
            normalized = _normalize_delivery_payload(item)
            existing = (
                db.session.query(ColonisationDelivery)
                .filter(ColonisationDelivery.delivery_id == normalized["delivery_id"])
                .first()
            )
            if existing:
                duplicates.append(_delivery_snapshot(existing))
                continue
            record = ColonisationDelivery(**normalized)
            db.session.add(record)
            saved.append(record)

        db.session.commit()
        return jsonify({
            "status": "success",
            "saved": [_delivery_snapshot(record) for record in saved],
            "duplicates": duplicates,
            "saved_count": len(saved),
            "duplicate_count": len(duplicates),
        })
    except ColonisationApiError as e:
        db.session.rollback()
        return _json_response_error(e.message, e.status_code)
    except Exception as e:
        db.session.rollback()
        logger.exception("Colonisation delivery post error")
        return _json_response_error(str(e), 500)


@colonisation_bp.route("/api/colonisation/deliveries", methods=["GET"])
@require_colonisation_api_key
def get_colonisation_deliveries():
    try:
        cmdr = _optional_string("cmdr", max_len=64)
        market_id = _request_int_arg("market_id", "marketId")
        session_id = _optional_string("session_id", "sessionId", max_len=128)
        limit = max(1, min(_request_int_arg("limit", default=100) or 100, 500))
        query = _delivery_query(cmdr, market_id, session_id)
        total_quantity = sum(_int_value(row.quantity) for row in query.all())
        records = (
            query.order_by(desc(ColonisationDelivery.created_at), desc(ColonisationDelivery.id))
            .limit(limit)
            .all()
        )
        return jsonify({
            "deliveries": [_delivery_snapshot(record) for record in records],
            "count": len(records),
            "total_quantity": total_quantity,
        })
    except Exception as e:
        logger.exception("Colonisation delivery list error")
        return _json_response_error(str(e), 500)


def _normalize_status_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ColonisationApiError("Status must be a JSON object")
    market_id = _positive_int_value(
        data,
        "market_id",
        "marketId",
        "TargetMarketID",
        "ConstructionMarketID",
        required=True,
    )
    cmdr = _string_value(data, "cmdr", "CmdrName", "Commander", "RcCmdrName", max_len=64)
    if not cmdr:
        raise ColonisationApiError("cmdr is required")
    updated_at = _string_value(data, "updated_at", "UpdatedAt", max_len=64) or _utc_now()
    return {
        "status_id": _string_value(data, "status_id", "StatusId", max_len=128) or str(uuid4()),
        "session_id": _string_value(data, "session_id", "SessionId", max_len=128),
        "client_id": _string_value(data, "client_id", "ClientId", max_len=128),
        "client_name": _string_value(data, "client_name", "ClientName", max_len=128),
        "cmdr": cmdr,
        "target_name": _string_value(data, "target_name", "TargetName", "Name", max_len=128),
        "target_system": _string_value(data, "target_system", "TargetSystem", max_len=128),
        "target_station": _string_value(data, "target_station", "TargetStation", max_len=128),
        "market_id": market_id,
        "phase": _string_value(data, "phase", "Phase", max_len=64),
        "reason": _string_value(data, "reason", "Reason", max_len=1000),
        "cargo_count": _positive_int_value(data, "cargo_count", "CargoCount", default=0),
        "updated_at": updated_at,
        "received_at": _utc_now(),
        "payload_json": json.dumps(data, ensure_ascii=False, sort_keys=True),
    }


@colonisation_bp.route("/api/colonisation/status", methods=["POST"])
@require_colonisation_api_key
def post_colonisation_status():
    try:
        payload = request.get_json(force=True, silent=False)
        normalized = _normalize_status_payload(payload)
        existing = (
            db.session.query(ColonisationAssistStatus)
            .filter(ColonisationAssistStatus.status_id == normalized["status_id"])
            .first()
        )
        if existing:
            for key, value in normalized.items():
                setattr(existing, key, value)
            record = existing
            status = "updated"
        else:
            record = ColonisationAssistStatus(**normalized)
            db.session.add(record)
            status = "created"
        db.session.commit()
        return jsonify({"status": status, "assist_status": _status_snapshot(record)})
    except ColonisationApiError as e:
        db.session.rollback()
        return _json_response_error(e.message, e.status_code)
    except Exception as e:
        db.session.rollback()
        logger.exception("Colonisation status post error")
        return _json_response_error(str(e), 500)
