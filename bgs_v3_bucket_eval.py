# bgs_v3_bucket_eval.py
# -*- coding: utf-8 -*-
"""
VALK BGS v3 - Bucket Model Evaluator (Phase 1: Combat/Bounty Bucket)

- Liest RedeemVoucherEvents (Type='bounty')
- Nimmt als *einzige* Faction-Quelle das JSON-Feld RedeemVoucherEvent.factions (Factions[])
- Aggregiert zuerst Credits pro (SystemAddress, Faction) über den gewählten Zeitraum/Tick
- Wendet *danach* die Bucket-Formel auf die SUMME an:
    bounty_points = 1.33 * log2( sum_bounty / 450000 )

Integration:
    from bgs_v3_bucket_eval import register_bucket_v3_routes
    register_bucket_v3_routes(app, db, require_api_key)

Hinweise:
- Zeitstempel sind im DB-Model als String (ISO8601 "....Z"). Lexikographisch filterbar per BETWEEN.
- Wenn RedeemVoucherEvent.systemaddress/starsystem nicht gesetzt sind, wird auf Event.systemaddress/starsystem fallbacked.
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
    log_handler = RotatingFileHandler(log_path, maxBytes=128 * 1024 * 1024, backupCount=3)
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


def _safe_bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


# ---------------------------
# Bucket formula (Bounty)
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
    cmdrs: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # cmdr -> {credits}

    def add(self, amount: int, timestamp: Optional[str], tickid: Optional[str], cmdr: Optional[str], count_voucher: bool):
        self.bounty_credits += int(amount or 0)
        if count_voucher:
            self.vouchers += 1

        if tickid:
            self.tickids.add(tickid)

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
    systemaddress: Optional[str] = None,
    system: Optional[str] = None,
    faction_filter: Optional[str] = None,
    include_cmdr: bool = True,
    clamp_negative: bool = True,
) -> List[Dict[str, Any]]:
    """
    Reads RedeemVoucherEvent rows for Type='bounty', expands rv.factions JSON (Factions[]),
    sums credits per (systemaddress, faction), THEN applies the formula on the SUM.
    """

    logger = init_logger()
    logger.info(f"evaluate_bounty_bucket called: period={period}, tickid={tickid}, systemaddress={systemaddress}, system={system}, faction_filter={faction_filter}, include_cmdr={include_cmdr}, clamp_negative={clamp_negative}")

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
                cmdr=cmdr if include_cmdr else None,
                count_voucher=count_voucher,
            )

    logger.info(f"Aggregation complete: produced {len(aggs)} groups (faction-per-system)")

    # Finalize: apply formula on SUM per group
    result: List[Dict[str, Any]] = []
    for (sysaddr, faction_name), agg in aggs.items():
        points = bounty_points_from_sum(agg.bounty_credits, clamp_negative=clamp_negative)

        item: Dict[str, Any] = {
            "system": agg.system,
            "systemaddress": str(agg.systemaddress) if agg.systemaddress is not None else None,
            "faction": agg.faction,
            "bounty_credits": agg.bounty_credits,
            "bounty_points": round(points, 6),
            "vouchers": agg.vouchers,
            "first_ts": agg.first_ts,
            "last_ts": agg.last_ts,
            "tickids": sorted(list(agg.tickids)) if agg.tickids else [],
        }

        if include_cmdr:
            # add cmdr points computed from their summed credits within this group (optional view)
            cmdr_out = {}
            for c, d in agg.cmdrs.items():
                c_points = bounty_points_from_sum(d["credits"], clamp_negative=clamp_negative)
                cmdr_out[c] = {
                    "credits": d["credits"],
                    "points": round(c_points, 6),
                }
            item["cmdrs"] = cmdr_out

        result.append(item)

    # Sort by bounty_points desc, then credits desc
    result.sort(key=lambda x: (x.get("bounty_points", 0.0), x.get("bounty_credits", 0)), reverse=True)
    # additional info: how many unique systems involved
    unique_systems = {r.get("system") for r in result if r.get("system")}
    logger.info(f"Aggregation covers {len(unique_systems)} unique system(s): {sorted(list(unique_systems))[:10]}")

    # Enrich results with population using the new helper
    try:
        # collect unique system names present in the result
        sysnames = sorted({(item.get("system") or None) for item in result if item.get("system")})
        pop_map = get_population_map_from_eddn(sysnames)

        for item in result:
            sysname = item.get("system")
            item["population"] = pop_map.get(sysname) if sysname else None
            # compute bgs_effect when population available and valid
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
        # fallback: ensure field exists
        for item in result:
            if "population" not in item:
                item["population"] = None
            if "bgs_effect" not in item:
                item["bgs_effect"] = None

    return result


# ---------------------------
# Bounty curve rendering
# ---------------------------

def render_bounty_curve_png(
    current_bounty_credits: int,
    population: Optional[int] = None,
    clamp_negative: bool = True,
    x_max_mcr: int = 100,
    title: str = "Bounties Bucket Curve",
) -> bytes:
    """
    Renders a curve chart (PNG) for:
      - bounty points vs bounty credits (in MCr)  [left y-axis]
      - bgs_effect (population-adjusted)         [right y-axis, if population is provided]
    and marks the current achieved value.
    """

    # x axis in credits (0..x_max_mcr MCr)
    x_vals: List[float] = []
    y_points: List[float] = []
    y_effect: List[float] = []

    has_pop = isinstance(population, (int, float)) and float(population) > 0

    # Use a small positive start to avoid log(0)
    for mcr in range(1, x_max_mcr + 1):
        credits = mcr * 1_000_000
        pts = bounty_points_from_sum(credits, clamp_negative=clamp_negative)

        x_vals.append(float(mcr))
        y_points.append(float(pts))

        if has_pop:
            try:
                y_effect.append(float(bgs_effect(float(pts), float(population))))
            except Exception:
                y_effect.append(0.0)

    # current point
    current_mcr = current_bounty_credits / 1_000_000.0
    current_points = bounty_points_from_sum(current_bounty_credits, clamp_negative=clamp_negative)

    current_effect = None
    if has_pop:
        try:
            current_effect = float(bgs_effect(float(current_points), float(population)))
        except Exception:
            current_effect = None

    fig = plt.figure(figsize=(12, 6), dpi=160)
    ax1 = fig.add_subplot(111)

    # Left axis: Bucket Points curve
    ax1.plot(x_vals, y_points, linewidth=2.5, color="black", label="Bucket Points")

    # Marker on left axis
    ax1.scatter([current_mcr], [current_points], s=140, color="red", zorder=5)

    # Optional right axis: BGS Effect curve
    ax2 = None
    if has_pop:
        ax2 = ax1.twinx()
        ax2.plot(x_vals, y_effect, linewidth=2.5, linestyle="--", color="gray", label="BGS Effect")

        # Marker on right axis
        if current_effect is not None:
            ax2.scatter([current_mcr], [current_effect], s=140, color="red", zorder=5)

    # Annotation (include effect if available)
    if current_effect is None:
        label_txt = f"{current_mcr:.2f} MCr\n{current_points:.2f} pts"
    else:
        label_txt = f"{current_mcr:.2f} MCr\n{current_points:.2f} pts\n{current_effect:.2f} eff"

    ax1.annotate(
        label_txt,
        xy=(current_mcr, current_points),
        xytext=(10, 10),
        textcoords="offset points",
        fontsize=10,
        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="black", alpha=0.9),
        arrowprops=dict(arrowstyle="->", color="black", lw=1.0),
    )

    # Titles/labels
    if has_pop:
        ax1.set_title(f"{title} (Pop: {int(population):,})")
    else:
        ax1.set_title(title)

    ax1.set_xlabel("Bounties Redeemed (M Cr)")
    ax1.set_ylabel("Bucket Points")
    ax1.grid(True, which="both", linewidth=0.6, alpha=0.6)

    # Legends: merge if we have ax2
    if ax2 is not None:
        ax2.set_ylabel("BGS Effect (population-scaled)")
        lines_1, labels_1 = ax1.get_legend_handles_labels()
        lines_2, labels_2 = ax2.get_legend_handles_labels()
        ax1.legend(lines_1 + lines_2, labels_1 + labels_2, loc="upper left")
    else:
        ax1.legend(loc="upper left")

    # Limits/headroom
    ax1.set_xlim(0, x_max_mcr)
    ax1.set_ylim(0, max(y_points) * 1.05 if y_points else 1)

    if ax2 is not None and y_effect:
        ax2.set_ylim(0, max(y_effect) * 1.05 if y_effect else 1)

    buf = BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()



# ---------------------------
# Flask integration
# ---------------------------

def register_bucket_v3_routes(app, db, require_api_key):
    """
    Registers:
        GET /api/bgs/v3/bucket/bounty
    """

    @app.route("/api/bgs/v3/bucket/bounty", methods=["GET"])
    @require_api_key
    def api_bgs_v3_bucket_bounty():
        logger = init_logger()
        # Query params
        period = request.args.get("period", "all")
        tickid = request.args.get("tickid")
        systemaddress = request.args.get("systemaddress")
        system = request.args.get("system")
        faction = request.args.get("faction")

        include_cmdr = _safe_bool(request.args.get("include_cmdr"), default=True)
        clamp_negative = _safe_bool(request.args.get("clamp"), default=True)

        logger.info(f"api_bgs_v3_bucket_bounty called with period={period}, tickid={tickid}, systemaddress={systemaddress}, system={system}, faction={faction}")

        data = evaluate_bounty_bucket(
            db=db,
            period=period,
            tickid=tickid,
            systemaddress=systemaddress,
            system=system,
            faction_filter=faction,
            include_cmdr=include_cmdr,
            clamp_negative=clamp_negative,
        )
        try:
            logger.info(f"api_bgs_v3_bucket_bounty returning {len(data)} rows")
        except Exception:
            pass

        return jsonify({
            "bucket": "combat",
            "metric": "bounty",
            "mode": "sum_then_formula",
            "period": period,
            "filters": {
                "tickid": tickid,
                "systemaddress": systemaddress,
                "system": system,
                "faction": faction,
                "include_cmdr": include_cmdr,
                "clamp_negative": clamp_negative,
            },
            "rows": data,
        })

    @app.route("/api/bgs/v3/bucket/bounty/discord", methods=["POST"])
    @require_api_key
    def api_bgs_v3_bucket_bounty_discord():
        # Wrapper: collect params and call top-level sender
        logger = init_logger()
        # Query params (same as API)
        period = request.args.get("period", request.form.get("period", "all"))
        tickid = request.args.get("tickid", request.form.get("tickid"))
        systemaddress = request.args.get("systemaddress", request.form.get("systemaddress"))
        system = request.args.get("system", request.form.get("system"))
        faction = request.args.get("faction", request.form.get("faction"))
        include_cmdr = _safe_bool(request.args.get("include_cmdr") or request.form.get("include_cmdr"), default=True)
        clamp_negative = _safe_bool(request.args.get("clamp") or request.form.get("clamp"), default=True)

        logger.info(f"api_bgs_v3_bucket_bounty_discord called: period={period}")

        calling_tenant = getattr(g, "tenant", None)
        if not calling_tenant:
            logger.error("No tenant found in request context (g.tenant missing)")
            return jsonify({"error": "Tenant not found in request context"}), 500

        results = _send_bounty_bucket_to_discord(
            app=app,
            db=db,
            period=period,
            tenant=calling_tenant,
            tickid=tickid,
            systemaddress=systemaddress,
            system=system,
            faction=faction,
            include_cmdr=include_cmdr,
            clamp=clamp_negative,
        )

        return jsonify({"results": results})


    @app.route("/api/bgs/v3/bucket/bounty/chart", methods=["GET"])
    @require_api_key
    def api_bgs_v3_bucket_bounty_chart():
        period = request.args.get("period", "all")
        tickid = request.args.get("tickid")
        systemaddress = request.args.get("systemaddress")
        system = request.args.get("system")
        faction = request.args.get("faction")  # required to pick one line reliably

        clamp_negative = _safe_bool(request.args.get("clamp"), default=True)
        x_max_mcr = int(request.args.get("x_max_mcr", "100"))

        # Evaluate (same logic), then select the requested row (or best match)
        rows = evaluate_bounty_bucket(
            db=db,
            period=period,
            tickid=tickid,
            systemaddress=systemaddress,
            system=system,
            faction_filter=faction,
            include_cmdr=False,
            clamp_negative=clamp_negative,
        )

        if not rows:
            return jsonify({"error": "No data found for the given filters."}), 404

        # If faction provided -> rows should already be single-group; otherwise take top row
        row = rows[0]
        current_credits = int(row.get("bounty_credits", 0))

        title = f"Bounties Curve – {row.get('system','?')} / {row.get('faction','?')}"

        pop = row.get("population")
        pop = int(pop) if isinstance(pop, (int, float)) and int(pop) > 0 else None

        png_bytes = render_bounty_curve_png(
            current_bounty_credits=current_credits,
            population=pop,
            clamp_negative=clamp_negative,
            x_max_mcr=x_max_mcr,
            title=title,
        )

        return Response(png_bytes, mimetype="image/png")


    # end register_bucket_v3_routes


# Internal implementation that accepts filter parameters (kept for the endpoint and programmatic calls)
def _send_bounty_bucket_to_discord(
    app,
    db,
    period="all",
    tenant=None,
    tickid=None,
    systemaddress=None,
    system=None,
    faction=None,
    include_cmdr=True,
    clamp=True
):
    logger = init_logger()

    # Generate data using same evaluation function
    data = evaluate_bounty_bucket(
        db=db,
        period=period,
        tickid=tickid,
        systemaddress=systemaddress,
        system=system,
        faction_filter=faction,
        include_cmdr=include_cmdr,
        clamp_negative=clamp,
    )

    # Partition results by system
    systems = {}
    for row in data:
        sysname = row.get("system") or "Unknown"
        systems.setdefault(sysname, []).append(row)

    # Determine tenants to send to
    tenants = [tenant] if tenant is not None else get_tenants()

    logger.info(f"Targeting {len(tenants)} tenant(s) for discord dispatch")
    results = []

    for t in tenants:
        # webhook_url = get_discord_webhook(t, "bullis")
        webhook_url = get_discord_webhook(t, "bgs")
        if not webhook_url:
            logger.warning(f"No 'bullis' webhook for tenant {t.get('name')}")
            results.append({"tenant": t.get("name"), "status": "no_webhook"})
            continue

        period_label = period if period != "all" else "All Time"
        tick_label = tickid if tickid else period_label

        header_msg = (
            "## BGS v3 - 'The 4 Bucket' Evaluation\n"
            "This evaluation applies the **BGS Bucket Model** by aggregating bounty credits per system and faction first and then converting the total into influence points using a logarithmic function, reflecting diminishing returns and soft caps. The approach follows the **four-bucket concept (combat, trade, exploration, missions)** described in *The BGS Guide* (see *“The Bucket Model”*, page 36 ff.), where balanced activity across multiple buckets is more effective than focusing on a single one (<https://sinc.science/bgsguide.pdf>).\n\n"
            "Note: This is **Phase 1** focusing on the **Combat/Bounty Bucket** using RedeemVoucherEvents of type 'bounty'.\n\n"
            f"Tenant: **{t.get('name')}**\n"
            f"Period/Tick: **{tick_label}**\n"
        )

        header_sent = False

        for system_name, rows in systems.items():
            # Sort factions by impact
            rows_sorted = sorted(
                rows,
                key=lambda r: (
                    float(r.get("bounty_points", 0.0) or 0.0),
                    int(r.get("bounty_credits", 0) or 0),
                ),
                reverse=True,
            )

            top_row = rows_sorted[0] if rows_sorted else None
            total_credits = sum(r.get("bounty_credits", 0) for r in rows_sorted)

            lines = []
            if not header_sent:
                lines.append(header_msg)
                header_sent = True
            else:
                lines.append("\u200B")

            lines.append(f"💰🏴‍☠️ **{system_name}**")
            lines.append(f"Total bounty credits: **{total_credits:,} Cr**")
            # Show population for the system (taken from first available row). If missing or 0, show hint.
            pop_val = None
            for _r in rows_sorted:
                p = _r.get("population")
                if p is not None:
                    pop_val = p
                    break

            if pop_val is None or (isinstance(pop_val, (int, float)) and int(pop_val) == 0):
                lines.append("Population: **unknown / not available**")
                lines.append("_Note: Population not determined — BGS Effect values cannot be calculated._")
            else:
                try:
                    lines.append(f"Population: **{int(pop_val):,}**")
                except Exception:
                    lines.append("Population: **unknown**")
            lines.append("```text")
            # Single header line including BGS Effect column
            lines.append(f"{'Faction':<32} | {'Credits':>14} | {'Points':>8} | {'BGS Eff':>9}")
            lines.append("-" * 74)

            for r in rows_sorted:
                fac = (r.get("faction") or "?")[:32]
                cr = int(r.get("bounty_credits", 0) or 0)
                pts = float(r.get("bounty_points", 0.0) or 0.0)
                # bgs_effect comes from evaluate_bounty_bucket; may be None
                bgs_val = r.get("bgs_effect")
                if bgs_val is None:
                    bgs_str = "-"
                else:
                    try:
                        bgs_str = f"{float(bgs_val):.2f}"
                    except Exception:
                        bgs_str = "-"

                lines.append(f"{fac:<32} | {cr:>14,} | {pts:>8.2f} | {bgs_str:>9}")

            lines.append("```")

            content = "\n".join(lines) # + "\n\u200B"

            # ---------- Render chart for top faction ----------
            file_payload = None
            if top_row:
                try:
                    current_credits = int(top_row.get("bounty_credits", 0) or 0)
                    title = f"Bounties Curve – {system_name} / {top_row.get('faction', '?')}"
                    pop = top_row.get("population")
                    pop = int(pop) if isinstance(pop, (int, float)) and int(pop) > 0 else None
                    png_bytes = render_bounty_curve_png(
                        current_bounty_credits=current_credits,
                        population=pop,
                        clamp_negative=clamp,
                        x_max_mcr=100,
                        title=title,
                    )
                    safe_sys = "".join(
                        c for c in (system_name or "system")
                        if c.isalnum() or c in ("-", "_")
                    )[:40]
                    filename = f"bounty_bucket_{safe_sys}.png"
                    file_payload = (filename, png_bytes, "image/png")
                except Exception as e:
                    logger.warning(f"Chart rendering failed for {system_name}: {e}")

            # ---------- Send Discord message ----------
            try:
                if file_payload:
                    # IMPORTANT: For webhook + file upload, Discord expects JSON in "payload_json"
                    files = {"file": file_payload}
                    payload = {
                        "content": content,
                        "allowed_mentions": {"parse": []},
                    }
                    resp = requests.post(
                        webhook_url,
                        data={"payload_json": json.dumps(payload)},
                        files=files,
                        timeout=20
                    )
                else:
                    resp = requests.post(
                        webhook_url,
                        json={
                            "content": content,
                            "allowed_mentions": {"parse": []},
                        },
                        timeout=20
                    )

                if resp.status_code not in (200, 204):
                    logger.error(f"Discord webhook error ({resp.status_code}): {resp.text}")
                    results.append({
                        "tenant": t.get("name"),
                        "system": system_name,
                        "status": "discord_error",
                        "code": resp.status_code,
                    })
                else:
                    results.append({
                        "tenant": t.get("name"),
                        "system": system_name,
                        "status": "sent",
                        "chart_attached": bool(file_payload),
                    })

            except Exception as e:
                logger.exception(f"Discord send failed for tenant={t.get('name')} system={system_name}")
                results.append({
                    "tenant": t.get("name"),
                    "system": system_name,
                    "status": "exception",
                    "error": str(e),
                })

    return results

