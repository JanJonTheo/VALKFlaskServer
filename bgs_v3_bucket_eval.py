# bgs_v3_bucket_eval.py
# -*- coding: utf-8 -*-
"""
VALK BGS v3 - "The 4 Bucket" Evaluation (Phase 2: Persisted Runs)

What this module does
---------------------
This module evaluates *player activity* and converts it into *bucket effort points* following the
Bucket Model concept described in "The BGS Guide" (Bucket Model, p. 36 ff.).

Current scope (Alpha)
---------------------
- Buckets implemented here:
  - Combat (Bounties via RedeemVoucherEvent, Type='bounty', expanded from rv.factions JSON list)
  - Exploration (SellExplorationDataEvent + MultiSellExplorationDataEvent, grouped by station_faction)
- The evaluator aggregates *per Cmdr first*, converts credits to points using a logarithmic function,
  and then sums points (reflecting diminishing returns / soft caps).
- Results are merged by (systemaddress, faction) and can be sent to Discord with an attached chart
  showing the two bucket curves and Cmdr markers.

Tick handling (IMPORTANT)
-------------------------
- We use **ticktime only** (string). tickid is legacy and can still be used as a filter,
  but all persistence keys are based on ticktime.
- For persistence we resolve ticktime in this order:
  1) request parameter "ticktime"
  2) fdev_tick_monitor.last_tick["value"] (server process tick source)

Phase 2: Persistence (ticktime-only)
------------------------------------
When calling the Discord endpoint, evaluation results are persisted by default into the tenant DB:
- bgs_eval_run     (one row per evaluation call)
- bgs_eval_result  (upsert per UNIQUE(ticktime, system_name, faction))

Persistence can be disabled per call:
- persist=0 / persist=false / persist=off  -> no DB writes
This enables manual re-runs multiple times a day without creating additional persisted runs.

Integration
-----------
    from bgs_v3_bucket_eval import register_bucket_v3_routes
    register_bucket_v3_routes(app, db, require_api_key)

Endpoints
---------
- GET  /api/bgs/v3/bucket
- POST /api/bgs/v3/bucket/discord        (persist enabled by default; disable via persist=0)
- GET  /api/bgs/v3/bucket/chart

Notes
-----
- DB timestamps are stored as ISO8601 strings "...Z" and are lexicographically filterable via BETWEEN.
- Population is enriched from EDDN DB (EDDN_DATABASE) if configured; otherwise population/effect are None.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from dateutil.relativedelta import relativedelta
from flask import jsonify, request, g, has_request_context
from urllib.parse import quote
from sqlalchemy import text
from sqlalchemy import create_engine
import requests
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Bounty Curve rendering (optional, for Discord image attachments)
from io import BytesIO
from flask import Response
import matplotlib
matplotlib.use("Agg")  # headless server rendering
import matplotlib.pyplot as plt

# Reuse tenant helpers from fac_shoutout_scheduler
from fac_shoutout_scheduler import get_discord_webhook
from fac_shoutout_scheduler import get_tenants

# Import send_discord_message helper (optional)
try:
    from fac_shoutout_scheduler import send_discord_message
except Exception:
    send_discord_message = None

# Phase 2 persistence (ticktime-only)
try:
    from fdev_tick_monitor import last_tick as _last_tick
except Exception:
    _last_tick = {"value": ""}

try:
    from models import BGSEvalRun, BGSEvalResult
except Exception:
    BGSEvalRun = None
    BGSEvalResult = None


# ---------------------------
# Init Logger
# ---------------------------

def init_logger():
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / "bgs_v3.log"

    # Only initialize handlers once. Subsequent calls should return the same logger
    logger = logging.getLogger("bgs_v3_bucket_eval")
    if getattr(logger, "_initialized", False):
        return logger

    # First-time initialization
    logger.setLevel(logging.INFO)
    log_handler = RotatingFileHandler(log_path, maxBytes=4 * 1024 * 1024, backupCount=10)
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    log_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    logger.addHandler(log_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False

    # mark as initialized to prevent double-adding handlers
    logger._initialized = True
    logger.info(f"Log Path: {log_path.resolve()}")
    return logger


# ---------------------------
# Helpers
# ---------------------------

def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _safe_bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _resolve_ticktime_for_persist(request_ticktime: Optional[str], logger) -> Optional[str]:
    """
    Persistierung erfolgt ausschließlich über ticktime (String).
    Priorität:
      1) ticktime Parameter (Request)
      2) fdev_tick_monitor.last_tick["value"] (Serverprozess)
    """
    tt = (request_ticktime or "").strip()
    if tt:
        logger.info(f"[EVAL_PERSIST] ticktime resolved from request_ticktime: {tt}")
        return tt

    try:
        tt2 = (_last_tick or {}).get("value") or ""
        tt2 = str(tt2).strip()
        if tt2:
            logger.info(f"[EVAL_PERSIST] ticktime resolved from fdev_tick_monitor.last_tick: {tt2}")
            return tt2
    except Exception as e:
        logger.warning(f"[EVAL_PERSIST] ticktime resolve from last_tick failed: {e}")

    logger.warning("[EVAL_PERSIST] No ticktime available; persistence skipped.")
    return None


def _persist_bgs_eval_results_ticktime_only(
    db,
    *,
    ticktime: str,
    eval_type: str,
    version: str,
    merged_rows: List[Dict[str, Any]],
    systems_cmdr_totals: Dict[str, Dict[str, Any]],
    logger
) -> bool:
    """
    Persist Phase 2 results into:
      - BGSEvalRun (one row per call)
      - BGSEvalResult (upsert per (ticktime, system_name, faction))

    total_effect:
      - Prefer computed bgs_effect if present
      - Else fallback to total_points

    breakdown_json:
      - stores credits/points/effect/population etc.

    cmdr_json:
      - aggregated Cmdr totals per system (already computed later in _send_bucket_to_discord)
    """
    if BGSEvalRun is None or BGSEvalResult is None:
        logger.warning("[EVAL_PERSIST] Models BGSEvalRun/BGSEvalResult not available (import failed); skip.")
        return False

    if not ticktime or not isinstance(ticktime, str):
        logger.warning("[EVAL_PERSIST] Invalid ticktime; skip.")
        return False

    created_at = _utcnow_iso()

    try:
        logger.info(
            "[EVAL_PERSIST] Start: ticktime='%s' eval_type='%s' version='%s' rows=%d systems=%d",
            ticktime, eval_type, version, len(merged_rows or []), len(systems_cmdr_totals or {})
        )

        # 1) Run header
        run = BGSEvalRun(
            ticktime=ticktime,
            created_at=created_at,
            eval_type=eval_type,
            version=version,
            meta_json=json.dumps({
                "source": "bgs_v3_bucket_eval",
                "metrics": ["bounty", "exploration"],
                "mode": "per_cmdr_then_sum_per_metric_then_sum",
                "rows": int(len(merged_rows or [])),
                "systems": int(len(systems_cmdr_totals or {})),
            }, ensure_ascii=False)
        )
        db.session.add(run)

        inserted = 0
        updated = 0
        skipped = 0

        # 2) Results
        for r in (merged_rows or []):
            system_name = (r.get("system") or "").strip()
            faction = (r.get("faction") or "").strip()
            if not system_name or not faction:
                skipped += 1
                continue

            # choose "total_effect"
            te = r.get("bgs_effect")
            if te is None:
                te = r.get("total_points")
            try:
                total_effect = float(te or 0.0)
            except Exception:
                total_effect = 0.0

            breakdown = {
                "systemaddress": r.get("systemaddress"),
                "population": r.get("population"),
                "bounty_credits": int(r.get("bounty_credits", 0) or 0),
                "exploration_credits": int(r.get("exploration_credits", 0) or 0),
                "total_credits": int(r.get("total_credits", 0) or 0),
                "bounty_points": float(r.get("bounty_points", 0.0) or 0.0),
                "exploration_points": float(r.get("exploration_points", 0.0) or 0.0),
                "total_points": float(r.get("total_points", 0.0) or 0.0),
                "bgs_effect": r.get("bgs_effect"),
                "vouchers": int(r.get("vouchers", 0) or 0),
                "sales": int(r.get("sales", 0) or 0),
                "first_ts": r.get("first_ts"),
                "last_ts": r.get("last_ts"),
            }

            # cmdr aggregation is stored per system (not per faction)
            cmdr_blob = systems_cmdr_totals.get(system_name) or {}
            cmdr_count = None
            try:
                cmdr_count = int(cmdr_blob.get("_active_cmdrs", 0) or 0)
            except Exception:
                cmdr_count = None

            cmdr_json = None
            try:
                # only store the table itself (without helper keys) to keep it smaller
                cmdr_table = cmdr_blob.get("cmdrs") if isinstance(cmdr_blob.get("cmdrs"), dict) else {}
                cmdr_json = json.dumps(cmdr_table, ensure_ascii=False) if cmdr_table else None
            except Exception:
                cmdr_json = None

            row = (BGSEvalResult.query
                   .filter_by(ticktime=ticktime, system_name=system_name, faction=faction)
                   .one_or_none())

            if row:
                row.total_effect = total_effect
                row.breakdown_json = json.dumps(breakdown, ensure_ascii=False)
                row.cmdr_count = cmdr_count
                row.cmdr_json = cmdr_json
                row.created_at = created_at
                updated += 1
            else:
                row = BGSEvalResult(
                    ticktime=ticktime,
                    system_name=system_name,
                    faction=faction,
                    total_effect=total_effect,
                    breakdown_json=json.dumps(breakdown, ensure_ascii=False),
                    cmdr_count=cmdr_count,
                    cmdr_json=cmdr_json,
                    created_at=created_at
                )
                db.session.add(row)
                inserted += 1

        db.session.commit()

        logger.info(
            "[EVAL_PERSIST] Done: ticktime='%s' inserted=%d updated=%d skipped=%d",
            ticktime, inserted, updated, skipped
        )
        return True

    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.exception(f"[EVAL_PERSIST] Failed: {e}")
        return False


# ---------------------------
# Helpers: period -> timestamp range
# ---------------------------

def _period_range_utc(period: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (start_iso, end_iso) as strings:
        YYYY-MM-DDT00:00:00Z, YYYY-MM-DDT23:59:59Z
    If period == "all" or unknown -> (None, None)
    """
    today = datetime.utcnow()
    start = end = None

    if period == "cw":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period == "lw":
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
    elif period == "cm":
        start = today.replace(day=1)
        end = (start + relativedelta(months=1)) - timedelta(days=1)
    elif period == "lm":
        this_month_start = today.replace(day=1)
        start = this_month_start - relativedelta(months=1)
        end = this_month_start - timedelta(days=1)
    elif period == "2m":
        this_month_start = today.replace(day=1)
        start = this_month_start - relativedelta(months=2)
        end = this_month_start - timedelta(days=1)
    elif period == "y":
        start = today.replace(month=1, day=1)
        end = today.replace(month=12, day=31)
    elif period == "cd":
        start = end = today
    elif period == "ld":
        start = end = today - timedelta(days=1)
    elif period == "all":
        return None, None

    if not start or not end:
        return None, None

    start_iso = start.strftime("%Y-%m-%dT00:00:00Z")
    end_iso = end.strftime("%Y-%m-%dT23:59:59Z")
    return start_iso, end_iso


# ---------------------------
# Bucket formulas
# ---------------------------

def bounty_points_from_sum(sum_bounty_credits: int, clamp_negative: bool = True) -> float:
    """
    Apply bucket formula to the *summed* bounty credits:
        points = 1.33 * log2(sum_bounty / 450000)
    """
    if sum_bounty_credits <= 0:
        return 0.0

    ratio = sum_bounty_credits / 450_000.0
    # ratio can be < 1 -> log2 negative
    points = 1.33 * (math.log(ratio, 2) if ratio > 0 else float("-inf"))

    if clamp_negative and points < 0:
        return 0.0
    return float(points)


def exploration_points_from_sum(sum_exploration_credits: int, clamp_negative: bool = True) -> float:
    """
    Exploration bucket formula (per your new spec):
        exploration_points = 1.0 * log2( exploration_value / 1,000,000 )
    """
    if sum_exploration_credits <= 0:
        return 0.0

    ratio = sum_exploration_credits / 1_000_000.0
    points = 1.0 * (math.log(ratio, 2) if ratio > 0 else float("-inf"))

    if clamp_negative and points < 0:
        return 0.0
    return float(points)


# ---------------------------
# BGS effect (population-adjusted)
# ---------------------------

def bgs_effect(effort: float, population: float) -> float:
    """
    Calculate BGS effect from effort and system population.
    Uses: Effect = max(0.025, (1 - log10(Population)/10.875)) * Effort
    """
    if population <= 0:
        raise ValueError("population must be > 0")

    pop_factor = max(0.025, 1.0 - (math.log10(population) / 10.875))
    return pop_factor * effort


# ---------------------------
# EDDN population helper
# ---------------------------

def get_population_map_from_eddn(system_names: List[str]) -> Dict[str, Optional[int]]:
    """
    Given a list of system names, returns a dict mapping system_name -> population (int) or None.
    If EDDN_DATABASE is not configured or an error occurs, returns None for the affected systems.
    """
    logger = init_logger()
    eddn_db_uri = os.getenv("EDDN_DATABASE")
    # initialize result with None defaults
    pop_map: Dict[str, Optional[int]] = {name: None for name in system_names}
    logger.info(f"get_population_map_from_eddn called for {len(system_names or [])} system(s)")
    if not eddn_db_uri:
        logger.info("EDDN_DATABASE not configured; returning None for all populations")
        return pop_map

    try:
        eddn_engine = create_engine(eddn_db_uri)
        with eddn_engine.connect() as conn:
            for name in system_names:
                if not name:
                    pop_map[name] = None
                    continue
                try:
                    row = conn.execute(
                        text("SELECT population FROM eddn_system_info WHERE system_name = :name COLLATE NOCASE"),
                        {"name": name}
                    ).mappings().first()
                    pop = int(row["population"]) if row and row.get("population") is not None else None
                    pop_map[name] = pop
                    logger.info(f"Population lookup: '{name}' -> {pop}")
                except Exception as e:
                    logger.warning(f"Population lookup failed for '{name}': {e}")
                    pop_map[name] = None
    except Exception as e:
        logger.warning(f"Failed to open EDDN DB at {eddn_db_uri}: {e}")
        # On engine/connect failure, return defaults (None)
        return pop_map

    found = sum(1 for v in pop_map.values() if v is not None)
    logger.info(f"get_population_map_from_eddn returning map for {len(pop_map)} systems, with {found} populations found")
    return pop_map


# ---------------------------
# Aggregation container
# ---------------------------

@dataclass
class BountyAgg:
    system: Optional[str]
    systemaddress: Optional[int]
    faction: str
    bounty_credits: int = 0
    vouchers: int = 0  # count of original redeem voucher rows contributing (best-effort)
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    tickids: set = field(default_factory=set)
    ticktimes: set = field(default_factory=set)
    cmdrs: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # cmdr -> {credits}

    def add(self, amount: int, timestamp: Optional[str], tickid: Optional[str], ticktime: Optional[str], cmdr: Optional[str], count_voucher: bool):
        self.bounty_credits += int(amount or 0)
        if count_voucher:
            self.vouchers += 1

        if tickid:
            self.tickids.add(tickid)

        if ticktime:
            self.ticktimes.add(ticktime)

        if timestamp:
            if self.first_ts is None or timestamp < self.first_ts:
                self.first_ts = timestamp
            if self.last_ts is None or timestamp > self.last_ts:
                self.last_ts = timestamp

        if cmdr:
            d = self.cmdrs.setdefault(cmdr, {"credits": 0})
            d["credits"] += int(amount or 0)


@dataclass
class ExplorationAgg:
    system: Optional[str]
    systemaddress: Optional[int]
    faction: str
    exploration_credits: int = 0
    sales: int = 0  # count of exploration sale events contributing (best-effort)
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    tickids: set = field(default_factory=set)
    ticktimes: set = field(default_factory=set)
    cmdrs: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # cmdr -> {credits}

    def add(
        self,
        amount: int,
        timestamp: Optional[str],
        tickid: Optional[str],
        ticktime: Optional[str],
        cmdr: Optional[str],
        count_sale: bool,
    ):
        self.exploration_credits += int(amount or 0)
        if count_sale:
            self.sales += 1

        if tickid:
            self.tickids.add(tickid)

        if ticktime:
            self.ticktimes.add(ticktime)

        if timestamp:
            if self.first_ts is None or timestamp < self.first_ts:
                self.first_ts = timestamp
            if self.last_ts is None or timestamp > self.last_ts:
                self.last_ts = timestamp

        if cmdr:
            d = self.cmdrs.setdefault(cmdr, {"credits": 0})
            d["credits"] += int(amount or 0)


# ---------------------------
# Core evaluator
# ---------------------------

def evaluate_bounty_bucket(
    db,
    period: str = "all",
    tickid: Optional[str] = None,
    ticktime: Optional[str] = None,
    systemaddress: Optional[str] = None,
    system: Optional[str] = None,
    faction_filter: Optional[str] = None,
    include_cmdr: bool = True,
    clamp_negative: bool = True,
    enrich_population: bool = True,
) -> List[Dict[str, Any]]:
    """
    Reads RedeemVoucherEvent rows for Type='bounty', expands rv.factions JSON (Factions[]),
    sums credits per (systemaddress, faction), THEN applies the formula on the SUM.
    """

    logger = init_logger()
    logger.info(f"evaluate_bounty_bucket called: period={period}, tickid={tickid}, ticktime={ticktime}, systemaddress={systemaddress}, system={system}, faction_filter={faction_filter}, include_cmdr={include_cmdr}, clamp_negative={clamp_negative}")

    where = ["rv.type = 'bounty'", "rv.factions IS NOT NULL", "rv.factions != ''"]

    params: Dict[str, Any] = {}

    # period filter on Event.timestamp (alias e)
    start_iso, end_iso = _period_range_utc(period)
    if start_iso and end_iso:
        where.append("e.timestamp BETWEEN :start_ts AND :end_ts")
        params["start_ts"] = start_iso
        params["end_ts"] = end_iso

    if tickid:
        where.append("e.tickid = :tickid")
        params["tickid"] = tickid

    if ticktime:
        # ticktime is an ISO timestamp string (human-readable tick timestamp); filter exact match
        where.append("e.ticktime = :ticktime")
        params["ticktime"] = ticktime

    if systemaddress:
        # compare against COALESCE(rv.systemaddress, e.systemaddress)
        where.append("CAST(COALESCE(rv.systemaddress, e.systemaddress) AS TEXT) = :systemaddress")
        params["systemaddress"] = str(systemaddress)

    if system:
        where.append("COALESCE(rv.starsystem, e.starsystem) = :system")
        params["system"] = system

    # We'll apply faction_filter after JSON expansion (because factions are inside JSON)

    sql = f"""
        SELECT
            e.id AS event_id,
            e.timestamp AS event_ts,
            e.tickid AS tickid,
            e.ticktime AS ticktime,
            e.cmdr AS cmdr,
            COALESCE(rv.starsystem, e.starsystem) AS starsystem,
            COALESCE(rv.systemaddress, e.systemaddress) AS systemaddress,
            rv.factions AS factions_json
        FROM redeem_voucher_event rv
        JOIN event e ON e.id = rv.event_id
        WHERE {" AND ".join(where)}
        ORDER BY e.timestamp ASC
    """

    logger.info(f"About to execute bounty query with params: {params}")
    rows = db.session.execute(text(sql), params).mappings().all()
    try:
        logger.info(f"Query executed, fetched {len(rows)} rows satisfying where-clause")
    except Exception:
        pass

    # Aggregate key: (systemaddress, faction)
    aggs: Dict[Tuple[Optional[int], str], BountyAgg] = {}

    # To count "vouchers" (original RV rows) per (system,faction), we can:
    # - count each (event_id,faction) once, even if JSON contains duplicate faction entries (rare).
    seen_event_faction: set[Tuple[int, str]] = set()

    for r in rows:
        event_id = int(r["event_id"])
        event_ts = r.get("event_ts")
        tickid_val = r.get("tickid")
        ticktime_val = r.get("ticktime")
        cmdr = r.get("cmdr")
        starsystem = r.get("starsystem")
        sysaddr = r.get("systemaddress")

        factions_json = r.get("factions_json")
        try:
            factions_list = json.loads(factions_json) if factions_json else []
        except Exception:
            factions_list = []

        if not isinstance(factions_list, list):
            continue

        for fe in factions_list:
            if not isinstance(fe, dict):
                continue
            faction_name = fe.get("Faction")
            amount = fe.get("Amount", 0)

            if not faction_name:
                continue

            if faction_filter and faction_name != faction_filter:
                continue

            key = (sysaddr, faction_name)
            if key not in aggs:
                aggs[key] = BountyAgg(
                    system=starsystem,
                    systemaddress=sysaddr,
                    faction=faction_name,
                )

            count_voucher = False
            ef_key = (event_id, faction_name)
            if ef_key not in seen_event_faction:
                seen_event_faction.add(ef_key)
                count_voucher = True

            aggs[key].add(
                amount=int(amount or 0),
                timestamp=event_ts,
                tickid=tickid_val,
                ticktime=ticktime_val,
                cmdr=cmdr if include_cmdr else None,
                count_voucher=count_voucher,
            )

    logger.info(f"Aggregation complete: produced {len(aggs)} groups (faction-per-system)")

    # Finalize: compute points PER CMDR and SUM (per cmdr -> sum), then expose cmdr breakdown
    result: List[Dict[str, Any]] = []
    for (sysaddr, faction_name), agg in aggs.items():

        # Per-CMDR points
        cmdr_out = {}
        total_points = 0.0

        if include_cmdr:
            for c, d in agg.cmdrs.items():
                c_credits = int(d.get("credits", 0) or 0)
                c_points = bounty_points_from_sum(c_credits, clamp_negative=clamp_negative)
                total_points += float(c_points)
                cmdr_out[c] = {
                    "credits": c_credits,
                    "points": round(float(c_points), 6),
                }
        else:
            # if cmdr details not requested, fall back to sum_then_formula for a consistent total
            total_points = float(bounty_points_from_sum(agg.bounty_credits, clamp_negative=clamp_negative))

        item: Dict[str, Any] = {
            "system": agg.system,
            "systemaddress": str(agg.systemaddress) if agg.systemaddress is not None else None,
            "faction": agg.faction,
            "bounty_credits": agg.bounty_credits,
            # IMPORTANT: total points is now sum(per cmdr points)
            "bounty_points": round(float(total_points), 6),
            "vouchers": agg.vouchers,
            "first_ts": agg.first_ts,
            "last_ts": agg.last_ts,
            "tickids": sorted(list(agg.tickids)) if agg.tickids else [],
            "ticktimes": sorted(list(agg.ticktimes)) if getattr(agg, 'ticktimes', None) else [],
        }

        if include_cmdr:
            item["cmdrs"] = cmdr_out

        result.append(item)

    # Sort by bounty_points desc, then credits desc
    result.sort(key=lambda x: (float(x.get("bounty_points", 0.0) or 0.0), int(x.get("bounty_credits", 0) or 0)),
                reverse=True)

    # additional info: how many unique systems involved
    unique_systems = {r.get("system") for r in result if r.get("system")}
    logger.info(f"Aggregation covers {len(unique_systems)} unique system(s): {sorted(list(unique_systems))[:10]}")

    # Enrich results with population (optional)
    if enrich_population:
        try:
            sysnames = sorted({(item.get("system") or None) for item in result if item.get("system")})
            pop_map = get_population_map_from_eddn(sysnames)

            for item in result:
                sysname = item.get("system")
                item["population"] = pop_map.get(sysname) if sysname else None
                try:
                    pop = item.get("population")
                    if pop is not None and isinstance(pop, (int, float)) and pop > 0:
                        effort = float(item.get("bounty_points", 0.0) or 0.0)
                        be = bgs_effect(effort, float(pop))
                        item["bgs_effect"] = round(float(be), 6)
                    else:
                        item["bgs_effect"] = None
                except Exception:
                    item["bgs_effect"] = None
        except Exception:
            for item in result:
                if "population" not in item:
                    item["population"] = None
                if "bgs_effect" not in item:
                    item["bgs_effect"] = None
    else:
        for item in result:
            if "population" not in item:
                item["population"] = None
            if "bgs_effect" not in item:
                item["bgs_effect"] = None

    return result


def evaluate_exploration_bucket(
    db,
    period: str = "all",
    tickid: Optional[str] = None,
    ticktime: Optional[str] = None,
    systemaddress: Optional[str] = None,
    system: Optional[str] = None,
    faction_filter: Optional[str] = None,
    include_cmdr: bool = True,
    clamp_negative: bool = True,
    enrich_population: bool = True,
) -> List[Dict[str, Any]]:
    """
    Reads SellExplorationDataEvent + MultiSellExplorationDataEvent rows,
    sums credits per (systemaddress, station_faction), THEN applies the same bucket formula.

    Credits:
      - SellExplorationDataEvent.earnings
      - MultiSellExplorationDataEvent.total_earnings

    System:
      - *.starsystem / *.systemaddress (fallback: event.starsystem / event.systemaddress)

    Faction:
      - *.station_faction (single faction)
    """

    logger = init_logger()
    logger.info(
        f"evaluate_exploration_bucket called: period={period}, tickid={tickid}, ticktime={ticktime}, "
        f"systemaddress={systemaddress}, system={system}, faction_filter={faction_filter}, "
        f"include_cmdr={include_cmdr}, clamp_negative={clamp_negative}"
    )

    params: Dict[str, Any] = {}

    start_iso, end_iso = _period_range_utc(period)

    # We apply the same filter set to BOTH tables via two WHERE blocks, then UNION ALL.
    where_common = ["e.cmdr IS NOT NULL"]

    if start_iso and end_iso:
        where_common.append("e.timestamp BETWEEN :start_ts AND :end_ts")
        params["start_ts"] = start_iso
        params["end_ts"] = end_iso

    if tickid:
        where_common.append("e.tickid = :tickid")
        params["tickid"] = tickid

    if ticktime:
        where_common.append("e.ticktime = :ticktime")
        params["ticktime"] = ticktime

    if systemaddress:
        # compare against COALESCE(tbl.systemaddress, e.systemaddress)
        where_common.append("CAST(COALESCE(x_systemaddress, e.systemaddress) AS TEXT) = :systemaddress")
        params["systemaddress"] = str(systemaddress)

    if system:
        where_common.append("COALESCE(x_starsystem, e.starsystem) = :system")
        params["system"] = system

    # NOTE: faction_filter is applied after fetch (like bounty JSON expansion), for symmetry and safety.

    # Build SQL with placeholders "x_starsystem/x_systemaddress" replaced per SELECT.
    where_sql_template = " AND ".join(where_common)

    sql = f"""
        SELECT
            e.id AS event_id,
            e.timestamp AS event_ts,
            e.tickid AS tickid,
            e.ticktime AS ticktime,
            e.cmdr AS cmdr,
            COALESCE(se.starsystem, e.starsystem) AS starsystem,
            COALESCE(se.systemaddress, e.systemaddress) AS systemaddress,
            se.station_faction AS station_faction,
            se.earnings AS credits
        FROM sell_exploration_data_event se
        JOIN event e ON e.id = se.event_id
        WHERE {where_sql_template.replace("x_starsystem", "se.starsystem").replace("x_systemaddress", "se.systemaddress")}

        UNION ALL

        SELECT
            e.id AS event_id,
            e.timestamp AS event_ts,
            e.tickid AS tickid,
            e.ticktime AS ticktime,
            e.cmdr AS cmdr,
            COALESCE(me.starsystem, e.starsystem) AS starsystem,
            COALESCE(me.systemaddress, e.systemaddress) AS systemaddress,
            me.station_faction AS station_faction,
            me.total_earnings AS credits
        FROM multi_sell_exploration_data_event me
        JOIN event e ON e.id = me.event_id
        WHERE {where_sql_template.replace("x_starsystem", "me.starsystem").replace("x_systemaddress", "me.systemaddress")}

        ORDER BY event_ts ASC
    """

    logger.info(f"About to execute exploration query with params: {params}")
    rows = db.session.execute(text(sql), params).mappings().all()
    try:
        logger.info(f"Exploration query executed, fetched {len(rows)} rows satisfying where-clause")
    except Exception:
        pass

    # Aggregate key: (systemaddress, faction)
    aggs: Dict[Tuple[Optional[int], str], ExplorationAgg] = {}

    # Count sales events per (system,faction) once per event_id (like bounty vouchers logic).
    seen_event: set[int] = set()

    for r in rows:
        event_id = int(r["event_id"])
        event_ts = r.get("event_ts")
        tickid_val = r.get("tickid")
        ticktime_val = r.get("ticktime")
        cmdr = r.get("cmdr")
        starsystem = r.get("starsystem")
        sysaddr = r.get("systemaddress")
        faction_name = (r.get("station_faction") or "").strip()
        credits = int(r.get("credits") or 0)

        if not faction_name:
            continue

        if faction_filter and faction_name != faction_filter:
            continue

        key = (sysaddr, faction_name)
        if key not in aggs:
            aggs[key] = ExplorationAgg(
                system=starsystem,
                systemaddress=sysaddr,
                faction=faction_name,
            )

        count_sale = False
        if event_id not in seen_event:
            seen_event.add(event_id)
            count_sale = True

        aggs[key].add(
            amount=credits,
            timestamp=event_ts,
            tickid=tickid_val,
            ticktime=ticktime_val,
            cmdr=cmdr if include_cmdr else None,
            count_sale=count_sale,
        )

    logger.info(f"Exploration aggregation complete: produced {len(aggs)} groups (faction-per-system)")

    # Finalize: compute points PER CMDR and SUM (per cmdr -> sum), then expose cmdr breakdown
    result: List[Dict[str, Any]] = []
    for (sysaddr, faction_name), agg in aggs.items():

        cmdr_out = {}
        total_points = 0.0

        if include_cmdr:
            for c, d in agg.cmdrs.items():
                c_credits = int(d.get("credits", 0) or 0)
                c_points = exploration_points_from_sum(c_credits, clamp_negative=clamp_negative)
                total_points += float(c_points)
                cmdr_out[c] = {
                    "credits": c_credits,
                    "points": round(float(c_points), 6),
                }
        else:
            total_points = float(exploration_points_from_sum(agg.exploration_credits, clamp_negative=clamp_negative))

        item: Dict[str, Any] = {
            "system": agg.system,
            "systemaddress": str(agg.systemaddress) if agg.systemaddress is not None else None,
            "faction": agg.faction,
            "exploration_credits": agg.exploration_credits,
            "exploration_points": round(float(total_points), 6),
            "sales": agg.sales,
            "first_ts": agg.first_ts,
            "last_ts": agg.last_ts,
            "tickids": sorted(list(agg.tickids)) if agg.tickids else [],
            "ticktimes": sorted(list(agg.ticktimes)) if getattr(agg, "ticktimes", None) else [],
        }

        if include_cmdr:
            item["cmdrs"] = cmdr_out

        result.append(item)

    # Sort by exploration_points desc, then credits desc
    result.sort(
        key=lambda x: (float(x.get("exploration_points", 0.0) or 0.0), int(x.get("exploration_credits", 0) or 0)),
        reverse=True,
    )

    unique_systems = {r.get("system") for r in result if r.get("system")}
    logger.info(f"Exploration covers {len(unique_systems)} unique system(s): {sorted(list(unique_systems))[:10]}")

    # Enrich with population + bgs_effect (optional)
    if enrich_population:
        try:
            sysnames = sorted({(item.get("system") or None) for item in result if item.get("system")})
            pop_map = get_population_map_from_eddn(sysnames)

            for item in result:
                sysname = item.get("system")
                item["population"] = pop_map.get(sysname) if sysname else None
                try:
                    pop = item.get("population")
                    if pop is not None and isinstance(pop, (int, float)) and pop > 0:
                        effort = float(item.get("exploration_points", 0.0) or 0.0)
                        be = bgs_effect(effort, float(pop))
                        item["bgs_effect"] = round(float(be), 6)
                    else:
                        item["bgs_effect"] = None
                except Exception:
                    item["bgs_effect"] = None
        except Exception:
            for item in result:
                if "population" not in item:
                    item["population"] = None
                if "bgs_effect" not in item:
                    item["bgs_effect"] = None
    else:
        for item in result:
            if "population" not in item:
                item["population"] = None
            if "bgs_effect" not in item:
                item["bgs_effect"] = None

    return result


def evaluate_bucket_all_metrics(
    db,
    period: str = "all",
    tickid: Optional[str] = None,
    ticktime: Optional[str] = None,
    systemaddress: Optional[str] = None,
    system: Optional[str] = None,
    faction_filter: Optional[str] = None,
    include_cmdr: bool = True,
    clamp_negative: bool = True,
) -> List[Dict[str, Any]]:
    """
    Combined evaluator over ALL metrics (currently: bounty + exploration).
    Output grouped by (systemaddress, faction).
    Includes totals + per-cmdr totals for combined plot and discord.
    """

    logger = init_logger()
    logger.info(
        f"evaluate_bucket_all_metrics called: period={period}, tickid={tickid}, ticktime={ticktime}, "
        f"systemaddress={systemaddress}, system={system}, faction_filter={faction_filter}, "
        f"include_cmdr={include_cmdr}, clamp_negative={clamp_negative}"
    )

    bounty_rows = evaluate_bounty_bucket(
        db=db,
        period=period,
        tickid=tickid,
        ticktime=ticktime,
        systemaddress=systemaddress,
        system=system,
        faction_filter=faction_filter,
        include_cmdr=include_cmdr,
        clamp_negative=clamp_negative,
        enrich_population=False,
    )

    expl_rows = evaluate_exploration_bucket(
        db=db,
        period=period,
        tickid=tickid,
        ticktime=ticktime,
        systemaddress=systemaddress,
        system=system,
        faction_filter=faction_filter,
        include_cmdr=include_cmdr,
        clamp_negative=clamp_negative,
        enrich_population=False,
    )

    merged: Dict[Tuple[Optional[str], str], Dict[str, Any]] = {}

    def _k(row: Dict[str, Any]) -> Tuple[Optional[str], str]:
        return (row.get("systemaddress"), (row.get("faction") or "").strip())

    # seed from bounty
    for r in bounty_rows:
        key = _k(r)
        merged[key] = {
            "system": r.get("system"),
            "systemaddress": r.get("systemaddress"),
            "faction": r.get("faction"),
            "bounty_credits": int(r.get("bounty_credits", 0) or 0),
            "bounty_points": float(r.get("bounty_points", 0.0) or 0.0),
            "vouchers": int(r.get("vouchers", 0) or 0),
            "exploration_credits": 0,
            "exploration_points": 0.0,
            "sales": 0,
            "first_ts": r.get("first_ts"),
            "last_ts": r.get("last_ts"),
            "tickids": sorted(set(r.get("tickids") or [])),
            "ticktimes": sorted(set(r.get("ticktimes") or [])),
            "cmdrs": {},
        }

        if include_cmdr and isinstance(r.get("cmdrs"), dict):
            for cmdr, d in r["cmdrs"].items():
                merged[key]["cmdrs"].setdefault(cmdr, {
                    "bounty_credits": 0, "bounty_points": 0.0,
                    "exploration_credits": 0, "exploration_points": 0.0,
                })
                merged[key]["cmdrs"][cmdr]["bounty_credits"] += int(d.get("credits", 0) or 0)
                merged[key]["cmdrs"][cmdr]["bounty_points"] += float(d.get("points", 0.0) or 0.0)

    # merge exploration
    for r in expl_rows:
        key = _k(r)
        if key not in merged:
            merged[key] = {
                "system": r.get("system"),
                "systemaddress": r.get("systemaddress"),
                "faction": r.get("faction"),
                "bounty_credits": 0,
                "bounty_points": 0.0,
                "vouchers": 0,
                "exploration_credits": int(r.get("exploration_credits", 0) or 0),
                "exploration_points": float(r.get("exploration_points", 0.0) or 0.0),
                "sales": int(r.get("sales", 0) or 0),
                "first_ts": r.get("first_ts"),
                "last_ts": r.get("last_ts"),
                "tickids": sorted(set(r.get("tickids") or [])),
                "ticktimes": sorted(set(r.get("ticktimes") or [])),
                "cmdrs": {},
            }
        else:
            merged[key]["exploration_credits"] = int(r.get("exploration_credits", 0) or 0)
            merged[key]["exploration_points"] = float(r.get("exploration_points", 0.0) or 0.0)
            merged[key]["sales"] = int(r.get("sales", 0) or 0)

            merged[key]["tickids"] = sorted(set((merged[key].get("tickids") or []) + (r.get("tickids") or [])))
            merged[key]["ticktimes"] = sorted(set((merged[key].get("ticktimes") or []) + (r.get("ticktimes") or [])))

            ft = merged[key].get("first_ts")
            lt = merged[key].get("last_ts")
            rft = r.get("first_ts")
            rlt = r.get("last_ts")
            merged[key]["first_ts"] = min([x for x in [ft, rft] if x]) if (ft or rft) else None
            merged[key]["last_ts"] = max([x for x in [lt, rlt] if x]) if (lt or rlt) else None

        if include_cmdr and isinstance(r.get("cmdrs"), dict):
            for cmdr, d in r["cmdrs"].items():
                merged[key]["cmdrs"].setdefault(cmdr, {
                    "bounty_credits": 0, "bounty_points": 0.0,
                    "exploration_credits": 0, "exploration_points": 0.0,
                })
                merged[key]["cmdrs"][cmdr]["exploration_credits"] += int(d.get("credits", 0) or 0)
                merged[key]["cmdrs"][cmdr]["exploration_points"] += float(d.get("points", 0.0) or 0.0)

    out: List[Dict[str, Any]] = []
    for row in merged.values():
        row["total_credits"] = int(row.get("bounty_credits", 0) or 0) + int(row.get("exploration_credits", 0) or 0)
        row["total_points"] = float(row.get("bounty_points", 0.0) or 0.0) + float(row.get("exploration_points", 0.0) or 0.0)

        if include_cmdr and isinstance(row.get("cmdrs"), dict):
            for _, d in row["cmdrs"].items():
                d["total_credits"] = int(d.get("bounty_credits", 0) or 0) + int(d.get("exploration_credits", 0) or 0)
                d["total_points"] = float(d.get("bounty_points", 0.0) or 0.0) + float(d.get("exploration_points", 0.0) or 0.0)

        out.append(row)

    # Population enrichment ONCE per system
    try:
        sysnames = sorted({(item.get("system") or None) for item in out if item.get("system")})
        pop_map = get_population_map_from_eddn(sysnames)

        for item in out:
            sysname = item.get("system")
            item["population"] = pop_map.get(sysname) if sysname else None
            try:
                pop = item.get("population")
                if pop is not None and isinstance(pop, (int, float)) and pop > 0:
                    effort = float(item.get("total_points", 0.0) or 0.0)
                    item["bgs_effect"] = round(float(bgs_effect(effort, float(pop))), 6)
                else:
                    item["bgs_effect"] = None
            except Exception:
                item["bgs_effect"] = None
    except Exception:
        for item in out:
            if "population" not in item:
                item["population"] = None
            if "bgs_effect" not in item:
                item["bgs_effect"] = None

    # sort by total_points desc, then total_credits desc
    out.sort(
        key=lambda x: (float(x.get("total_points", 0.0) or 0.0), int(x.get("total_credits", 0) or 0)),
        reverse=True,
    )

    return out


# ---------------------------
# Curve rendering
# ---------------------------

def render_bucket_curves_png(
    current_bounty_credits: int,
    current_exploration_credits: int,
    clamp_negative: bool = True,
    x_max_mcr: int = 100,
    title: str = "Bucket Curves",
    bounty_cmdr_points: Optional[Dict[str, Dict[str, Any]]] = None,       # cmdr -> {credits, points}
    exploration_cmdr_points: Optional[Dict[str, Dict[str, Any]]] = None,  # cmdr -> {credits, points}
) -> bytes:
    """
    Renders a 2-line curve chart (PNG) like the reference screenshot:
      - Bounties (black): bounty_points_from_sum(credits)
      - Exploration (orange): exploration_points_from_sum(credits)

    Cmdr markers are plotted ON the corresponding curve:
      - bounty_cmdr_points on black curve
      - exploration_cmdr_points on orange curve
    """

    # Curve samples
    x_vals: List[float] = []
    y_bounty: List[float] = []
    y_expl: List[float] = []

    for mcr in range(1, x_max_mcr + 1):
        credits = mcr * 1_000_000
        x_vals.append(float(mcr))
        y_bounty.append(float(bounty_points_from_sum(credits, clamp_negative=clamp_negative)))
        y_expl.append(float(exploration_points_from_sum(credits, clamp_negative=clamp_negative)))

    fig = plt.figure(figsize=(12, 6), dpi=160)
    ax = fig.add_subplot(111)

    # Lines (match screenshot intent)
    ax.plot(x_vals, y_bounty, linewidth=2.5, color="black", label="Bounties (M Cr)")
    ax.plot(x_vals, y_expl, linewidth=2.5, color="orange", label="Exploration (M Cr)")

    # --- Cmdr markers for bounty (on black curve) ---
    if bounty_cmdr_points:
        items = []
        for name, d in bounty_cmdr_points.items():
            try:
                cr = float(d.get("credits", 0) or 0)
                pts = float(d.get("points", 0) or 0)
                items.append((name, cr, pts))
            except Exception:
                continue

        # plot markers
        xs = [(cr / 1_000_000.0) for _, cr, _ in items]
        ys = [pts for _, _, pts in items]
        ax.scatter(xs, ys, s=90, color="black", alpha=0.85, zorder=5, label="Cmdr (Bounties)")

        # label top N by points
        items.sort(key=lambda t: t[2], reverse=True)
        label_n = min(10, len(items))
        for i in range(label_n):
            name, cr, pts = items[i]
            ax.annotate(
                name[:12],
                xy=(cr / 1_000_000.0, pts),
                xytext=(6, 4),
                textcoords="offset points",
                fontsize=9,
                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="black", alpha=0.8),
            )

    # --- Cmdr markers for exploration (on orange curve) ---
    if exploration_cmdr_points:
        items = []
        for name, d in exploration_cmdr_points.items():
            try:
                cr = float(d.get("credits", 0) or 0)
                pts = float(d.get("points", 0) or 0)
                items.append((name, cr, pts))
            except Exception:
                continue

        xs = [(cr / 1_000_000.0) for _, cr, _ in items]
        ys = [pts for _, _, pts in items]
        ax.scatter(xs, ys, s=90, color="orange", alpha=0.85, zorder=5, label="Cmdr (Exploration)")

        items.sort(key=lambda t: t[2], reverse=True)
        label_n = min(10, len(items))
        for i in range(label_n):
            name, cr, pts = items[i]
            ax.annotate(
                name[:12],
                xy=(cr / 1_000_000.0, pts),
                xytext=(6, -10),
                textcoords="offset points",
                fontsize=9,
                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="black", alpha=0.8),
            )

    # Labels / grid (match screenshot feel)
    ax.set_title(title)
    ax.set_xlabel("Credits (M Cr)")
    ax.set_ylabel("Bucket Points")
    ax.grid(True, which="both", linewidth=0.6, alpha=0.6)
    ax.legend(loc="upper left")

    ax.set_xlim(0, x_max_mcr)
    y_max = max(max(y_bounty) if y_bounty else 1, max(y_expl) if y_expl else 1)
    ax.set_ylim(0, y_max * 1.05)

    buf = BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------
# Flask integration (COMBINED only)
# ---------------------------

def register_bucket_v3_routes(app, db, require_api_key):
    """
    Combined endpoints (all metrics together):
        GET  /api/bgs/v3/bucket
        POST /api/bgs/v3/bucket/discord
        GET  /api/bgs/v3/bucket/chart
    """

    def _read_common_params():
        period = request.args.get("period", request.form.get("period", "all"))
        tickid = request.args.get("tickid", request.form.get("tickid"))
        ticktime = request.args.get("ticktime", request.form.get("ticktime"))
        systemaddress = request.args.get("systemaddress", request.form.get("systemaddress"))
        system = request.args.get("system", request.form.get("system"))
        faction = request.args.get("faction", request.form.get("faction"))

        include_cmdr = _safe_bool(request.args.get("include_cmdr") or request.form.get("include_cmdr"), default=True)
        clamp_negative = _safe_bool(request.args.get("clamp") or request.form.get("clamp"), default=True)
        x_max_mcr = int(request.args.get("x_max_mcr", request.form.get("x_max_mcr", "100")))

        # Phase 2: persistence toggle (default ON)
        persist_enabled = _safe_bool(request.args.get("persist") or request.form.get("persist"), default=True)

        return period, tickid, ticktime, systemaddress, system, faction, include_cmdr, clamp_negative, x_max_mcr, persist_enabled

    @app.route("/api/bgs/v3/bucket", methods=["GET"])
    @require_api_key
    def api_bgs_v3_bucket():
        period, tickid, ticktime, systemaddress, system, faction, include_cmdr, clamp_negative, _, _persist = _read_common_params()

        rows = evaluate_bucket_all_metrics(
            db=db,
            period=period,
            tickid=tickid,
            ticktime=ticktime,
            systemaddress=systemaddress,
            system=system,
            faction_filter=faction,
            include_cmdr=include_cmdr,
            clamp_negative=clamp_negative,
        )

        return jsonify({
            "bucket": "combined",
            "metrics": ["bounty", "exploration"],
            "mode": "per_cmdr_then_sum_per_metric_then_sum",
            "period": period,
            "filters": {
                "tickid": tickid,
                "ticktime": ticktime,
                "systemaddress": systemaddress,
                "system": system,
                "faction": faction,
                "include_cmdr": include_cmdr,
                "clamp_negative": clamp_negative,
            },
            "rows": rows,
        })

    @app.route("/api/bgs/v3/bucket/discord", methods=["POST"])
    @require_api_key
    def api_bgs_v3_bucket_discord():
        logger = init_logger()
        period, tickid, ticktime, systemaddress, system, faction, include_cmdr, clamp_negative, _, persist_enabled = _read_common_params()

        calling_tenant = getattr(g, "tenant", None)
        if not calling_tenant:
            logger.error("No tenant found in request context (g.tenant missing)")
            return jsonify({"error": "Tenant not found in request context"}), 500

        results = _send_bucket_to_discord(
            app=app,
            db=db,
            period=period,
            tenant=calling_tenant,
            tickid=tickid,
            ticktime=ticktime,
            systemaddress=systemaddress,
            system=system,
            faction=faction,
            include_cmdr=include_cmdr,
            clamp=clamp_negative,
            persist_enabled=persist_enabled,  # Phase 2 toggle
        )

        return jsonify({"results": results, "persist": persist_enabled})

    @app.route("/api/bgs/v3/bucket/chart", methods=["GET"])
    @require_api_key
    def api_bgs_v3_bucket_chart():
        period = request.args.get("period", "all")
        tickid = request.args.get("tickid")
        ticktime = request.args.get("ticktime")
        systemaddress = request.args.get("systemaddress")
        system = request.args.get("system")
        faction = request.args.get("faction")

        clamp_negative = _safe_bool(request.args.get("clamp"), default=True)
        x_max_mcr = int(request.args.get("x_max_mcr", "100"))

        # You need both evaluations present:
        bounty_rows = evaluate_bounty_bucket(
            db=db,
            period=period,
            tickid=tickid,
            ticktime=ticktime,
            systemaddress=systemaddress,
            system=system,
            faction_filter=faction,
            include_cmdr=True,
            clamp_negative=clamp_negative,
        )

        exploration_rows = evaluate_exploration_bucket(
            db=db,
            period=period,
            tickid=tickid,
            ticktime=ticktime,
            systemaddress=systemaddress,
            system=system,
            faction_filter=faction,
            include_cmdr=True,
            clamp_negative=clamp_negative,
        )

        # pick a target row (same system/faction) - prefer bounty row if exists, else exploration
        if bounty_rows:
            target = bounty_rows[0]
        elif exploration_rows:
            target = exploration_rows[0]
        else:
            return jsonify({"error": "No data found for the given filters."}), 404

        sysname = target.get("system", "?")
        facname = target.get("faction", "?")

        # map rows by (systemaddress,faction) to combine the correct pair
        def _key(r):
            return (r.get("systemaddress"), r.get("faction"))

        b_map = {_key(r): r for r in bounty_rows}
        e_map = {_key(r): r for r in exploration_rows}

        k = _key(target)
        b_row = b_map.get(k, {})
        e_row = e_map.get(k, {})

        bounty_credits = int(b_row.get("bounty_credits", 0) or 0)
        expl_credits = int(e_row.get("exploration_credits", 0) or 0)

        bounty_cmdr = b_row.get("cmdrs") if isinstance(b_row.get("cmdrs"), dict) else None
        expl_cmdr = e_row.get("cmdrs") if isinstance(e_row.get("cmdrs"), dict) else None

        title = f"Bucket Curves – {sysname} / {facname}"

        png_bytes = render_bucket_curves_png(
            current_bounty_credits=bounty_credits,
            current_exploration_credits=expl_credits,
            clamp_negative=clamp_negative,
            x_max_mcr=x_max_mcr,
            title=title,
            bounty_cmdr_points=bounty_cmdr,
            exploration_cmdr_points=expl_cmdr,
        )

        return Response(png_bytes, mimetype="image/png")


def _send_bucket_to_discord(
    app,
    db,
    period="all",
    tenant=None,
    tickid=None,
    ticktime=None,
    systemaddress=None,
    system=None,
    faction=None,
    include_cmdr=True,
    clamp=True,
    persist_enabled: bool = True
):
    """
    Discord sender (combined):
    - loads bounty + exploration ONCE
    - merges by (systemaddress,faction)
    - sends one message per system
    - attaches combined 2-line chart (bounty + exploration) with Cmdr markers on each line
    - Phase 2: optionally persist results (ticktime-only)
    """
    logger = init_logger()

    # 1) Load BOTH buckets once (cmdr included to draw markers)
    bounty_rows = evaluate_bounty_bucket(
        db=db,
        period=period,
        tickid=tickid,
        ticktime=ticktime,
        systemaddress=systemaddress,
        system=system,
        faction_filter=faction,
        include_cmdr=include_cmdr,
        clamp_negative=clamp,
        enrich_population=False,  # we enrich once after merge
    )

    exploration_rows = evaluate_exploration_bucket(
        db=db,
        period=period,
        tickid=tickid,
        ticktime=ticktime,
        systemaddress=systemaddress,
        system=system,
        faction_filter=faction,
        include_cmdr=include_cmdr,
        clamp_negative=clamp,
        enrich_population=False,  # we enrich once after merge
    )

    def _key(r: Dict[str, Any]) -> Tuple[Optional[str], str]:
        return (r.get("systemaddress"), (r.get("faction") or "").strip())

    b_map = {_key(r): r for r in bounty_rows}
    e_map = {_key(r): r for r in exploration_rows}

    all_keys = set(b_map.keys()) | set(e_map.keys())

    # 2) Merge by (systemaddress,faction)
    merged_rows: List[Dict[str, Any]] = []
    for k in all_keys:
        br = b_map.get(k) or {}
        er = e_map.get(k) or {}

        # prefer system name from bounty row, else exploration row
        sysname = br.get("system") or er.get("system")
        sysaddr = (br.get("systemaddress") or er.get("systemaddress"))
        fac = (br.get("faction") or er.get("faction") or "").strip()

        bounty_credits = int(br.get("bounty_credits", 0) or 0)
        bounty_points = float(br.get("bounty_points", 0.0) or 0.0)
        vouchers = int(br.get("vouchers", 0) or 0)

        expl_credits = int(er.get("exploration_credits", 0) or 0)
        expl_points = float(er.get("exploration_points", 0.0) or 0.0)
        sales = int(er.get("sales", 0) or 0)

        # merge ticks/times/ts
        tickids = sorted(set((br.get("tickids") or []) + (er.get("tickids") or [])))
        ticktimes = sorted(set((br.get("ticktimes") or []) + (er.get("ticktimes") or [])))

        ft_candidates = [x for x in [br.get("first_ts"), er.get("first_ts")] if x]
        lt_candidates = [x for x in [br.get("last_ts"), er.get("last_ts")] if x]
        first_ts = min(ft_candidates) if ft_candidates else None
        last_ts = max(lt_candidates) if lt_candidates else None

        # cmdr merge
        cmdrs_combined: Dict[str, Dict[str, Any]] = {}
        if include_cmdr:
            b_cmdrs = br.get("cmdrs") if isinstance(br.get("cmdrs"), dict) else {}
            e_cmdrs = er.get("cmdrs") if isinstance(er.get("cmdrs"), dict) else {}

            for cmdr_name, d in b_cmdrs.items():
                cmdrs_combined.setdefault(cmdr_name, {
                    "bounty_credits": 0, "bounty_points": 0.0,
                    "exploration_credits": 0, "exploration_points": 0.0,
                })
                cmdrs_combined[cmdr_name]["bounty_credits"] += int(d.get("credits", 0) or 0)
                cmdrs_combined[cmdr_name]["bounty_points"] += float(d.get("points", 0.0) or 0.0)

            for cmdr_name, d in e_cmdrs.items():
                cmdrs_combined.setdefault(cmdr_name, {
                    "bounty_credits": 0, "bounty_points": 0.0,
                    "exploration_credits": 0, "exploration_points": 0.0,
                })
                cmdrs_combined[cmdr_name]["exploration_credits"] += int(d.get("credits", 0) or 0)
                cmdrs_combined[cmdr_name]["exploration_points"] += float(d.get("points", 0.0) or 0.0)

            for _, d in cmdrs_combined.items():
                d["total_credits"] = int(d.get("bounty_credits", 0) or 0) + int(d.get("exploration_credits", 0) or 0)
                d["total_points"] = float(d.get("bounty_points", 0.0) or 0.0) + float(d.get("exploration_points", 0.0) or 0.0)

        row = {
            "system": sysname,
            "systemaddress": sysaddr,
            "faction": fac,

            "bounty_credits": bounty_credits,
            "bounty_points": bounty_points,
            "vouchers": vouchers,

            "exploration_credits": expl_credits,
            "exploration_points": expl_points,
            "sales": sales,

            "total_credits": bounty_credits + expl_credits,
            "total_points": bounty_points + expl_points,

            "first_ts": first_ts,
            "last_ts": last_ts,
            "tickids": tickids,
            "ticktimes": ticktimes,
        }

        if include_cmdr:
            row["cmdrs"] = cmdrs_combined

        merged_rows.append(row)

    # 3) Enrich population once per system, + effect on combined points
    try:
        sysnames = sorted({(r.get("system") or None) for r in merged_rows if r.get("system")})
        pop_map = get_population_map_from_eddn(sysnames)

        for r in merged_rows:
            sysn = r.get("system")
            r["population"] = pop_map.get(sysn) if sysn else None
            try:
                pop = r.get("population")
                if pop is not None and isinstance(pop, (int, float)) and float(pop) > 0:
                    r["bgs_effect"] = round(float(bgs_effect(float(r.get("total_points", 0.0) or 0.0), float(pop))), 6)
                else:
                    r["bgs_effect"] = None
            except Exception:
                r["bgs_effect"] = None
    except Exception:
        for r in merged_rows:
            r["population"] = None
            r["bgs_effect"] = None

    # Sort merged rows by total_points desc then total_credits desc
    merged_rows.sort(
        key=lambda x: (float(x.get("total_points", 0.0) or 0.0), int(x.get("total_credits", 0) or 0)),
        reverse=True,
    )

    # 4) Group: one message per system
    systems: Dict[str, List[Dict[str, Any]]] = {}
    for r in merged_rows:
        sysname = r.get("system") or "Unknown"
        systems.setdefault(sysname, []).append(r)

    tenants = [tenant] if tenant is not None else get_tenants()
    logger.info(f"Targeting {len(tenants)} tenant(s) for discord dispatch (systems={len(systems)})")
    results = []

    def _fmt_int(v):
        try:
            return f"{int(v):,}"
        except Exception:
            return "n/a"

    def _fmt_float(v, digits=2):
        try:
            return f"{float(v):.{digits}f}"
        except Exception:
            return "n/a"

    # -------------------------------------------------------------------------
    # Phase 2: Persist evaluation results (ticktime-only) BEFORE sending discord
    # -------------------------------------------------------------------------
    # Persist only for the normal API call path (tenant passed) – and only if enabled.
    systems_cmdr_totals: Dict[str, Dict[str, Any]] = {}
    if include_cmdr:
        # Build per-system cmdr totals once (used both for persistence + discord tables)
        for system_name, rows in systems.items():
            cmdr_totals: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                cmdrs = r.get("cmdrs") if isinstance(r.get("cmdrs"), dict) else {}
                for cmdr_name, d in cmdrs.items():
                    ct = cmdr_totals.setdefault(cmdr_name, {
                        "bounty_credits": 0, "bounty_points": 0.0,
                        "exploration_credits": 0, "exploration_points": 0.0,
                        "total_credits": 0, "total_points": 0.0,
                    })
                    ct["bounty_credits"] += int(d.get("bounty_credits", 0) or 0)
                    ct["bounty_points"] += float(d.get("bounty_points", 0.0) or 0.0)
                    ct["exploration_credits"] += int(d.get("exploration_credits", 0) or 0)
                    ct["exploration_points"] += float(d.get("exploration_points", 0.0) or 0.0)

            for _, d in cmdr_totals.items():
                d["total_credits"] = int(d.get("bounty_credits", 0) or 0) + int(d.get("exploration_credits", 0) or 0)
                d["total_points"] = float(d.get("bounty_points", 0.0) or 0.0) + float(d.get("exploration_points", 0.0) or 0.0)

            systems_cmdr_totals[system_name] = {
                "_active_cmdrs": int(len(cmdr_totals)),
                "cmdrs": cmdr_totals
            }

    if persist_enabled:
        ticktime_persist = _resolve_ticktime_for_persist(ticktime, logger)
        if ticktime_persist:
            logger.info("[EVAL_PERSIST] Persistence enabled with ticktime: %s; persisting results...", ticktime_persist)
            _persist_bgs_eval_results_ticktime_only(
                db=db,
                ticktime=ticktime_persist,
                eval_type="bgs_v3_4bucket",
                version="3.0",
                merged_rows=merged_rows,
                systems_cmdr_totals=systems_cmdr_totals,
                logger=logger
            )
        else:
            logger.warning("[EVAL_PERSIST] persist_enabled=True but ticktime missing -> skipped.")
    else:
        logger.info("[EVAL_PERSIST] Persistence disabled via parameter persist=0/false/off; skip.")

    # -------------------------------------------------------------------------
    # Discord output
    # -------------------------------------------------------------------------
    for t in tenants:
        webhook_url = get_discord_webhook(t, "bullis")
        if not webhook_url:
            logger.warning(f"No webhook for tenant {t.get('name')}")
            results.append({"tenant": t.get("name"), "status": "no_webhook"})
            continue

        period_label = period if period != "all" else "All Time"
        tick_label = ticktime or tickid or period_label

        # --- SEND HEADER MESSAGE FIRST (per tenant) ---
        has_systems_to_send = False
        for _sysname, _rows in systems.items():
            _rows_sorted = sorted(
                _rows,
                key=lambda r: (float(r.get("total_points", 0.0) or 0.0), int(r.get("total_credits", 0) or 0)),
                reverse=True,
            )
            if faction:
                _rows_sorted = [r for r in _rows_sorted if (r.get("faction") or "") == faction] or _rows_sorted
            if _rows_sorted:
                has_systems_to_send = True
                break

        if not has_systems_to_send:
            logger.info(f"Skipping Discord header for tenant {t.get('name')} - no systems to send.")
        else:
            try:
                header_msg = (
                    "## BGS v3 - 'The 4 Bucket' Evaluation\n"
                    "This evaluation applies the **BGS Bucket Model** by aggregating bounty credits per system and faction first and then converting the total into influence points using a logarithmic function, reflecting diminishing returns and soft caps. The approach follows the **four-bucket concept (combat, trade, exploration, missions)** described in *The BGS Guide* (see *\"The Bucket Model\"*, page 36 ff.), where balanced activity across multiple buckets is more effective than focusing on a single one (<https://sinc.science/bgsguide.pdf>).\n\n"
                    "Note: This is **Phase 2** focusing on the **Bounty Bucket** and **Exploration Bucket** and it is Alpha Version.\n\n"
                    f"Tenant: **{t.get('name')}**\n"
                    f"Period/Tick: **{tick_label}**\n"
                    f"Persisted: **{'yes' if persist_enabled else 'no'}**\n"
                )

                resp_hdr = requests.post(
                    webhook_url,
                    json={"content": header_msg, "allowed_mentions": {"parse": []}},
                    timeout=20,
                )
                if resp_hdr.status_code not in (200, 204):
                    logger.error(f"Discord webhook header error ({resp_hdr.status_code}): {resp_hdr.text}")
                    results.append({"tenant": t.get("name"), "system": None, "status": "header_discord_error", "code": resp_hdr.status_code})
                else:
                    results.append({"tenant": t.get("name"), "system": None, "status": "header_sent"})
                    try:
                        logger.info(f"Discord header sent for tenant={t.get('name')} tick={tick_label}")
                    except Exception:
                        pass

            except Exception as e:
                logger.exception(f"Discord header send failed for tenant={t.get('name')}")
                results.append({"tenant": t.get("name"), "system": None, "status": "header_exception", "error": str(e)})

        for system_name, rows in systems.items():
            rows_sorted = sorted(
                rows,
                key=lambda r: (float(r.get("total_points", 0.0) or 0.0), int(r.get("total_credits", 0) or 0)),
                reverse=True,
            )

            if faction:
                rows_sorted = [r for r in rows_sorted if (r.get("faction") or "") == faction] or rows_sorted

            if not rows_sorted:
                continue

            sys_total_points = sum(float(r.get("total_points", 0.0) or 0.0) for r in rows_sorted)
            sys_total_credits = sum(int(r.get("total_credits", 0) or 0) for r in rows_sorted)
            sys_bounty_points = sum(float(r.get("bounty_points", 0.0) or 0.0) for r in rows_sorted)
            sys_expl_points = sum(float(r.get("exploration_points", 0.0) or 0.0) for r in rows_sorted)
            sys_bounty_credits = sum(int(r.get("bounty_credits", 0) or 0) for r in rows_sorted)
            sys_expl_credits = sum(int(r.get("exploration_credits", 0) or 0) for r in rows_sorted)

            pop_val = rows_sorted[0].get("population")
            pop_txt = _fmt_int(pop_val) if pop_val else "unknown"

            sys_effect_total = None
            try:
                if pop_val and isinstance(pop_val, (int, float)) and float(pop_val) > 0:
                    sys_effect_total = bgs_effect(float(sys_total_points), float(pop_val))
            except Exception:
                sys_effect_total = None

            cmdr_blob = systems_cmdr_totals.get(system_name) or {}
            cmdr_totals = cmdr_blob.get("cmdrs") if isinstance(cmdr_blob.get("cmdrs"), dict) else {}
            active_cmdrs = int(cmdr_blob.get("_active_cmdrs", 0) or 0)

            # per-metric dicts for chart markers
            bounty_cmdr_points: Dict[str, Dict[str, Any]] = {}
            exploration_cmdr_points: Dict[str, Dict[str, Any]] = {}

            if include_cmdr:
                for name, d in cmdr_totals.items():
                    if int(d.get("bounty_credits", 0) or 0) > 0:
                        bounty_cmdr_points[name] = {"credits": int(d.get("bounty_credits", 0) or 0), "points": float(d.get("bounty_points", 0.0) or 0.0)}
                    if int(d.get("exploration_credits", 0) or 0) > 0:
                        exploration_cmdr_points[name] = {"credits": int(d.get("exploration_credits", 0) or 0), "points": float(d.get("exploration_points", 0.0) or 0.0)}

            # ---------------------------
            # Build discord content (combined tables)
            # ---------------------------
            lines: List[str] = []
            lines.append(f"## 📊 {system_name}")
            if faction:
                lines.append(f"Faction filter: **{faction}**")
            lines.append("")

            lines.append("**:star: System Overview**")
            lines.append("```text")
            lines.append(f"{'Population':<16} | {pop_txt:>15}")
            lines.append(f"{'Active Cmdrs':<16} | {_fmt_int(active_cmdrs):>15}")
            lines.append(f"{'Total Credits':<16} | {_fmt_int(sys_total_credits):>15}")
            lines.append(f"{'Bounty Credits':<16} | {_fmt_int(sys_bounty_credits):>15}")
            lines.append(f"{'Explr Credits':<16} | {_fmt_int(sys_expl_credits):>15}")
            lines.append(f"{'Bounty Effort':<16} | {_fmt_float(sys_bounty_points, 2):>15}")
            lines.append(f"{'Explr Effort':<16} | {_fmt_float(sys_expl_points, 2):>15}")
            lines.append("-" * 34)
            lines.append(f"{'Total Effort':<16} | {_fmt_float(sys_total_points, 2):>15}")
            lines.append(f"{'Total Effect':<16} | {('-' if sys_effect_total is None else _fmt_float(sys_effect_total, 2)):>15}")
            lines.append("```")
            lines.append("Calculated using the BGS Bucket Model (logarithmic scaling with diminishing returns).")
            lines.append("Refer to <https://sinc.science/bgsguide.pdf> page 39/40 for details.")
            lines.append("")

            lines.append("**:astronaut: Cmdr Contributions**")
            lines.append("```text")
            lines.append(f"{'Cmdr':<18} | {'B MCr':>8} | {'B Pt':>7} | {'E MCr':>8} | {'E Pt':>7} | {'T Pt':>7}")
            lines.append("-" * 70)

            cmdr_items = []
            for cmdr_name, d in cmdr_totals.items():
                cmdr_items.append((
                    cmdr_name,
                    int(d.get("bounty_credits", 0) or 0),
                    float(d.get("bounty_points", 0.0) or 0.0),
                    int(d.get("exploration_credits", 0) or 0),
                    float(d.get("exploration_points", 0.0) or 0.0),
                    float(d.get("total_points", 0.0) or 0.0),
                ))
            cmdr_items.sort(key=lambda x: x[5], reverse=True)

            max_rows = 25
            for cmdr_name, bcr, bpt, ecr, ept, tpt in cmdr_items[:max_rows]:
                lines.append(
                    f"{cmdr_name[:18]:<18} | {bcr/1_000_000:>8.2f} | {bpt:>7.2f} | {ecr/1_000_000:>8.2f} | {ept:>7.2f} | {tpt:>7.2f}"
                )
            if len(cmdr_items) > max_rows:
                lines.append(f"... ({len(cmdr_items) - max_rows} more cmdrs omitted)")
            lines.append("```")
            lines.append("")

            lines.append("**:classical_building: Faction Breakdown (within system)**")
            lines.append("```text")
            lines.append(f"{'Faction':<22} | {'B Pt':>7} | {'E Pt':>7} | {'T Pt':>7} | {'Share':>6}")
            lines.append("-" * 61)

            denom = sys_total_points if sys_total_points > 0 else 1.0
            for r in rows_sorted[:25]:
                fac = (r.get("faction") or "?")[:22]
                bp = float(r.get("bounty_points", 0.0) or 0.0)
                ep = float(r.get("exploration_points", 0.0) or 0.0)
                tp = float(r.get("total_points", 0.0) or 0.0)
                share = (tp / denom) * 100.0
                lines.append(f"{fac:<22} | {bp:>7.2f} | {ep:>7.2f} | {tp:>7.2f} | {share:>5.1f}%")
            if len(rows_sorted) > 25:
                lines.append(f"... ({len(rows_sorted) - 25} more factions omitted)")
            lines.append("```")
            lines.append("")

            content = "\n".join(lines)

            # ---------------------------
            # Render + attach chart (2 curves + cmdr markers)
            # ---------------------------
            file_payload = None
            try:
                title = f"Bucket Curves – {system_name}"
                png_bytes = render_bucket_curves_png(
                    current_bounty_credits=int(sys_bounty_credits),
                    current_exploration_credits=int(sys_expl_credits),
                    clamp_negative=clamp,
                    x_max_mcr=100,
                    title=title,
                    bounty_cmdr_points=bounty_cmdr_points if bounty_cmdr_points else None,
                    exploration_cmdr_points=exploration_cmdr_points if exploration_cmdr_points else None,
                )

                safe_sys = "".join(c for c in (system_name or "system") if c.isalnum() or c in ("-", "_"))[:40]
                filename = f"bucket_curves_{safe_sys}.png"
                file_payload = (filename, png_bytes, "image/png")
            except Exception as e:
                logger.warning(f"Chart rendering failed for {system_name}: {e}")
                file_payload = None

            # ---------------------------
            # Send message
            # ---------------------------
            try:
                if file_payload:
                    files = {"file": file_payload}
                    payload = {"content": content, "allowed_mentions": {"parse": []}}
                    resp = requests.post(
                        webhook_url,
                        data={"payload_json": json.dumps(payload)},
                        files=files,
                        timeout=20
                    )
                else:
                    resp = requests.post(
                        webhook_url,
                        json={"content": content, "allowed_mentions": {"parse": []}},
                        timeout=20
                    )

                if resp.status_code not in (200, 204):
                    logger.error(f"Discord webhook error ({resp.status_code}): {resp.text}")
                    results.append({
                        "tenant": t.get("name"),
                        "system": system_name,
                        "status": "discord_error",
                        "code": resp.status_code
                    })
                else:
                    results.append({
                        "tenant": t.get("name"),
                        "system": system_name,
                        "status": "sent",
                        "chart_attached": bool(file_payload)
                    })
                    try:
                        logger.info(f"Discord message sent tenant={t.get('name')} system={system_name} chart_attached={bool(file_payload)}")
                    except Exception:
                        pass

            except Exception as e:
                logger.exception(f"Discord send failed tenant={t.get('name')} system={system_name}")
                results.append({
                    "tenant": t.get("name"),
                    "system": system_name,
                    "status": "exception",
                    "error": str(e)
                })

    return results
