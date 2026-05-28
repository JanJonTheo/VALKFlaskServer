from datetime import datetime
import hashlib
import json
import logging
import os
import re
from typing import Optional

from flask import Blueprint, g, jsonify, request
from sqlalchemy import create_engine, func, text

from models import (
    Activity,
    CommitCrimeEvent,
    Cmdr,
    Event,
    FactionKillBondEvent,
    Faction,
    ManualActivitySubmission,
    MarketBuyEvent,
    MarketSellEvent,
    MissionCompletedEvent,
    MissionCompletedInfluence,
    MissionFailedEvent,
    MultiSellExplorationDataEvent,
    RedeemVoucherEvent,
    SellExplorationDataEvent,
    SyntheticCZ,
    SyntheticGroundCZ,
    System,
    db,
)

manual_activity_bp = Blueprint("manual_activity", __name__)

logger = logging.getLogger(__name__)

TICK_FIELD_ERROR = "Tick fields are not accepted. Current tick is resolved server-side."
NO_TICK_ERROR = "No current tick available. Submit at least one current BGS-Tally event first."
FORBIDDEN_TICK_FIELDS = {"tick", "tickid", "ticktime", "tick_mode"}
SUPPORTED_ACTIVITY_TYPES = {
    "bounty_voucher",
    "combat_bond",
    "exploration_sale",
    "mission_completed",
    "mission_failed",
    "space_cz",
    "ground_cz",
    "scenario",
    "murder_space",
    "murder_ground",
    "black_market_trade",
    "market_buy",
    "market_sell",
}
AMOUNT_REQUIRED = {
    "bounty_voucher",
    "combat_bond",
    "exploration_sale",
    "black_market_trade",
    "market_buy",
    "market_sell",
}
COUNT_DEFAULT_ONE = {
    "mission_failed",
    "space_cz",
    "ground_cz",
    "scenario",
    "murder_space",
    "murder_ground",
}
INF_DEFAULT_ONE = {"mission_completed"}
CZ_TYPES = {"low", "medium", "high"}
SNAPSHOT_DB_URI = "sqlite:///db/bgs_eddn_snapshots.db"


class ManualActivityError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_response_error(message, status_code=400):
    return jsonify({"error": message}), status_code


def _lookup_limit() -> int:
    try:
        value = int(request.args.get("limit", 25))
    except (TypeError, ValueError):
        value = 25
    return max(1, min(value, 25))


def _lookup_query(min_len: int) -> Optional[str]:
    value = (request.args.get("q") or "").strip()
    if len(value) < min_len:
        return None
    return value


def _prefix_sort_key(name: str, query: str):
    lower_name = (name or "").lower()
    lower_query = (query or "").lower()
    return (0 if lower_name.startswith(lower_query) else 1, lower_name)


def _dedupe_systems(rows, query: str, limit: int):
    by_name = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        address = row.get("address")
        if key not in by_name or (not by_name[key].get("address") and address):
            by_name[key] = {"name": name, "address": address}
    values = sorted(by_name.values(), key=lambda item: _prefix_sort_key(item["name"], query))
    return values[:limit]


def _dedupe_name_rows(rows, query: str, limit: int, state_default=None):
    by_name = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in by_name:
            item = {"name": name}
            if state_default is not None:
                item["state"] = row.get("state") or state_default
            by_name[key] = item
        elif state_default is not None and by_name[key].get("state") == state_default and row.get("state"):
            by_name[key]["state"] = row.get("state")
    values = sorted(by_name.values(), key=lambda item: _prefix_sort_key(item["name"], query))
    return values[:limit]


def _eddn_engine():
    eddn_db_uri = os.getenv("SNAPSHOT_DB_URI", SNAPSHOT_DB_URI)
    try:
        connect_args = {"check_same_thread": False, "timeout": 30} if eddn_db_uri.startswith("sqlite") else {}
        return create_engine(eddn_db_uri, connect_args=connect_args)
    except Exception:
        logger.warning("EDDN snapshot database engine could not be created for manual lookup")
        return None


def _auth_and_set_tenant():
    from app import API_VERSION, get_tenant_by_apikey, set_tenant_db_config

    apikey = request.headers.get("apikey")
    tenant = get_tenant_by_apikey(apikey)
    if not tenant:
        logger.warning("Invalid API-Key received for manual activity endpoint")
        return _json_response_error("Unauthorized: Invalid API key", 401)

    g.tenant = tenant
    set_tenant_db_config(tenant)
    if hasattr(g, "tenant_db_error"):
        logger.error("Tenant database error for manual activity endpoint")
        return _json_response_error("Tenant database not found or not reachable", 500)

    api_version = request.headers.get("apiversion")
    if not api_version:
        return _json_response_error("Missing required header: apiversion", 400)
    if not re.match(r"^\d+\.\d+\.\d+$", api_version):
        return _json_response_error("Invalid apiversion format. Expected x.y.z notation", 400)
    if api_version != tenant.get("api_version", API_VERSION):
        logger.warning(
            "Client using different API version for manual activity endpoint: %s (tenant expected: %s)",
            api_version,
            tenant.get("api_version", API_VERSION),
        )
    return None


def _contains_forbidden_tick_field(value) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_TICK_FIELDS:
                return True
            if _contains_forbidden_tick_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_tick_field(item) for item in value)
    return False


def _payload_hash(payload: dict) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _as_dict(value, field_name):
    if not isinstance(value, dict):
        raise ManualActivityError(f"{field_name} must be an object")
    return value


def _trimmed_string(value, field_name, max_len, required=True, default=None):
    if value is None:
        if required:
            raise ManualActivityError(f"{field_name} is required")
        return default
    if not isinstance(value, str):
        raise ManualActivityError(f"{field_name} must be a string")
    value = value.strip()
    if required and not value:
        raise ManualActivityError(f"{field_name} is required")
    if value and len(value) > max_len:
        raise ManualActivityError(f"{field_name} must not exceed {max_len} characters")
    return value or default


def _optional_string(value, field_name, max_len):
    return _trimmed_string(value, field_name, max_len, required=False)


def _parse_int(value, field_name, required=False, default=None, allow_negative=False):
    if value is None:
        if required:
            raise ManualActivityError(f"{field_name} is required")
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManualActivityError(f"{field_name} must be an integer")
    if value < 0 and not allow_negative:
        raise ManualActivityError(f"{field_name} must not be negative")
    return value


def _validate_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ManualActivityError("Request body must be a JSON object")

    submission_id = _trimmed_string(payload.get("submission_id"), "submission_id", 128)
    source = _trimmed_string(payload.get("source"), "source", 64, required=False, default="discord_modal")
    discord = payload.get("discord") or {}
    discord = _as_dict(discord, "discord")
    system = _as_dict(payload.get("system"), "system")
    faction = _as_dict(payload.get("faction"), "faction")
    activity = _as_dict(payload.get("activity"), "activity")

    cmdr = _trimmed_string(payload.get("cmdr"), "cmdr", 64)
    system_name = _trimmed_string(system.get("name"), "system.name", 128)
    faction_name = _trimmed_string(faction.get("name"), "faction.name", 128)
    faction_state = _trimmed_string(faction.get("state"), "faction.state", 64, required=False, default="None")
    activity_type = _trimmed_string(activity.get("type"), "activity.type", 64)
    if activity_type not in SUPPORTED_ACTIVITY_TYPES:
        raise ManualActivityError(f"Unsupported activity.type: {activity_type}")

    correction = bool(activity.get("correction") is True)
    amount = _parse_int(
        activity.get("amount"),
        "activity.amount",
        required=activity_type in AMOUNT_REQUIRED,
        allow_negative=correction,
    )
    count = _parse_int(
        activity.get("count"),
        "activity.count",
        required=False,
        default=1 if activity_type in COUNT_DEFAULT_ONE else None,
        allow_negative=correction,
    )
    influence = _parse_int(
        activity.get("influence"),
        "activity.influence",
        required=False,
        default=1 if activity_type in INF_DEFAULT_ONE else None,
        allow_negative=correction,
    )
    profit = _parse_int(activity.get("profit"), "activity.profit", required=False, allow_negative=correction)

    cz_type = _optional_string(activity.get("cz_type"), "activity.cz_type", 16)
    if activity_type in {"space_cz", "ground_cz"}:
        if not cz_type:
            raise ManualActivityError("activity.cz_type is required")
        cz_type = cz_type.lower()
        if cz_type not in CZ_TYPES:
            raise ManualActivityError("activity.cz_type must be low, medium or high")
    elif cz_type:
        cz_type = cz_type.lower()
        if cz_type not in CZ_TYPES:
            raise ManualActivityError("activity.cz_type must be low, medium or high")

    address = system.get("address")
    if address in (None, ""):
        address = None
    else:
        address = _parse_int(address, "system.address", required=False, allow_negative=False)

    return {
        "submission_id": submission_id,
        "source": source,
        "discord": {
            "guild_id": _optional_string(discord.get("guild_id"), "discord.guild_id", 64),
            "channel_id": _optional_string(discord.get("channel_id"), "discord.channel_id", 64),
            "user_id": _optional_string(discord.get("user_id"), "discord.user_id", 64),
            "message_id": _optional_string(discord.get("message_id"), "discord.message_id", 64),
            "interaction_id": _optional_string(discord.get("interaction_id"), "discord.interaction_id", 64),
        },
        "cmdr": cmdr,
        "client_timestamp": _optional_string(payload.get("client_timestamp"), "client_timestamp", 64),
        "system_name": system_name,
        "system_address": address,
        "faction_name": faction_name,
        "faction_state": faction_state,
        "activity_type": activity_type,
        "amount": amount,
        "count": count,
        "influence": influence,
        "profit": profit,
        "cz_type": cz_type,
        "settlement": _optional_string(activity.get("settlement"), "activity.settlement", 128),
        "correction": correction,
        "note": _optional_string(payload.get("note"), "note", 1024),
    }


def _get_current_tick():
    row = db.session.execute(text(
        "SELECT tickid, ticktime FROM event "
        "WHERE tickid IS NOT NULL AND tickid != '' "
        "ORDER BY timestamp DESC LIMIT 1"
    )).fetchone()
    if not row:
        row = db.session.execute(text(
            "SELECT tickid, ticktime FROM activity "
            "WHERE tickid IS NOT NULL AND tickid != '' "
            "ORDER BY timestamp DESC LIMIT 1"
        )).fetchone()
    if not row:
        raise ManualActivityError(NO_TICK_ERROR, 409)
    return row[0], row[1]


def _resolve_system_address(system_name: str, supplied_address):
    if supplied_address:
        return supplied_address
    row = db.session.execute(
        text(
            "SELECT systemaddress FROM event "
            "WHERE lower(starsystem) = lower(:system_name) "
            "AND systemaddress IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1"
        ),
        {"system_name": system_name},
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = db.session.execute(
        text(
            "SELECT address FROM system "
            "WHERE lower(name) = lower(:system_name) "
            "AND address IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"system_name": system_name},
    ).fetchone()
    if row and row[0]:
        return row[0]
    raise ManualActivityError("SystemAddress could not be resolved. Please provide system address.", 400)


def _add_int(current, delta):
    return (current or 0) + (delta or 0)


def _subtract_int(current, delta):
    return max(0, (current or 0) - (delta or 0))


def _load_json_obj(value, default):
    if not value:
        return json.loads(json.dumps(default))
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed if isinstance(parsed, type(default)) else json.loads(json.dumps(default))
    except Exception:
        return json.loads(json.dumps(default))


def _dump_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _level_code(cz_type):
    return {"low": "L", "medium": "M", "high": "H"}.get(cz_type, "")


def _update_activity_tree(data, tickid, ticktime, captured_at):
    activity = db.session.query(Activity).filter_by(tickid=tickid, cmdr=data["cmdr"]).first()
    if not activity:
        activity = Activity(tickid=tickid, ticktime=ticktime, timestamp=captured_at, cmdr=data["cmdr"])
        db.session.add(activity)
        db.session.flush()
    else:
        activity.ticktime = ticktime
        activity.timestamp = captured_at

    system = db.session.query(System).filter_by(
        activity_id=activity.id,
        name=data["system_name"],
        address=data["system_address"],
    ).first()
    if not system:
        system = System(name=data["system_name"], address=data["system_address"], activity_id=activity.id)
        db.session.add(system)
        db.session.flush()

    faction = db.session.query(Faction).filter_by(system_id=system.id, name=data["faction_name"]).first()
    if not faction:
        faction = Faction(
            name=data["faction_name"],
            state=data["faction_state"] or "None",
            bvs=0,
            cbs=0,
            exobiology=0,
            exploration=0,
            scenarios=0,
            infprimary=0,
            infsecondary=0,
            missionfails=0,
            murdersground=0,
            murdersspace=0,
            tradebm=0,
            system_id=system.id,
        )
        db.session.add(faction)
        db.session.flush()

    activity_type = data["activity_type"]
    amount = data["amount"]
    count = data["count"]
    influence = data["influence"]

    if activity_type == "bounty_voucher":
        faction.bvs = _add_int(faction.bvs, amount)
    elif activity_type == "combat_bond":
        faction.cbs = _add_int(faction.cbs, amount)
    elif activity_type == "exploration_sale":
        faction.exploration = _add_int(faction.exploration, amount)
    elif activity_type == "mission_completed":
        faction.infprimary = _add_int(faction.infprimary, influence)
    elif activity_type == "mission_failed":
        faction.missionfails = _add_int(faction.missionfails, count)
    elif activity_type == "space_cz":
        czspace = _load_json_obj(faction.czspace, {"low": 0, "medium": 0, "high": 0})
        czspace.setdefault("low", 0)
        czspace.setdefault("medium", 0)
        czspace.setdefault("high", 0)
        czspace[data["cz_type"]] = _add_int(czspace.get(data["cz_type"]), count)
        faction.czspace = _dump_json(czspace)
    elif activity_type == "ground_cz":
        czground = _load_json_obj(
            faction.czground,
            {"low": 0, "medium": 0, "high": 0, "settlements": []},
        )
        czground.setdefault("low", 0)
        czground.setdefault("medium", 0)
        czground.setdefault("high", 0)
        czground.setdefault("settlements", [])
        czground[data["cz_type"]] = _add_int(czground.get(data["cz_type"]), count)
        settlement = data.get("settlement")
        if settlement:
            entry_type = _level_code(data["cz_type"]).lower()
            existing = None
            for entry in czground["settlements"]:
                if entry.get("name") == settlement and entry.get("type") == entry_type:
                    existing = entry
                    break
            if existing:
                existing["count"] = _add_int(existing.get("count"), count)
            else:
                czground["settlements"].append({"name": settlement, "type": entry_type, "count": count})
        faction.czground = _dump_json(czground)
    elif activity_type == "scenario":
        faction.scenarios = _add_int(faction.scenarios, count)
    elif activity_type == "murder_space":
        faction.murdersspace = _add_int(faction.murdersspace, count)
    elif activity_type == "murder_ground":
        faction.murdersground = _add_int(faction.murdersground, count)
    elif activity_type == "black_market_trade":
        faction.tradebm = _add_int(faction.tradebm, amount)
    elif activity_type == "market_buy":
        tradebuy = _load_json_obj(
            faction.tradebuy,
            {"high": {"items": 0, "value": 0}, "low": {"items": 0, "value": 0}, "zero": {"items": 0, "value": 0}},
        )
        tradebuy.setdefault("high", {"items": 0, "value": 0})
        tradebuy["high"]["items"] = _add_int(tradebuy["high"].get("items"), count or 0)
        tradebuy["high"]["value"] = _add_int(tradebuy["high"].get("value"), amount)
        faction.tradebuy = _dump_json(tradebuy)
    elif activity_type == "market_sell":
        tradesell = _load_json_obj(
            faction.tradesell,
            {
                "high": {"items": 0, "value": 0, "profit": 0},
                "low": {"items": 0, "value": 0, "profit": 0},
                "zero": {"items": 0, "value": 0, "profit": 0},
            },
        )
        tradesell.setdefault("high", {"items": 0, "value": 0, "profit": 0})
        tradesell["high"]["items"] = _add_int(tradesell["high"].get("items"), count or 0)
        tradesell["high"]["value"] = _add_int(tradesell["high"].get("value"), amount)
        tradesell["high"]["profit"] = _add_int(tradesell["high"].get("profit"), data.get("profit") if data.get("profit") is not None else amount)
        faction.tradesell = _dump_json(tradesell)

    return activity


def _submission_to_activity_data(submission: ManualActivitySubmission) -> dict:
    profit = None
    try:
        payload = json.loads(submission.payload_json or "{}")
        profit = ((payload.get("activity") or {}).get("profit"))
    except Exception:
        profit = None
    return {
        "submission_id": submission.submission_id,
        "source": submission.source,
        "discord": {
            "guild_id": submission.discord_guild_id,
            "channel_id": submission.discord_channel_id,
            "user_id": submission.discord_user_id,
            "message_id": submission.discord_message_id,
            "interaction_id": submission.discord_interaction_id,
        },
        "cmdr": submission.cmdr,
        "system_name": submission.system_name,
        "system_address": submission.system_address,
        "faction_name": submission.faction_name,
        "faction_state": submission.faction_state,
        "activity_type": submission.activity_type,
        "amount": submission.amount,
        "count": submission.count,
        "influence": submission.influence,
        "profit": profit,
        "cz_type": submission.cz_type,
        "settlement": submission.settlement,
        "note": submission.note,
    }


def _reverse_activity_tree(submission: ManualActivitySubmission):
    activity = None
    if submission.activity_id:
        activity = db.session.query(Activity).filter_by(id=submission.activity_id).first()
    if not activity:
        activity = db.session.query(Activity).filter_by(tickid=submission.tickid, cmdr=submission.cmdr).first()
    if not activity:
        return

    system = db.session.query(System).filter_by(
        activity_id=activity.id,
        name=submission.system_name,
        address=submission.system_address,
    ).first()
    if not system:
        return

    faction = db.session.query(Faction).filter_by(system_id=system.id, name=submission.faction_name).first()
    if not faction:
        return

    activity_type = submission.activity_type
    amount = submission.amount or 0
    count = submission.count or 0
    influence = submission.influence or 0

    if activity_type == "bounty_voucher":
        faction.bvs = _subtract_int(faction.bvs, amount)
    elif activity_type == "combat_bond":
        faction.cbs = _subtract_int(faction.cbs, amount)
    elif activity_type == "exploration_sale":
        faction.exploration = _subtract_int(faction.exploration, amount)
    elif activity_type == "mission_completed":
        faction.infprimary = _subtract_int(faction.infprimary, influence)
    elif activity_type == "mission_failed":
        faction.missionfails = _subtract_int(faction.missionfails, count)
    elif activity_type == "space_cz":
        czspace = _load_json_obj(faction.czspace, {"low": 0, "medium": 0, "high": 0})
        for key in ("low", "medium", "high"):
            czspace.setdefault(key, 0)
        if submission.cz_type:
            czspace[submission.cz_type] = _subtract_int(czspace.get(submission.cz_type), count)
        faction.czspace = _dump_json(czspace)
    elif activity_type == "ground_cz":
        czground = _load_json_obj(
            faction.czground,
            {"low": 0, "medium": 0, "high": 0, "settlements": []},
        )
        for key in ("low", "medium", "high"):
            czground.setdefault(key, 0)
        czground.setdefault("settlements", [])
        if submission.cz_type:
            czground[submission.cz_type] = _subtract_int(czground.get(submission.cz_type), count)
        if submission.settlement and submission.cz_type:
            entry_type = _level_code(submission.cz_type).lower()
            kept = []
            for entry in czground["settlements"]:
                if entry.get("name") == submission.settlement and entry.get("type") == entry_type:
                    entry["count"] = _subtract_int(entry.get("count"), count)
                    if entry["count"] > 0:
                        kept.append(entry)
                else:
                    kept.append(entry)
            czground["settlements"] = kept
        faction.czground = _dump_json(czground)
    elif activity_type == "scenario":
        faction.scenarios = _subtract_int(faction.scenarios, count)
    elif activity_type == "murder_space":
        faction.murdersspace = _subtract_int(faction.murdersspace, count)
    elif activity_type == "murder_ground":
        faction.murdersground = _subtract_int(faction.murdersground, count)
    elif activity_type == "black_market_trade":
        faction.tradebm = _subtract_int(faction.tradebm, amount)
    elif activity_type == "market_buy":
        tradebuy = _load_json_obj(
            faction.tradebuy,
            {"high": {"items": 0, "value": 0}, "low": {"items": 0, "value": 0}, "zero": {"items": 0, "value": 0}},
        )
        tradebuy.setdefault("high", {"items": 0, "value": 0})
        tradebuy["high"]["items"] = _subtract_int(tradebuy["high"].get("items"), count)
        tradebuy["high"]["value"] = _subtract_int(tradebuy["high"].get("value"), amount)
        faction.tradebuy = _dump_json(tradebuy)
    elif activity_type == "market_sell":
        data = _submission_to_activity_data(submission)
        profit = data.get("profit") if data.get("profit") is not None else amount
        tradesell = _load_json_obj(
            faction.tradesell,
            {
                "high": {"items": 0, "value": 0, "profit": 0},
                "low": {"items": 0, "value": 0, "profit": 0},
                "zero": {"items": 0, "value": 0, "profit": 0},
            },
        )
        tradesell.setdefault("high", {"items": 0, "value": 0, "profit": 0})
        tradesell["high"]["items"] = _subtract_int(tradesell["high"].get("items"), count)
        tradesell["high"]["value"] = _subtract_int(tradesell["high"].get("value"), amount)
        tradesell["high"]["profit"] = _subtract_int(tradesell["high"].get("profit"), profit)
        faction.tradesell = _dump_json(tradesell)


def _raw_json_payload(data, captured_at):
    return {
        "ManualSubmissionId": data["submission_id"],
        "Source": data["source"],
        "Discord": data["discord"],
        "Form": {
            "cmdr": data["cmdr"],
            "client_timestamp": data.get("client_timestamp"),
            "system": {"name": data["system_name"], "address": data["system_address"]},
            "faction": {"name": data["faction_name"], "state": data["faction_state"]},
            "activity": {
                "type": data["activity_type"],
                "amount": data.get("amount"),
                "count": data.get("count"),
                "influence": data.get("influence"),
                "profit": data.get("profit"),
                "cz_type": data.get("cz_type"),
                "settlement": data.get("settlement"),
                "correction": data.get("correction"),
            },
            "note": data.get("note"),
        },
        "event": data["activity_type"],
        "cmdr": data["cmdr"],
        "StarSystem": data["system_name"],
        "SystemAddress": data["system_address"],
        "timestamp": captured_at,
    }


def _create_base_event(data, captured_at, tickid, ticktime, event_name, raw_extra=None):
    raw = _raw_json_payload(data, captured_at)
    if raw_extra:
        raw.update(raw_extra)
    event = Event(
        event=event_name,
        timestamp=captured_at,
        tickid=tickid,
        ticktime=ticktime,
        cmdr=data["cmdr"],
        starsystem=data["system_name"],
        systemaddress=data["system_address"],
        raw_json=json.dumps(raw, ensure_ascii=False, sort_keys=True),
    )
    db.session.add(event)
    db.session.flush()
    return event


def _influence_string(value):
    if value is None:
        return ""
    if value > 0:
        return "+" * value
    if value < 0:
        return "-" * abs(value)
    return ""


def _influence_trend(value):
    if value is None or value == 0:
        return "None"
    return "UpGood" if value > 0 else "DownBad"


def _create_events(data, captured_at, tickid, ticktime):
    activity_type = data["activity_type"]
    event_ids = []
    amount = data.get("amount")
    count = data.get("count") or 0
    influence = data.get("influence")
    faction_name = data["faction_name"]

    def add_id(event):
        event_ids.append(event.id)
        return event

    if activity_type == "bounty_voucher":
        event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "RedeemVoucher", {"Amount": amount, "Faction": faction_name, "Type": "bounty"}))
        db.session.add(RedeemVoucherEvent(event_id=event.id, amount=amount, faction=faction_name, type="bounty", starsystem=data["system_name"], systemaddress=data["system_address"]))
    elif activity_type == "combat_bond":
        event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "RedeemVoucher", {"Amount": amount, "Faction": faction_name, "Type": "CombatBond"}))
        db.session.add(RedeemVoucherEvent(event_id=event.id, amount=amount, faction=faction_name, type="CombatBond", starsystem=data["system_name"], systemaddress=data["system_address"]))
    elif activity_type == "exploration_sale":
        event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "SellExplorationData", {"TotalEarnings": amount}))
        db.session.add(SellExplorationDataEvent(event_id=event.id, earnings=amount, starsystem=data["system_name"], systemaddress=data["system_address"]))
    elif activity_type == "mission_completed":
        reward = amount or 0
        event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "MissionCompleted", {"Reward": reward, "Faction": faction_name, "Name": "Manual Discord Activity"}))
        db.session.add(MissionCompletedEvent(
            event_id=event.id,
            mission_id=event.id,
            name="Manual Discord Activity",
            mission_name="Manual Discord Activity",
            reward=reward,
            faction=faction_name,
            awarding_faction=faction_name,
        ))
        db.session.add(MissionCompletedInfluence(
            mission_id=event.id,
            system=str(data["system_address"]),
            influence=_influence_string(influence),
            trend=_influence_trend(influence),
            faction_name=faction_name,
        ))
    elif activity_type == "mission_failed":
        for _ in range(abs(count)):
            event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "MissionFailed", {"Fine": amount or 0, "Faction": faction_name, "Name": "Manual Discord Activity"}))
            db.session.add(MissionFailedEvent(event_id=event.id, awarding_faction=faction_name, mission_name="Manual Discord Activity", fine=amount or 0))
    elif activity_type == "space_cz":
        raw_cz = {"low": 0, "medium": 0, "high": 0}
        raw_cz[data["cz_type"]] = 1
        for _ in range(abs(count)):
            event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "SyntheticCZ", raw_cz))
            db.session.add(SyntheticCZ(event_id=event.id, cz_type=data["cz_type"], faction=faction_name, cmdr=data["cmdr"]))
    elif activity_type == "ground_cz":
        raw_cz = {"low": 0, "medium": 0, "high": 0}
        raw_cz[data["cz_type"]] = 1
        if data.get("settlement"):
            raw_cz["settlement"] = data["settlement"]
        for _ in range(abs(count)):
            event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "SyntheticGroundCZ", raw_cz))
            db.session.add(SyntheticGroundCZ(event_id=event.id, cz_type=data["cz_type"], settlement=data.get("settlement"), faction=faction_name, cmdr=data["cmdr"]))
    elif activity_type == "scenario":
        add_id(_create_base_event(data, captured_at, tickid, ticktime, "SyntheticScenario", {"Count": count}))
    elif activity_type == "murder_space":
        for _ in range(abs(count)):
            event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "CommitCrime", {"CrimeType": "murder", "Faction": faction_name, "Bounty": amount or 0}))
            db.session.add(CommitCrimeEvent(event_id=event.id, crime_type="murder", faction=faction_name, bounty=amount or 0))
    elif activity_type == "murder_ground":
        for _ in range(abs(count)):
            event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "CommitCrime", {"CrimeType": "onFoot_murder", "Faction": faction_name, "Bounty": amount or 0}))
            db.session.add(CommitCrimeEvent(event_id=event.id, crime_type="onFoot_murder", faction=faction_name, bounty=amount or 0))
    elif activity_type == "black_market_trade":
        event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "MarketSell", {"BlackMarket": True, "TotalSale": amount}))
        db.session.add(MarketSellEvent(event_id=event.id, value=amount, profit=amount, count=count or 0, total_sale=amount, starsystem=data["system_name"], systemaddress=data["system_address"]))
    elif activity_type == "market_buy":
        event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "MarketBuy", {"TotalCost": amount}))
        db.session.add(MarketBuyEvent(event_id=event.id, value=amount, count=count or 0, total_cost=amount, starsystem=data["system_name"], systemaddress=data["system_address"]))
    elif activity_type == "market_sell":
        profit = data.get("profit") if data.get("profit") is not None else amount
        event = add_id(_create_base_event(data, captured_at, tickid, ticktime, "MarketSell", {"TotalSale": amount}))
        db.session.add(MarketSellEvent(event_id=event.id, value=amount, profit=profit, count=count or 0, total_sale=amount, starsystem=data["system_name"], systemaddress=data["system_address"]))

    return event_ids


def _safe_discord_text(value: str, max_len: int = 128) -> str:
    if value is None:
        return ""
    value = str(value).strip().replace("`", "")
    value = value.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    value = re.sub(r"<@([!&]?\d+)>", lambda match: f"<@\u200b{match.group(1)}>", value)
    value = re.sub(r"<#(\d+)>", lambda match: f"<#\u200b{match.group(1)}>", value)
    if len(value) > max_len:
        return value[: max_len - 1] + "..."
    return value


def _ansi_color(text_value, color_code, bold=False):
    prefix = "1;" if bold else ""
    return f"\u001b[{prefix}{color_code}m{text_value}\u001b[0m"


def _human_format(value):
    if value is None:
        return "0"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        formatted = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{formatted}M"
    if value >= 1_000:
        formatted = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{formatted}K"
    return f"{sign}{value}"


def _build_manual_activity_field_value(
    *,
    cmdr: str,
    faction_name: str,
    activity_type: str,
    amount: Optional[int],
    count: Optional[int],
    influence: Optional[int],
    cz_type: Optional[str],
    settlement: Optional[str],
) -> str:
    safe_cmdr = _safe_discord_text(cmdr, 64)
    safe_faction = _safe_discord_text(faction_name, 128)
    safe_settlement = _safe_discord_text(settlement, 128)
    faction_part = _ansi_color(safe_faction, "33", bold=True)
    value_part = _ansi_color(_human_format(amount), "32")
    count_part = _ansi_color(str(count or 0), "32")
    influence_part = _ansi_color(("+" if (influence or 0) > 0 else "") + str(influence or 0), "34")
    red_labels = {"BVs", "CBs", "Murders", "GroundMurders", "Fails"}
    trade_labels = {"TrdBMProfit", "TrdBuy", "TrdProfit"}

    def label(text_label):
        if text_label in red_labels:
            return _ansi_color(text_label, "31")
        if text_label in trade_labels:
            return _ansi_color(text_label, "36")
        if text_label == "INF":
            return _ansi_color(text_label, "34")
        return text_label

    lines = []
    if activity_type == "bounty_voucher":
        lines.append(f"{faction_part} {label('BVs')} {value_part}")
    elif activity_type == "combat_bond":
        lines.append(f"{faction_part} {label('CBs')} {value_part}")
    elif activity_type == "exploration_sale":
        lines.append(f"{faction_part} Expl {value_part}")
    elif activity_type == "mission_completed":
        lines.append(f"{faction_part} {label('INF')} {influence_part}")
    elif activity_type == "mission_failed":
        lines.append(f"{faction_part} {label('Fails')} {count_part}")
    elif activity_type == "space_cz":
        lines.append(f"{faction_part} SpaceCZs {_level_code(cz_type)} x {count_part}")
    elif activity_type == "ground_cz":
        lines.append(f"{faction_part} GroundCZs {_level_code(cz_type)} x {count_part}")
        if safe_settlement:
            lines.append(f"  \u2694\ufe0f {safe_settlement} x {count_part}")
    elif activity_type == "scenario":
        lines.append(f"{faction_part} Scenarios {count_part}")
    elif activity_type == "murder_space":
        lines.append(f"{faction_part} {label('Murders')} {count_part}")
    elif activity_type == "murder_ground":
        lines.append(f"{faction_part} {label('GroundMurders')} {count_part}")
    elif activity_type == "black_market_trade":
        lines.append(f"{faction_part} {label('TrdBMProfit')} {value_part}")
    elif activity_type == "market_buy":
        lines.append(f"{faction_part} {label('TrdBuy')} {value_part}")
    elif activity_type == "market_sell":
        lines.append(f"{faction_part} {label('TrdProfit')} {value_part}")
    return "```ansi\n" + "\n".join(lines)[:1000] + "\n```"


def _manual_activity_summary_line(
    *,
    faction_name: str,
    activity_type: str,
    amount: Optional[int],
    count: Optional[int],
    influence: Optional[int],
    cz_type: Optional[str],
    settlement: Optional[str],
) -> list:
    safe_faction = _safe_discord_text(faction_name, 128)
    safe_settlement = _safe_discord_text(settlement, 128)
    faction_part = _ansi_color(safe_faction, "33", bold=True)
    value_part = _ansi_color(_human_format(amount), "32")
    count_part = _ansi_color(str(count or 0), "32")
    influence_part = _ansi_color(("+" if (influence or 0) > 0 else "") + str(influence or 0), "34")

    def red(text_label):
        return _ansi_color(text_label, "31")

    def cyan(text_label):
        return _ansi_color(text_label, "36")

    if activity_type == "bounty_voucher":
        return [f"{faction_part} {red('BVs')} {value_part}"]
    if activity_type == "combat_bond":
        return [f"{faction_part} {red('CBs')} {value_part}"]
    if activity_type == "exploration_sale":
        return [f"{faction_part} Expl {value_part}"]
    if activity_type == "mission_completed":
        return [f"{faction_part} {_ansi_color('INF', '34')} {influence_part}"]
    if activity_type == "mission_failed":
        return [f"{faction_part} {red('Fails')} {count_part}"]
    if activity_type == "space_cz":
        return [f"{faction_part} SpaceCZs {_level_code(cz_type)} x {count_part}"]
    if activity_type == "ground_cz":
        lines = [f"{faction_part} GroundCZs {_level_code(cz_type)} x {count_part}"]
        if safe_settlement:
            lines.append(f"  \u2694\ufe0f {safe_settlement} x {count_part}")
        return lines
    if activity_type == "scenario":
        return [f"{faction_part} Scenarios {count_part}"]
    if activity_type == "murder_space":
        return [f"{faction_part} {red('Murders')} {count_part}"]
    if activity_type == "murder_ground":
        return [f"{faction_part} {red('GroundMurders')} {count_part}"]
    if activity_type == "black_market_trade":
        return [f"{faction_part} {cyan('TrdBMProfit')} {value_part}"]
    if activity_type == "market_buy":
        return [f"{faction_part} {cyan('TrdBuy')} {value_part}"]
    if activity_type == "market_sell":
        return [f"{faction_part} {cyan('TrdProfit')} {value_part}"]
    return [f"{faction_part} {activity_type}"]


def _build_grouped_system_field_value(*, cmdr: str, items: list) -> str:
    lines = []
    for item in items:
        lines.extend(_manual_activity_summary_line(
            faction_name=item["faction_name"],
            activity_type=item["activity_type"],
            amount=item.get("amount"),
            count=item.get("count"),
            influence=item.get("influence"),
            cz_type=item.get("cz_type"),
            settlement=item.get("settlement"),
        ))
    return "```ansi\n" + "\n".join(lines)[:1000] + "\n```"


def _format_ticktime(ticktime: str) -> str:
    raw = (ticktime or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return raw[:64]


def _build_bgs_tally_style_embed(
    *,
    cmdr: str,
    ticktime: str,
    captured_at: str,
    system_name: str,
    faction_name: str,
    activity_type: str,
    amount: Optional[int],
    count: Optional[int],
    influence: Optional[int],
    cz_type: Optional[str],
    settlement: Optional[str],
    note: Optional[str],
) -> dict:
    safe_cmdr = _safe_discord_text(cmdr, 64)
    description = f"Manual Discord entry via VALKBot\nCMDR: {safe_cmdr}"
    safe_note = _safe_discord_text(note, 512)
    if safe_note:
        description += f"\nNote: {safe_note}"
    return {
        "color": 10682531,
        "author": {
            "name": "Manual Discord Input",
            "icon_url": "https://raw.githubusercontent.com/wiki/aussig/BGS-Tally/images/logo-square-white.png",
            "url": "https://github.com/aussig/BGS-Tally/wiki",
        },
        "title": _safe_discord_text(f"BGS Activity after Tick: {_format_ticktime(ticktime)} (game)", 256),
        "description": description[:4096],
        "fields": [
            {
                "name": _safe_discord_text(system_name, 256),
                "value": _build_manual_activity_field_value(
                    cmdr=cmdr,
                    faction_name=faction_name,
                    activity_type=activity_type,
                    amount=amount,
                    count=count,
                    influence=influence,
                    cz_type=cz_type,
                    settlement=settlement,
                )[:1024],
                "inline": False,
            }
        ],
        "footer": {
            "text": _safe_discord_text(f"Posted at {captured_at} (game) | Manual VALKBot", 256),
            "icon_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/84/Fxemoji_u1F556.svg/240px-Fxemoji_u1F556.svg.png",
        },
    }


def _submission_to_webhook_data(submission: ManualActivitySubmission) -> dict:
    return {
        "cmdr": submission.cmdr,
        "system_name": submission.system_name,
        "faction_name": submission.faction_name,
        "activity_type": submission.activity_type,
        "amount": submission.amount,
        "count": submission.count,
        "influence": submission.influence,
        "cz_type": submission.cz_type,
        "settlement": submission.settlement,
        "note": submission.note,
    }


def _build_bgs_tally_style_grouped_embed(*, submissions, ticktime: str, captured_at: str) -> dict:
    latest = submissions[-1]
    safe_cmdr = _safe_discord_text(latest["cmdr"], 64)
    description = f"Manual Discord entries via VALKBot\nCMDR: {safe_cmdr}"
    notes = []
    for item in submissions:
        safe_note = _safe_discord_text(item.get("note"), 180)
        if safe_note and safe_note not in notes:
            notes.append(safe_note)
    if notes:
        description += "\nNotes: " + " | ".join(notes)[:3500]

    grouped_by_system = []
    by_system = {}
    for item in submissions:
        system_key = (item["system_name"] or "").strip().lower()
        if system_key not in by_system:
            bucket = {"system_name": item["system_name"], "items": []}
            by_system[system_key] = bucket
            grouped_by_system.append(bucket)
        by_system[system_key]["items"].append(item)

    fields = []
    for group in grouped_by_system[-25:]:
        fields.append({
            "name": _safe_discord_text(group["system_name"], 256),
            "value": _build_grouped_system_field_value(cmdr=safe_cmdr, items=group["items"])[:1024],
            "inline": False,
        })

    return {
        "color": 10682531,
        "author": {
            "name": "Manual Discord Input",
            "icon_url": "https://raw.githubusercontent.com/wiki/aussig/BGS-Tally/images/logo-square-white.png",
            "url": "https://github.com/aussig/BGS-Tally/wiki",
        },
        "title": _safe_discord_text(f"BGS Activity after Tick: {_format_ticktime(ticktime)} (game)", 256),
        "description": description[:4096],
        "fields": fields,
        "footer": {
            "text": _safe_discord_text(f"Updated at {captured_at} (game) | Manual VALKBot", 256),
            "icon_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/84/Fxemoji_u1F556.svg/240px-Fxemoji_u1F556.svg.png",
        },
    }


def _get_manual_activity_webhook_config():
    tenant = getattr(g, "tenant", None) or {}
    webhook_settings = tenant.get("manual_activity_webhook") or {}
    discord_webhooks = tenant.get("discord_webhooks") or {}

    enabled_value = webhook_settings.get("enabled", tenant.get("manual_activity_webhook_enabled", True))
    if isinstance(enabled_value, str):
        enabled = enabled_value.strip().lower() not in {"0", "false", "no", "off"}
    else:
        enabled = bool(enabled_value)

    timeout_raw = webhook_settings.get(
        "timeout_seconds",
        tenant.get("manual_activity_webhook_timeout_seconds", 10),
    )
    try:
        timeout_seconds = max(1, int(timeout_raw))
    except ValueError:
        timeout_seconds = 10

    webhook_url = (
        webhook_settings.get("url")
        or tenant.get("manual_activity_webhook_url")
        or discord_webhooks.get("manual_activity")
        or ""
    )
    username = (
        webhook_settings.get("username")
        or tenant.get("manual_activity_webhook_username")
        or "BGS-Tally Manual"
    )
    avatar_url = (
        webhook_settings.get("avatar_url")
        or tenant.get("manual_activity_webhook_avatar_url")
        or ""
    )

    return {
        "enabled": enabled,
        "url": str(webhook_url).strip(),
        "username": str(username).strip() or "BGS-Tally Manual",
        "avatar_url": str(avatar_url).strip(),
        "timeout_seconds": timeout_seconds,
    }


def _truncate_discord_payload(payload: dict) -> dict:
    payload["content"] = (payload.get("content") or "")[:2000]
    for embed in payload.get("embeds", []):
        if "title" in embed:
            embed["title"] = embed["title"][:256]
        if "description" in embed:
            embed["description"] = embed["description"][:4096]
        embed["fields"] = embed.get("fields", [])[:25]
        for field in embed["fields"]:
            field["name"] = field.get("name", "")[:256]
            field["value"] = field.get("value", "")[:1024]
    return payload


def _safe_error_text(value, max_len=300):
    if value is None:
        return ""
    return _safe_discord_text(str(value), max_len)


def _post_manual_activity_webhook(data, ticktime, captured_at):
    config = _get_manual_activity_webhook_config()
    if not config["enabled"]:
        return {"status": "disabled", "message_id": None, "error": None}
    if not config["url"]:
        return {"status": "not_configured", "message_id": None, "error": None}

    embed = _build_bgs_tally_style_embed(
        cmdr=data["cmdr"],
        ticktime=ticktime,
        captured_at=captured_at,
        system_name=data["system_name"],
        faction_name=data["faction_name"],
        activity_type=data["activity_type"],
        amount=data.get("amount"),
        count=data.get("count"),
        influence=data.get("influence"),
        cz_type=data.get("cz_type"),
        settlement=data.get("settlement"),
        note=data.get("note"),
    )
    payload = {
        "content": "",
        "username": _safe_discord_text(data["cmdr"], 80),
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }
    if config["avatar_url"]:
        payload["avatar_url"] = config["avatar_url"]
    payload = _truncate_discord_payload(payload)

    try:
        import requests as http_requests

        resp = http_requests.post(
            config["url"],
            params={"wait": "true"},
            json=payload,
            timeout=config["timeout_seconds"],
        )
        if resp.status_code in (200, 204):
            message_id = None
            if resp.status_code == 200:
                try:
                    message_id = (resp.json() or {}).get("id")
                except Exception:
                    message_id = None
            return {"status": "posted", "message_id": message_id, "error": None}
        return {
            "status": "failed",
            "message_id": None,
            "error": f"Discord webhook returned HTTP {resp.status_code}",
        }
    except Exception as exc:
        logger.warning("Manual activity Discord webhook failed: %s", _safe_error_text(exc))
        return {"status": "failed", "message_id": None, "error": _safe_error_text(exc)}


def _post_manual_activity_payload(payload: dict, config: dict):
    import requests as http_requests

    resp = http_requests.post(
        config["url"],
        params={"wait": "true"},
        json=_truncate_discord_payload(payload),
        timeout=config["timeout_seconds"],
    )
    if resp.status_code in (200, 204):
        message_id = None
        if resp.status_code == 200:
            try:
                message_id = (resp.json() or {}).get("id")
            except Exception:
                message_id = None
        return {"status": "posted", "message_id": message_id, "error": None}
    return {
        "status": "failed",
        "message_id": None,
        "error": f"Discord webhook returned HTTP {resp.status_code}",
    }


def _find_previous_manual_activity_message(submission: ManualActivitySubmission):
    return db.session.query(ManualActivitySubmission).filter(
        ManualActivitySubmission.id != submission.id,
        ManualActivitySubmission.tickid == submission.tickid,
        ManualActivitySubmission.cmdr == submission.cmdr,
        ManualActivitySubmission.status == "saved",
        ManualActivitySubmission.webhook_status == "posted",
        ManualActivitySubmission.webhook_message_id.isnot(None),
        ManualActivitySubmission.webhook_message_id != "",
    ).order_by(ManualActivitySubmission.id.desc()).first()


def _grouped_submissions_for_message(message_id: str, current_submission: ManualActivitySubmission):
    rows = db.session.query(ManualActivitySubmission).filter(
        ManualActivitySubmission.webhook_message_id == message_id,
        ManualActivitySubmission.tickid == current_submission.tickid,
        ManualActivitySubmission.cmdr == current_submission.cmdr,
        ManualActivitySubmission.status == "saved",
    ).order_by(ManualActivitySubmission.id.asc()).all()
    rows = [row for row in rows if row.id != current_submission.id]
    rows.append(current_submission)
    return rows


def _send_or_update_manual_activity_webhook(submission: ManualActivitySubmission):
    config = _get_manual_activity_webhook_config()
    if not config["enabled"]:
        return {"status": "disabled", "message_id": None, "error": None}
    if not config["url"]:
        return {"status": "not_configured", "message_id": None, "error": None}

    try:
        import requests as http_requests

        previous = _find_previous_manual_activity_message(submission)
        if previous:
            grouped = [_submission_to_webhook_data(row) for row in _grouped_submissions_for_message(previous.webhook_message_id, submission)]
            embed = _build_bgs_tally_style_grouped_embed(
                submissions=grouped,
                ticktime=submission.ticktime,
                captured_at=_utc_now(),
            )
            payload = {
                "content": "",
                "username": _safe_discord_text(submission.cmdr, 80),
                "allowed_mentions": {"parse": []},
                "embeds": [embed],
            }
            if config["avatar_url"]:
                payload["avatar_url"] = config["avatar_url"]
            resp = http_requests.patch(
                f"{config['url'].rstrip('/')}/messages/{previous.webhook_message_id}",
                json=_truncate_discord_payload(payload),
                timeout=config["timeout_seconds"],
            )
            if resp.status_code in (200, 204):
                return {"status": "posted", "message_id": previous.webhook_message_id, "error": None}
            if resp.status_code == 404:
                logger.warning("Manual activity Discord webhook message was not found; posting a new grouped message")
                return _post_manual_activity_payload(payload, config)
            return {
                "status": "failed",
                "message_id": previous.webhook_message_id,
                "error": f"Discord webhook edit returned HTTP {resp.status_code}",
            }

        return _post_manual_activity_webhook(
            _submission_to_webhook_data(submission),
            submission.ticktime,
            submission.captured_at,
        )
    except Exception as exc:
        logger.warning("Manual activity Discord webhook send/update failed: %s", _safe_error_text(exc))
        return {"status": "failed", "message_id": None, "error": _safe_error_text(exc)}


def _delete_or_update_manual_activity_webhook(message_id: Optional[str], tickid: str, cmdr: str):
    if not message_id:
        return {"status": "skipped", "message_id": None, "error": None}

    config = _get_manual_activity_webhook_config()
    if not config["enabled"]:
        return {"status": "disabled", "message_id": message_id, "error": None}
    if not config["url"]:
        return {"status": "not_configured", "message_id": message_id, "error": None}

    rows = db.session.query(ManualActivitySubmission).filter(
        ManualActivitySubmission.webhook_message_id == message_id,
        ManualActivitySubmission.tickid == tickid,
        ManualActivitySubmission.cmdr == cmdr,
        ManualActivitySubmission.status == "saved",
    ).order_by(ManualActivitySubmission.id.asc()).all()

    try:
        import requests as http_requests

        if rows:
            grouped = [_submission_to_webhook_data(row) for row in rows]
            embed = _build_bgs_tally_style_grouped_embed(
                submissions=grouped,
                ticktime=rows[-1].ticktime,
                captured_at=_utc_now(),
            )
            payload = {
                "content": "",
                "username": _safe_discord_text(rows[-1].cmdr, 80),
                "allowed_mentions": {"parse": []},
                "embeds": [embed],
            }
            if config["avatar_url"]:
                payload["avatar_url"] = config["avatar_url"]
            resp = http_requests.patch(
                f"{config['url'].rstrip('/')}/messages/{message_id}",
                json=_truncate_discord_payload(payload),
                timeout=config["timeout_seconds"],
            )
            if resp.status_code in (200, 204):
                return {"status": "posted", "message_id": message_id, "error": None}
            return {
                "status": "failed",
                "message_id": message_id,
                "error": f"Discord webhook edit returned HTTP {resp.status_code}",
            }

        resp = http_requests.delete(
            f"{config['url'].rstrip('/')}/messages/{message_id}",
            timeout=config["timeout_seconds"],
        )
        if resp.status_code in (200, 204, 404):
            return {"status": "deleted", "message_id": message_id, "error": None}
        return {
            "status": "failed",
            "message_id": message_id,
            "error": f"Discord webhook delete returned HTTP {resp.status_code}",
        }
    except Exception as exc:
        logger.warning("Manual activity Discord webhook delete/update failed: %s", _safe_error_text(exc))
        return {"status": "failed", "message_id": message_id, "error": _safe_error_text(exc)}


def _lookup_systems_from_eddn(query: str, limit: int):
    engine = _eddn_engine()
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT system_name AS name, MAX(system_address) AS address "
                    "FROM eddn_system_info "
                    "WHERE system_name LIKE :contains COLLATE NOCASE "
                    "GROUP BY lower(system_name) "
                    "ORDER BY CASE WHEN system_name LIKE :prefix COLLATE NOCASE THEN 0 ELSE 1 END, system_name "
                    "LIMIT :limit"
                ),
                {"contains": f"%{query}%", "prefix": f"{query}%", "limit": limit},
            ).mappings().all()
        return [{"name": row["name"], "address": row.get("address")} for row in rows if row.get("name")]
    except Exception:
        logger.warning("EDDN system_info lookup failed; trying system_tick_snapshot")
        return _lookup_systems_from_snapshot_payload(query, limit)
    finally:
        try:
            engine.dispose()
        except Exception:
            pass


def _lookup_systems_from_snapshot_payload(query: str, limit: int):
    engine = _eddn_engine()
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT system_name AS name, MAX(system_address) AS address "
                    "FROM system_tick_snapshot "
                    "WHERE system_name LIKE :contains COLLATE NOCASE "
                    "GROUP BY lower(system_name) "
                    "ORDER BY CASE WHEN system_name LIKE :prefix COLLATE NOCASE THEN 0 ELSE 1 END, system_name "
                    "LIMIT :limit"
                ),
                {"contains": f"%{query}%", "prefix": f"{query}%", "limit": limit},
            ).mappings().all()
        return [{"name": row["name"], "address": row.get("address")} for row in rows if row.get("name")]
    except Exception:
        logger.warning("EDDN snapshot system lookup failed")
        return []
    finally:
        try:
            engine.dispose()
        except Exception:
            pass


def _lookup_factions_from_snapshot_payload(query: Optional[str], system_name: str, limit: int):
    engine = _eddn_engine()
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT payload_json "
                    "FROM system_tick_snapshot "
                    "WHERE system_name = :system_name COLLATE NOCASE "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {"system_name": system_name},
            ).mappings().first()
        if not row:
            return []
        try:
            payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
        except Exception:
            return []
        factions = payload.get("Factions") if isinstance(payload, dict) else []
        if not isinstance(factions, list):
            return []
        query_lower = (query or "").lower()
        rows = []
        for faction in factions:
            if not isinstance(faction, dict):
                continue
            name = (faction.get("Name") or faction.get("name") or "").strip()
            if not name:
                continue
            if query_lower and query_lower not in name.lower():
                continue
            rows.append({
                "name": name,
                "state": faction.get("FactionState") or faction.get("state") or "None",
            })
        rows.sort(key=lambda item: _prefix_sort_key(item["name"], query or ""))
        return rows[:limit]
    except Exception:
        logger.warning("EDDN snapshot faction lookup failed")
        return []
    finally:
        try:
            engine.dispose()
        except Exception:
            pass


def _lookup_factions_from_eddn(query: Optional[str], system_name: str, limit: int):
    engine = _eddn_engine()
    if not engine:
        return []
    try:
        params = {"system_name": system_name, "limit": limit}
        query_filter = ""
        order_sql = "name"
        if query:
            params["contains"] = f"%{query}%"
            params["prefix"] = f"{query}%"
            query_filter = "AND name LIKE :contains COLLATE NOCASE "
            order_sql = "CASE WHEN name LIKE :prefix COLLATE NOCASE THEN 0 ELSE 1 END, name"
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT name, state "
                    "FROM eddn_faction "
                    "WHERE name IS NOT NULL AND name != '' "
                    "AND system_name = :system_name COLLATE NOCASE "
                    f"{query_filter}"
                    f"ORDER BY {order_sql} "
                    "LIMIT :limit"
                ),
                params,
            ).mappings().all()
        result = [{"name": row["name"], "state": row.get("state") or "None"} for row in rows if row.get("name")]
        if result:
            return result
        return _lookup_factions_from_snapshot_payload(query, system_name, limit)
    except Exception:
        logger.debug("EDDN faction table lookup failed; trying system_tick_snapshot payload")
        return _lookup_factions_from_snapshot_payload(query, system_name, limit)
    finally:
        try:
            engine.dispose()
        except Exception:
            pass


def _validate_system_faction_combination(system_name: str, faction_name: str) -> bool:
    matches = _lookup_factions_from_eddn(None, system_name, 100)
    return any(
        (item.get("name") or "").strip().lower() == faction_name.strip().lower()
        for item in matches
    )


@manual_activity_bp.route("/api/manual/lookup/systems", methods=["GET"])
def lookup_manual_systems():
    auth_error = _auth_and_set_tenant()
    if auth_error:
        return auth_error

    limit = _lookup_limit()
    query = _lookup_query(min_len=2)
    if not query:
        return jsonify([])

    try:
        rows = []
        rows.extend(_lookup_systems_from_eddn(query, limit))
        tenant_limit = limit * 3
        event_rows = db.session.execute(
            text(
                "SELECT starsystem AS name, MAX(systemaddress) AS address "
                "FROM event "
                "WHERE starsystem IS NOT NULL AND starsystem != '' "
                "AND starsystem LIKE :contains COLLATE NOCASE "
                "GROUP BY lower(starsystem) "
                "ORDER BY CASE WHEN starsystem LIKE :prefix COLLATE NOCASE THEN 0 ELSE 1 END, starsystem "
                "LIMIT :limit"
            ),
            {"contains": f"%{query}%", "prefix": f"{query}%", "limit": tenant_limit},
        ).mappings().all()
        rows.extend({"name": row["name"], "address": row["address"]} for row in event_rows)

        system_rows = db.session.execute(
            text(
                "SELECT name, MAX(address) AS address "
                "FROM system "
                "WHERE name IS NOT NULL AND name != '' "
                "AND name LIKE :contains COLLATE NOCASE "
                "GROUP BY lower(name) "
                "ORDER BY CASE WHEN name LIKE :prefix COLLATE NOCASE THEN 0 ELSE 1 END, name "
                "LIMIT :limit"
            ),
            {"contains": f"%{query}%", "prefix": f"{query}%", "limit": tenant_limit},
        ).mappings().all()
        rows.extend({"name": row["name"], "address": row["address"]} for row in system_rows)
        return jsonify(_dedupe_systems(rows, query, limit))
    except Exception as exc:
        logger.warning("Manual system lookup failed: %s", _safe_error_text(exc))
        return _json_response_error("Manual system lookup failed", 500)


@manual_activity_bp.route("/api/manual/lookup/factions", methods=["GET"])
def lookup_manual_factions():
    auth_error = _auth_and_set_tenant()
    if auth_error:
        return auth_error

    limit = _lookup_limit()
    system_name = (request.args.get("system") or "").strip() or None
    if not system_name:
        return jsonify([])
    query = (request.args.get("q") or "").strip() or None

    try:
        rows = _lookup_factions_from_eddn(query, system_name, limit * 3)
        return jsonify(_dedupe_name_rows(rows, query or "", limit, state_default="None"))
    except Exception as exc:
        logger.warning("Manual faction lookup failed: %s", _safe_error_text(exc))
        return _json_response_error("Manual faction lookup failed", 500)


@manual_activity_bp.route("/api/manual/lookup/cmdrs", methods=["GET"])
def lookup_manual_cmdrs():
    auth_error = _auth_and_set_tenant()
    if auth_error:
        return auth_error

    limit = _lookup_limit()
    query = _lookup_query(min_len=1)
    if not query:
        return jsonify([])

    try:
        rows = []
        tenant_limit = limit * 3
        cmdr_rows = db.session.query(Cmdr.name).filter(
            Cmdr.name.isnot(None),
            Cmdr.name != "",
            Cmdr.name.ilike(f"%{query}%"),
        ).order_by(
            func.lower(Cmdr.name)
        ).limit(tenant_limit).all()
        rows.extend({"name": name} for (name,) in cmdr_rows if name)

        event_rows = db.session.execute(
            text(
                "SELECT DISTINCT cmdr AS name "
                "FROM event "
                "WHERE cmdr IS NOT NULL AND cmdr != '' "
                "AND cmdr LIKE :contains COLLATE NOCASE "
                "ORDER BY CASE WHEN cmdr LIKE :prefix COLLATE NOCASE THEN 0 ELSE 1 END, cmdr "
                "LIMIT :limit"
            ),
            {"contains": f"%{query}%", "prefix": f"{query}%", "limit": tenant_limit},
        ).mappings().all()
        rows.extend({"name": row["name"]} for row in event_rows)

        activity_rows = db.session.execute(
            text(
                "SELECT DISTINCT cmdr AS name "
                "FROM activity "
                "WHERE cmdr IS NOT NULL AND cmdr != '' "
                "AND cmdr LIKE :contains COLLATE NOCASE "
                "ORDER BY CASE WHEN cmdr LIKE :prefix COLLATE NOCASE THEN 0 ELSE 1 END, cmdr "
                "LIMIT :limit"
            ),
            {"contains": f"%{query}%", "prefix": f"{query}%", "limit": tenant_limit},
        ).mappings().all()
        rows.extend({"name": row["name"]} for row in activity_rows)
        return jsonify(_dedupe_name_rows(rows, query, limit))
    except Exception as exc:
        logger.warning("Manual cmdr lookup failed: %s", _safe_error_text(exc))
        return _json_response_error("Manual cmdr lookup failed", 500)


def _create_response(status, submission, data, event_ids, include_error=False):
    response = {
        "status": status,
        "submission_id": submission.submission_id,
        "activity_id": submission.activity_id,
        "event_ids": event_ids,
        "tickid": submission.tickid,
        "ticktime": submission.ticktime,
        "captured_at": submission.captured_at,
        "cmdr": submission.cmdr,
        "activity_type": submission.activity_type,
        "system": submission.system_name,
        "faction": submission.faction_name,
        "amount": data.get("amount"),
        "count": data.get("count"),
        "influence": data.get("influence"),
        "webhook_status": submission.webhook_status,
        "webhook_message_id": submission.webhook_message_id,
    }
    if include_error and submission.webhook_error:
        response["webhook_error"] = submission.webhook_error
    return response


def _validate_delete_payload(payload: dict) -> dict:
    root = _as_dict(payload, "request body")
    source = _trimmed_string(root.get("source"), "source", 64)
    if source != "discord_modal":
        raise ManualActivityError("source must be discord_modal")
    discord = _as_dict(root.get("discord"), "discord")
    return {
        "source": source,
        "discord": {
            "guild_id": _optional_string(discord.get("guild_id"), "discord.guild_id", 64),
            "channel_id": _optional_string(discord.get("channel_id"), "discord.channel_id", 64),
            "user_id": _trimmed_string(discord.get("user_id"), "discord.user_id", 64),
            "interaction_id": _optional_string(discord.get("interaction_id"), "discord.interaction_id", 64),
        },
    }


def _event_ids_for_submission(submission: ManualActivitySubmission) -> list:
    try:
        event_ids = json.loads(submission.event_ids_json or "[]")
    except Exception:
        event_ids = []
    return [event_id for event_id in event_ids if isinstance(event_id, int)]


def _deleted_activity_snapshot(submission: ManualActivitySubmission) -> dict:
    return _manual_activity_submission_snapshot(submission)


def _manual_activity_submission_snapshot(submission: ManualActivitySubmission) -> dict:
    return {
        "submission_id": submission.submission_id,
        "cmdr": submission.cmdr,
        "system": submission.system_name,
        "faction": submission.faction_name,
        "activity_type": submission.activity_type,
        "amount": submission.amount,
        "count": submission.count,
        "influence": submission.influence,
        "cz_type": submission.cz_type,
        "settlement": submission.settlement,
        "event_ids": _event_ids_for_submission(submission),
        "captured_at": submission.captured_at,
        "webhook_status": submission.webhook_status,
    }


def _delete_synthetic_events(event_ids: list):
    if not event_ids:
        return
    mission_completed_ids = [
        row[0]
        for row in db.session.query(MissionCompletedEvent.id)
        .filter(MissionCompletedEvent.event_id.in_(event_ids))
        .all()
    ]
    if mission_completed_ids:
        db.session.query(MissionCompletedInfluence).filter(
            MissionCompletedInfluence.mission_id.in_(mission_completed_ids)
        ).delete(synchronize_session=False)

    subtype_models = (
        MarketBuyEvent,
        MarketSellEvent,
        MissionCompletedEvent,
        MissionFailedEvent,
        MultiSellExplorationDataEvent,
        RedeemVoucherEvent,
        SellExplorationDataEvent,
        CommitCrimeEvent,
        FactionKillBondEvent,
        SyntheticCZ,
        SyntheticGroundCZ,
    )
    for model in subtype_models:
        db.session.query(model).filter(model.event_id.in_(event_ids)).delete(synchronize_session=False)
    db.session.query(Event).filter(Event.id.in_(event_ids)).delete(synchronize_session=False)


def _find_manual_submissions_for_delete(operation: str, discord_user_id: str, tickid: str) -> list:
    base_query = db.session.query(ManualActivitySubmission).filter(
        ManualActivitySubmission.discord_user_id == discord_user_id,
        ManualActivitySubmission.tickid == tickid,
        ManualActivitySubmission.status == "saved",
    )
    if operation == "clear_ct":
        return base_query.order_by(ManualActivitySubmission.id.asc()).all()

    latest = base_query.order_by(
        ManualActivitySubmission.captured_at.desc(),
        ManualActivitySubmission.id.desc(),
    ).first()
    if not latest:
        return []

    suffix = ":combat_bond"
    if latest.submission_id.endswith(suffix):
        base_submission_id = latest.submission_id[: -len(suffix)]
        return base_query.filter(
            (ManualActivitySubmission.submission_id == base_submission_id)
            | (ManualActivitySubmission.submission_id.like(f"{base_submission_id}:%"))
        ).order_by(ManualActivitySubmission.id.asc()).all()
    return [latest]


def _deleted_response(operation: str, tickid: str, ticktime: str, snapshots: list, webhook_result=None):
    webhook_result = webhook_result or {"status": "skipped", "error": None}
    status = "deleted" if snapshots else "no_op"
    return {
        "status": status,
        "operation": operation,
        "tickid": tickid,
        "ticktime": ticktime,
        "deleted_count": len(snapshots),
        "deleted_activities": snapshots,
        "webhook_status": webhook_result.get("status") or "skipped",
        "webhook_error": webhook_result.get("error"),
    }


def _list_current_tick_response(tickid: str, ticktime: str, snapshots: list):
    return {
        "status": "ok" if snapshots else "no_op",
        "operation": "list_ct",
        "tickid": tickid,
        "ticktime": ticktime,
        "count": len(snapshots),
        "activities": snapshots,
    }


def _combine_webhook_results(results: list):
    if not results:
        return {"status": "skipped", "error": None}
    errors = [item.get("error") for item in results if item.get("error")]
    if errors:
        return {"status": "failed", "error": "; ".join(errors)}
    statuses = [item.get("status") for item in results if item.get("status")]
    if "posted" in statuses:
        return {"status": "posted", "error": None}
    if "deleted" in statuses:
        return {"status": "deleted", "error": None}
    return {"status": statuses[0] if statuses else "skipped", "error": None}


def _manual_activity_delete(operation: str):
    auth_error = _auth_and_set_tenant()
    if auth_error:
        return auth_error

    try:
        payload = request.get_json(silent=True)
        if payload is None:
            return _json_response_error("Request body must be valid JSON", 400)
        if _contains_forbidden_tick_field(payload):
            return _json_response_error(TICK_FIELD_ERROR, 400)
        data = _validate_delete_payload(payload)
        tickid, ticktime = _get_current_tick()
    except ManualActivityError as exc:
        return _json_response_error(exc.message, exc.status_code)

    from app import commit_with_retry, db_write_lock

    with db_write_lock:
        try:
            submissions = _find_manual_submissions_for_delete(operation, data["discord"]["user_id"], tickid)
            snapshots = [_deleted_activity_snapshot(submission) for submission in submissions]
            message_keys = sorted({
                (submission.webhook_message_id, submission.tickid, submission.cmdr)
                for submission in submissions
                if submission.webhook_message_id
            })
            deleted_at = _utc_now()
            for submission in submissions:
                _reverse_activity_tree(submission)
                _delete_synthetic_events(_event_ids_for_submission(submission))
                submission.status = "deleted"
                submission.error_message = _dump_json({
                    "operation": operation,
                    "actor_discord_user_id": data["discord"]["user_id"],
                    "deleted_at": deleted_at,
                    "previous_status": "saved",
                })
            commit_with_retry(db.session)
        except Exception as exc:
            db.session.rollback()
            logger.exception("Manual activity delete failed")
            return _json_response_error(_safe_error_text(exc), 400)

    if not snapshots:
        return jsonify(_deleted_response(operation, tickid, ticktime, [])), 200

    webhook_results = []
    for message_id, message_tickid, cmdr in message_keys:
        webhook_results.append(_delete_or_update_manual_activity_webhook(message_id, message_tickid, cmdr))
    webhook_result = _combine_webhook_results(webhook_results)
    return jsonify(_deleted_response(operation, tickid, ticktime, snapshots, webhook_result)), 200


@manual_activity_bp.route("/api/manual/activity/undo", methods=["POST"])
def undo_manual_activity():
    return _manual_activity_delete("undo")


@manual_activity_bp.route("/api/manual/activity/clear-ct", methods=["POST"])
def clear_current_tick_manual_activity():
    return _manual_activity_delete("clear_ct")


@manual_activity_bp.route("/api/manual/activity/list-ct", methods=["POST"])
def list_current_tick_manual_activity():
    auth_error = _auth_and_set_tenant()
    if auth_error:
        return auth_error

    try:
        payload = request.get_json(silent=True)
        if payload is None:
            return _json_response_error("Request body must be valid JSON", 400)
        if _contains_forbidden_tick_field(payload):
            return _json_response_error(TICK_FIELD_ERROR, 400)
        data = _validate_delete_payload(payload)
        tickid, ticktime = _get_current_tick()
    except ManualActivityError as exc:
        return _json_response_error(exc.message, exc.status_code)

    submissions = db.session.query(ManualActivitySubmission).filter(
        ManualActivitySubmission.discord_user_id == data["discord"]["user_id"],
        ManualActivitySubmission.tickid == tickid,
        ManualActivitySubmission.status == "saved",
    ).order_by(ManualActivitySubmission.id.asc()).all()
    snapshots = [_manual_activity_submission_snapshot(submission) for submission in submissions]
    return jsonify(_list_current_tick_response(tickid, ticktime, snapshots)), 200


@manual_activity_bp.route("/api/manual/activity", methods=["POST"])
def post_manual_activity():
    auth_error = _auth_and_set_tenant()
    if auth_error:
        return auth_error

    try:
        payload = request.get_json(silent=True)
        if payload is None:
            return _json_response_error("Request body must be valid JSON", 400)
        if _contains_forbidden_tick_field(payload):
            return _json_response_error(TICK_FIELD_ERROR, 400)
        data = _validate_payload(payload)
        captured_at = _utc_now()
        payload_hash = _payload_hash(payload)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except ManualActivityError as exc:
        return _json_response_error(exc.message, exc.status_code)

    from app import commit_with_retry, db_write_lock

    with db_write_lock:
        try:
            existing = db.session.query(ManualActivitySubmission).filter_by(submission_id=data["submission_id"]).first()
            if existing:
                if existing.payload_hash == payload_hash:
                    event_ids = json.loads(existing.event_ids_json or "[]")
                    duplicate_data = {
                        "amount": existing.amount,
                        "count": existing.count,
                        "influence": existing.influence,
                    }
                    response = _create_response("duplicate", existing, duplicate_data, event_ids)
                    response["webhook_status"] = "skipped_duplicate"
                    response.pop("tickid", None)
                    response.pop("ticktime", None)
                    response.pop("captured_at", None)
                    response.pop("cmdr", None)
                    response.pop("activity_type", None)
                    response.pop("system", None)
                    response.pop("faction", None)
                    response.pop("amount", None)
                    response.pop("count", None)
                    response.pop("influence", None)
                    response.pop("webhook_message_id", None)
                    return jsonify(response), 200
                return _json_response_error("submission_id already exists with different payload", 409)

            tickid, ticktime = _get_current_tick()
            data["system_address"] = _resolve_system_address(data["system_name"], data["system_address"])
            if not _validate_system_faction_combination(data["system_name"], data["faction_name"]):
                raise ManualActivityError("Faction is not present in the selected system.", 400)

            submission = ManualActivitySubmission(
                submission_id=data["submission_id"],
                source=data["source"],
                discord_guild_id=data["discord"].get("guild_id"),
                discord_channel_id=data["discord"].get("channel_id"),
                discord_user_id=data["discord"].get("user_id"),
                discord_message_id=data["discord"].get("message_id"),
                discord_interaction_id=data["discord"].get("interaction_id"),
                cmdr=data["cmdr"],
                tickid=tickid,
                ticktime=ticktime,
                captured_at=captured_at,
                client_timestamp=data.get("client_timestamp"),
                system_name=data["system_name"],
                system_address=data["system_address"],
                faction_name=data["faction_name"],
                faction_state=data["faction_state"],
                activity_type=data["activity_type"],
                amount=data.get("amount"),
                count=data.get("count"),
                influence=data.get("influence"),
                cz_type=data.get("cz_type"),
                settlement=data.get("settlement"),
                payload_hash=payload_hash,
                payload_json=payload_json,
                note=data.get("note"),
                status="saved",
                created_at=captured_at,
            )
            db.session.add(submission)
            activity = _update_activity_tree(data, tickid, ticktime, captured_at)
            event_ids = _create_events(data, captured_at, tickid, ticktime)
            submission.activity_id = activity.id
            submission.event_ids_json = json.dumps(event_ids)
            commit_with_retry(db.session)
        except ManualActivityError as exc:
            db.session.rollback()
            return _json_response_error(exc.message, exc.status_code)
        except Exception as exc:
            db.session.rollback()
            logger.exception("Manual activity processing failed")
            return _json_response_error(_safe_error_text(exc), 400)

    webhook_result = _send_or_update_manual_activity_webhook(submission)
    with db_write_lock:
        try:
            stored_submission = db.session.query(ManualActivitySubmission).filter_by(submission_id=submission.submission_id).first()
            stored_submission.webhook_status = webhook_result["status"]
            stored_submission.webhook_message_id = webhook_result["message_id"]
            stored_submission.webhook_error = webhook_result["error"]
            stored_submission.webhook_posted_at = _utc_now()
            commit_with_retry(db.session)
            submission = stored_submission
        except Exception as exc:
            db.session.rollback()
            logger.warning("Manual activity webhook status update failed: %s", _safe_error_text(exc))
            submission.webhook_status = webhook_result["status"]
            submission.webhook_message_id = webhook_result["message_id"]
            submission.webhook_error = webhook_result["error"]

    return jsonify(_create_response("saved", submission, data, event_ids, include_error=True)), 200
