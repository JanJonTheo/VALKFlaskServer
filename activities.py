from flask import Blueprint, request, jsonify, g
from models import db
from models import Activity, System, Faction
from sqlalchemy import text
import json

activities_bp = Blueprint("activities", __name__)

# API-Endpunkt zum Empfangen und Speichern von Aktivitäten
@activities_bp.route("/activities", methods=["PUT"])
def put_activities():
    # Tenant-DB setzen wie bei GET-Endpunkten
    from app import get_tenant_by_apikey, set_tenant_db_config
    apikey = request.headers.get("apikey")
    tenant = get_tenant_by_apikey(apikey)
    if not tenant:
        return jsonify({"error": "Unauthorized: Invalid API key"}), 401
    g.tenant = tenant
    set_tenant_db_config(tenant)
    if hasattr(g, "tenant_db_error"):
        return jsonify({"error": f"Tenant-Datenbank nicht erreichbar: {g.tenant_db_error}"}), 500

    try:
        activity_data = request.get_json()
        validated = activity_data

        # Suche nach vorhandenem Activity-Datensatz
        activity = db.session.query(Activity).filter_by(
            tickid=validated['tickid'],
            cmdr=validated.get('cmdr')
        ).first()
        if not activity:
            activity = Activity(
                tickid=validated['tickid'],
                ticktime=validated['ticktime'],
                timestamp=validated['timestamp'],
                cmdr=validated.get('cmdr')
            )
            db.session.add(activity)
        else:
            activity.ticktime = validated['ticktime']
            activity.timestamp = validated['timestamp']

        # Systeme aktualisieren/hinzufügen
        for sys in validated['systems']:
            system = db.session.query(System).filter_by(
                activity_id=activity.id,
                name=sys['name'],
                address=sys['address']
            ).first()
            if not system:
                system = System(
                    name=sys['name'],
                    address=sys['address']
                )
                activity.systems.append(system)
            # Felder aktualisieren
            if 'twkills' in sys:
                system.twkills = json.dumps(sys['twkills'])
            if 'twsandr' in sys:
                system.twsandr = json.dumps(sys['twsandr'])
            if 'twreactivate' in sys:
                system.twreactivate = sys['twreactivate']

            # Fraktionen aktualisieren/hinzufügen
            for fac in sys['factions']:
                faction = db.session.query(Faction).filter_by(
                    system_id=system.id,
                    name=fac['name']
                ).first()
                if not faction:
                    faction = Faction(
                        name=fac['name'],
                        state=fac['state'],
                        bvs=fac.get('bvs', 0),
                        cbs=fac.get('cbs', 0),
                        exobiology=fac.get('exobiology', 0),
                        exploration=fac.get('exploration', 0),
                        scenarios=fac.get('scenarios', 0),
                        infprimary=fac.get('infprimary', 0),
                        infsecondary=fac.get('infsecondary', 0),
                        missionfails=fac.get('missionfails', 0),
                        murdersground=fac.get('murdersground', 0),
                        murdersspace=fac.get('murdersspace', 0),
                        tradebm=fac.get('tradebm', 0)
                    )
                    system.factions.append(faction)
                else:
                    faction.state = fac['state']
                    faction.bvs = fac.get('bvs', 0)
                    faction.cbs = fac.get('cbs', 0)
                    faction.exobiology = fac.get('exobiology', 0)
                    faction.exploration = fac.get('exploration', 0)
                    faction.scenarios = fac.get('scenarios', 0)
                    faction.infprimary = fac.get('infprimary', 0)
                    faction.infsecondary = fac.get('infsecondary', 0)
                    faction.missionfails = fac.get('missionfails', 0)
                    faction.murdersground = fac.get('murdersground', 0)
                    faction.murdersspace = fac.get('murdersspace', 0)
                    faction.tradebm = fac.get('tradebm', 0)
                # Neue Felder für Faction
                if 'stations' in fac:
                    faction.stations = json.dumps(fac['stations'])
                if 'czground' in fac:
                    faction.czground = json.dumps(fac['czground'])
                if 'czspace' in fac:
                    faction.czspace = json.dumps(fac['czspace'])
                if 'tradebuy' in fac:
                    faction.tradebuy = json.dumps(fac['tradebuy'])
                if 'tradesell' in fac:
                    faction.tradesell = json.dumps(fac['tradesell'])
                if 'sandr' in fac:
                    faction.sandr = json.dumps(fac['sandr'])

        db.session.commit()

        return jsonify({"status": "activity saved"}), 200
    except Exception as e:
        db.session.rollback()
        import logging
        logging.getLogger(__name__).error(f"Activity processing error: {str(e)}")
        return jsonify({"error": str(e)}), 400


# Funktion zum Abrufen von Aktivitäten mit Filtern
def get_activities(tick_filter=None, cmdr=None, system_name=None, faction_name=None):

    query = db.session.query(Activity)

    # Filter anwenden
    if tick_filter == "current":
        tickid_row = db.session.execute(text("SELECT tickid FROM activity ORDER BY timestamp DESC LIMIT 1")).fetchone()
        if tickid_row and tickid_row[0]:
            query = query.filter(Activity.tickid == tickid_row[0])
    elif tick_filter == "last":
        tickids = db.session.execute(text("SELECT DISTINCT tickid FROM activity ORDER BY timestamp DESC LIMIT 2")).fetchall()
        if len(tickids) == 2:
            query = query.filter(Activity.tickid == tickids[1][0])
    elif tick_filter:
        query = query.filter(Activity.tickid == tick_filter)
    if cmdr:
        query = query.filter(Activity.cmdr == cmdr)

    activities = query.all()
    result = []
    for activity in activities:
        activity_dict = {
            "tickid": activity.tickid,
            "ticktime": activity.ticktime,
            "timestamp": activity.timestamp,
            "cmdr": activity.cmdr,
            "systems": []
        }
        for system in activity.systems:
            if system_name and system.name != system_name:
                continue
            system_dict = {
                "name": system.name,
                "address": system.address,
                "twkills": json.loads(system.twkills) if system.twkills else None,
                "twsandr": json.loads(system.twsandr) if system.twsandr else None,
                "twreactivate": system.twreactivate,
                "factions": []
            }
            for faction in system.factions:
                if faction_name and faction.name != faction_name:
                    continue
                faction_dict = {
                    "name": faction.name,
                    "state": faction.state,
                    "bvs": faction.bvs,
                    "cbs": faction.cbs,
                    "exobiology": faction.exobiology,
                    "exploration": faction.exploration,
                    "scenarios": faction.scenarios,
                    "infprimary": faction.infprimary,
                    "infsecondary": faction.infsecondary,
                    "missionfails": faction.missionfails,
                    "murdersground": faction.murdersground,
                    "murdersspace": faction.murdersspace,
                    "tradebm": faction.tradebm,
                    "stations": json.loads(faction.stations) if faction.stations else None,
                    "czground": json.loads(faction.czground) if faction.czground else None,
                    "czspace": json.loads(faction.czspace) if faction.czspace else None,
                    "tradebuy": json.loads(faction.tradebuy) if faction.tradebuy else None,
                    "tradesell": json.loads(faction.tradesell) if faction.tradesell else None,
                    "sandr": json.loads(faction.sandr) if faction.sandr else None
                }
                system_dict["factions"].append(faction_dict)
            activity_dict["systems"].append(system_dict)
        result.append(activity_dict)
    return result


# Aktivitäten-API-Endpunkt mit Filtermöglichkeiten
@activities_bp.route("/api/activities", methods=["GET"])
def activities_api():
    # API-Key aus Header holen und Tenant setzen
    from app import get_tenant_by_apikey, set_tenant_db_config
    apikey = request.headers.get("apikey")
    tenant = get_tenant_by_apikey(apikey)
    if not tenant:
        return jsonify({"error": "Unauthorized: Invalid API key"}), 401
    g.tenant = tenant
    set_tenant_db_config(tenant)
    if hasattr(g, "tenant_db_error"):
        return jsonify({"error": f"Tenant-Datenbank nicht erreichbar: {g.tenant_db_error}"}), 500

    # Filter aus Query-Parametern
    period = request.args.get("period")  # ct|lt|current|last|<tickid>
    cmdr = request.args.get("cmdr")
    system_name = request.args.get("system")
    faction_name = request.args.get("faction")

    try:
        tick_filter = _resolve_tick_filter(period)
        result = get_activities(
            tick_filter=tick_filter,
            cmdr=cmdr,
            system_name=system_name,
            faction_name=faction_name
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Hilfsfunktion zur robusten Summierung von JSON-Feldern
def _safe_sum_from_json(obj):
    """
    Summiert Zahlen in JSON-Feldern robust:
    - dict -> Summe numerischer Werte
    - list -> Anzahl Elemente oder Summe numerischer Elemente
    - int/float -> Wert selbst
    - sonst -> 0
    """
    if obj is None:
        return 0
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, dict):
        return sum(v for v in obj.values() if isinstance(v, (int, float)))
    if isinstance(obj, list):
        # Falls Liste numerisch -> Summe, sonst Anzahl
        numeric = [v for v in obj if isinstance(v, (int, float))]
        return sum(numeric) if numeric else len(obj)
    return 0


# Hilfsfunktion zur Zählung von CZ-Leveln
def _cz_count_levels(obj):
    """
    Ermittelt (L, M, H) für CZ-Felder robust.
    Unterstützt:
    - {"L": 1, "M": 2, "H": 0}
    - [{"level":"L","count":1}, ...]
    - einfache Listen (zählt als Länge, in L einsortiert)
    """
    L = M = H = 0
    if obj is None:
        return L, M, H
    if isinstance(obj, dict):
        L = obj.get("low", 0) if isinstance(obj.get("low"), (int, float)) else 0
        M = obj.get("medium", 0) if isinstance(obj.get("medium"), (int, float)) else 0
        H = obj.get("high", 0) if isinstance(obj.get("high"), (int, float)) else 0
        return int(L), int(M), int(H)
    if isinstance(obj, list):
        # Varianten: Liste von dicts oder gemischte Werte
        for item in obj:
            if isinstance(item, dict):
                lvl = str(item.get("level", "")).upper()
                cnt = item.get("count", 1)
                if not isinstance(cnt, (int, float)):
                    cnt = 1
                if lvl == "L":
                    L += int(cnt)
                elif lvl == "M":
                    M += int(cnt)
                elif lvl == "H":
                    H += int(cnt)
            else:
                # Sonst als "L" zählen
                L += 1
        return L, M, H
    # Andere Typen: alles auf L
    return 1, 0, 0


# Hilfsfunktion zur Auflösung von Tick-Filtern
def _resolve_tick_filter(tick_filter: str):
    """
    Unterstützt:
    - 'current', 'last' (wie bestehend)
    - 'ct', 'lt' (Kurzformen)
    - konkrete tickid (string)
    - None -> kein Filter
    """
    if not tick_filter:
        return None

    norm = tick_filter.strip().lower()
    if norm in {"ct", "current"}:
        row = db.session.execute(
            text("SELECT tickid FROM activity ORDER BY timestamp DESC LIMIT 1")
        ).fetchone()
        return row[0] if row and row[0] else None

    if norm in {"lt", "last"}:
        rows = db.session.execute(
            text("SELECT DISTINCT tickid FROM activity ORDER BY timestamp DESC LIMIT 2")
        ).fetchall()
        return rows[1][0] if len(rows) == 2 else None

    # sonst als konkrete tickid behandeln
    return tick_filter


# Hilfsfunktion zur robusten JSON-Konvertierung
def _coerce_json(obj):
    """String mit JSON -> dict/list, sonst unverändert zurückgeben, bei Fehler None."""
    if obj is None:
        return None
    if isinstance(obj, (dict, list)):
        return obj
    if isinstance(obj, str):
        try:
            import json
            return json.loads(obj)
        except Exception:
            return None
    return None


# Hilfsfunktion zur Aufsummierung von TradeBuy
def _sum_tradebuy(tb):
    """
    Erwartete Struktur:
      { "high": {"items": int, "value": int}, "low": {"items": int, "value": int} }
    Rückgabe: dict mit High/Low und Total (Items/Value)
    """
    tb = _coerce_json(tb) or {}
    high = tb.get("high") or {}
    low  = tb.get("low")  or {}

    hi_items = int(high.get("items") or 0)
    hi_value = int(high.get("value") or 0)
    lo_items = int(low.get("items") or 0)
    lo_value = int(low.get("value") or 0)

    return {
        "buy_items_high": hi_items,
        "buy_value_high": hi_value,
        "buy_items_low": lo_items,
        "buy_value_low": lo_value,
        "buy_items_total": hi_items + lo_items,
        "buy_value_total": hi_value + lo_value,
    }


# Hilfsfunktion zur Aufsummierung von TradeSell
def _sum_tradesell(ts):
    """
    Erwartete Struktur:
      {
        "high": {"items": int, "profit": int, "value": int},
        "low":  {"items": int, "profit": int, "value": int},
        "zero": {"items": int, "profit": int, "value": int}
      }
    Rückgabe: dict mit High/Low/Zero und Total (Items/Value/Profit)
    """
    ts = _coerce_json(ts) or {}
    def _blk(name):
        b = ts.get(name) or {}
        return (int(b.get("items") or 0),
                int(b.get("value") or 0),
                int(b.get("profit") or 0))

    hi_i, hi_v, hi_p = _blk("high")
    lo_i, lo_v, lo_p = _blk("low")
    ze_i, ze_v, ze_p = _blk("zero")

    return {
        # High
        "sell_items_high":  hi_i, "sell_value_high":  hi_v, "sell_profit_high":  hi_p,
        # Low
        "sell_items_low":   lo_i, "sell_value_low":   lo_v, "sell_profit_low":   lo_p,
        # Zero
        "sell_items_zero":  ze_i, "sell_value_zero":  ze_v, "sell_profit_zero":  ze_p,
        # Totals
        "sell_items_total": hi_i + lo_i + ze_i,
        "sell_value_total": hi_v + lo_v + ze_v,
        "sell_profit_total": hi_p + lo_p + ze_p,
    }


# API-Endpunkt für systemweite/factionweite Aktivitätszusammenfassung
@activities_bp.route("/api/activities/system-summary", methods=["GET"])
def activities_system_summary_api():
    """
    Aggregiert Aktivitäten:
      - group=system  (Default): 1 Zeile je System (wie bisher)
      - group=faction: 1 Zeile je Minor Faction innerhalb eines Systems
      - group=cmdr: 1 Zeile je Cmdr, System und Tick

    Filter:
      - period=ct|lt|current|last|<tickid>  (einheitlich wie /api/activities)
      - system=<Systemname>

    Ausgabe: flache, tabellarische Struktur.
    """
    from app import get_tenant_by_apikey, set_tenant_db_config

    # --- Tenant prüfen ---
    apikey = request.headers.get("apikey")
    tenant = get_tenant_by_apikey(apikey)
    if not tenant:
        return jsonify({"error": "Unauthorized: Invalid API key"}), 401
    g.tenant = tenant
    set_tenant_db_config(tenant)
    if hasattr(g, "tenant_db_error"):
        return jsonify({"error": f"Tenant-Datenbank nicht erreichbar: {g.tenant_db_error}"}), 500

    # --- Filter ---
    period = request.args.get("period")        # ct|lt|current|last|<tickid>
    tick_alias = request.args.get("tick")      # Alias (rückwärts-kompatibel)
    system_name = request.args.get("system")
    group = (request.args.get("group") or "system").strip().lower()  # "system" | "faction" | "cmdr"

    try:
        effective_tickid = _resolve_tick_filter(period or tick_alias)

        # Basis-Query
        q = db.session.query(Activity).join(Activity.systems)
        if effective_tickid:
            q = q.filter(Activity.tickid == effective_tickid)
        if system_name:
            q = q.filter(System.name == system_name)

        acts = q.all()

        # ---------------------------
        # Aggregation: CMD-LEVEL
        # ---------------------------
        def init_cmdr_bucket(cmdr: str, sysname: str, tickid: str, ticktime: str):
            return {
                "cmdr": cmdr,
                "system": sysname,
                "tickid": tickid,
                "ticktime": ticktime,
                "total_inf_primary": 0,
                "total_inf_secondary": 0,
                "total_trade_bm": 0,
                "total_bvs": 0,
                "total_exploration": 0,
                "total_exobiology": 0,
                "total_cbs": 0,
                "total_mission_fails": 0,
                "total_murders_ground": 0,
                "total_murders_ship": 0,
                "total_sandr": 0,
                "cz_space_L": 0, "cz_space_M": 0, "cz_space_H": 0,
                "cz_ground_L": 0, "cz_ground_M": 0, "cz_ground_H": 0,
                # Trade BUY totals
                "buy_items_high": 0, "buy_value_high": 0,
                "buy_items_low": 0,  "buy_value_low": 0,
                "buy_items_total": 0, "buy_value_total": 0,
                # Trade SELL totals
                "sell_items_high": 0, "sell_value_high": 0, "sell_profit_high": 0,
                "sell_items_low": 0,  "sell_value_low": 0,  "sell_profit_low": 0,
                "sell_items_zero": 0, "sell_value_zero": 0, "sell_profit_zero": 0,
                "sell_items_total": 0, "sell_value_total": 0, "sell_profit_total": 0,
            }

        if group == "cmdr":
            per = {}  # key = (cmdr, system, tickid)
            for act in acts:
                for sys in act.systems:
                    if system_name and sys.name != system_name:
                        continue
                    key = (act.cmdr, sys.name, act.tickid)
                    if key not in per:
                        per[key] = init_cmdr_bucket(act.cmdr, sys.name, act.tickid, act.ticktime)
                    bucket = per[key]
                    for fac in sys.factions:
                        bucket["total_inf_primary"]   += int(fac.infprimary or 0)
                        bucket["total_inf_secondary"] += int(fac.infsecondary or 0)
                        bucket["total_trade_bm"]      += int(fac.tradebm or 0)
                        bucket["total_bvs"]           += int(fac.bvs or 0)
                        bucket["total_exploration"]   += int(fac.exploration or 0)
                        bucket["total_exobiology"]    += int(fac.exploration or 0)
                        bucket["total_cbs"]           += int(fac.cbs or 0)
                        bucket["total_mission_fails"] += int(fac.missionfails or 0)
                        bucket["total_murders_ground"] += int(fac.murdersground or 0)
                        bucket["total_murders_ship"]   += int(fac.murdersspace or 0)
                        # S&R
                        try:
                            sandr = fac.sandr
                            if isinstance(sandr, str):
                                import json
                                sandr = json.loads(sandr)
                            bucket["total_sandr"] += _safe_sum_from_json(sandr)
                        except Exception:
                            pass
                        # CZ Space
                        try:
                            czs = fac.czspace
                            if isinstance(czs, str):
                                import json
                                czs = json.loads(czs)
                            l, m, h = _cz_count_levels(czs)
                            bucket["cz_space_L"] += l
                            bucket["cz_space_M"] += m
                            bucket["cz_space_H"] += h
                        except Exception:
                            pass
                        # CZ Ground
                        try:
                            czg = fac.czground
                            if isinstance(czg, str):
                                import json
                                czg = json.loads(czg)
                            l, m, h = _cz_count_levels(czg)
                            bucket["cz_ground_L"] += l
                            bucket["cz_ground_M"] += m
                            bucket["cz_ground_H"] += h
                        except Exception:
                            pass
                        # Trade BUY
                        try:
                            buy = _sum_tradebuy(fac.tradebuy)
                            bucket["buy_items_high"]  += buy["buy_items_high"]
                            bucket["buy_value_high"]  += buy["buy_value_high"]
                            bucket["buy_items_low"]   += buy["buy_items_low"]
                            bucket["buy_value_low"]   += buy["buy_value_low"]
                            bucket["buy_items_total"] += buy["buy_items_total"]
                            bucket["buy_value_total"] += buy["buy_value_total"]
                        except Exception:
                            pass
                        # Trade SELL
                        try:
                            sell = _sum_tradesell(fac.tradesell)
                            bucket["sell_items_high"]   += sell["sell_items_high"]
                            bucket["sell_value_high"]   += sell["sell_value_high"]
                            bucket["sell_profit_high"]  += sell["sell_profit_high"]
                            bucket["sell_items_low"]    += sell["sell_items_low"]
                            bucket["sell_value_low"]    += sell["sell_value_low"]
                            bucket["sell_profit_low"]   += sell["sell_profit_low"]
                            bucket["sell_items_zero"]   += sell["sell_items_zero"]
                            bucket["sell_value_zero"]   += sell["sell_value_zero"]
                            bucket["sell_profit_zero"]  += sell["sell_profit_zero"]
                            bucket["sell_items_total"]  += sell["sell_items_total"]
                            bucket["sell_value_total"]  += sell["sell_value_total"]
                            bucket["sell_profit_total"] += sell["sell_profit_total"]
                        except Exception:
                            pass
            rows = list(per.values())
            rows.sort(key=lambda r: (r.get("cmdr",""), r["system"], r["tickid"]))
            return jsonify(rows), 200

        # ---------------------------
        # Aggregation: SYSTEM-LEVEL
        # ---------------------------
        def init_system_bucket(sysname: str, tickid: str, ticktime: str):
            return {
                "system": sysname,
                "tickid": tickid,
                "ticktime": ticktime,
                "total_inf_primary": 0,
                "total_inf_secondary": 0,
                "total_trade_bm": 0,
                "total_bvs": 0,
                "total_exploration": 0,
                "total_exobiology": 0,
                "total_cbs": 0,
                "total_mission_fails": 0,
                "total_murders_ground": 0,
                "total_murders_ship": 0,
                "total_sandr": 0,
                "cz_space_L": 0, "cz_space_M": 0, "cz_space_H": 0,
                "cz_ground_L": 0, "cz_ground_M": 0, "cz_ground_H": 0,
                # Trade BUY totals
                "buy_items_high": 0, "buy_value_high": 0,
                "buy_items_low": 0,  "buy_value_low": 0,
                "buy_items_total": 0, "buy_value_total": 0,
                # Trade SELL totals
                "sell_items_high": 0, "sell_value_high": 0, "sell_profit_high": 0,
                "sell_items_low": 0,  "sell_value_low": 0,  "sell_profit_low": 0,
                "sell_items_zero": 0, "sell_value_zero": 0, "sell_profit_zero": 0,
                "sell_items_total": 0, "sell_value_total": 0, "sell_profit_total": 0,
            }

        # ---------------------------
        # Aggregation: FACTION-LEVEL
        # ---------------------------
        def init_faction_bucket(sysname: str, facname: str, tickid: str, ticktime: str, state: str):
            b = init_system_bucket(sysname, tickid, ticktime)
            b.update({
                "faction": facname,
                "state": state or "None",
                # Optional: falls dein Modell einen Influence-%-Wert trägt – sonst bleibt None
                "influence": None,
            })
            return b

        if group == "faction":
            per = {}  # key = (system, faction, tickid)

            for act in acts:
                for sys in act.systems:
                    if system_name and sys.name != system_name:
                        continue
                    for fac in sys.factions:
                        key = (sys.name, fac.name, act.tickid)
                        if key not in per:
                            per[key] = init_faction_bucket(sys.name, fac.name, act.tickid, act.ticktime, fac.state)

                        bucket = per[key]

                        # --- INF / generische Zähler ---
                        bucket["total_inf_primary"]   += int(fac.infprimary or 0)
                        bucket["total_inf_secondary"] += int(fac.infsecondary or 0)
                        bucket["total_trade_bm"]      += int(fac.tradebm or 0)
                        bucket["total_bvs"]           += int(fac.bvs or 0)
                        bucket["total_exploration"]   += int(fac.exploration or 0)
                        bucket["total_exobiology"]    += int(fac.exploration or 0)
                        bucket["total_cbs"]           += int(fac.cbs or 0)
                        bucket["total_mission_fails"] += int(fac.missionfails or 0)
                        bucket["total_murders_ground"] += int(fac.murdersground or 0)
                        bucket["total_murders_ship"]   += int(fac.murdersspace or 0)

                        # --- S&R (robust) ---
                        try:
                            sandr = fac.sandr
                            if isinstance(sandr, str):
                                import json
                                sandr = json.loads(sandr)
                            bucket["total_sandr"] += _safe_sum_from_json(sandr)
                        except Exception:
                            pass

                        # --- CZ Space ---
                        try:
                            czs = fac.czspace
                            if isinstance(czs, str):
                                import json
                                czs = json.loads(czs)
                            l, m, h = _cz_count_levels(czs)
                            bucket["cz_space_L"] += l
                            bucket["cz_space_M"] += m
                            bucket["cz_space_H"] += h
                        except Exception:
                            pass

                        # --- CZ Ground ---
                        try:
                            czg = fac.czground
                            if isinstance(czg, str):
                                import json
                                czg = json.loads(czg)
                            l, m, h = _cz_count_levels(czg)
                            bucket["cz_ground_L"] += l
                            bucket["cz_ground_M"] += m
                            bucket["cz_ground_H"] += h
                        except Exception:
                            pass

                        # --- Trade BUY ---
                        try:
                            buy = _sum_tradebuy(fac.tradebuy)
                            bucket["buy_items_high"]  += buy["buy_items_high"]
                            bucket["buy_value_high"]  += buy["buy_value_high"]
                            bucket["buy_items_low"]   += buy["buy_items_low"]
                            bucket["buy_value_low"]   += buy["buy_value_low"]
                            bucket["buy_items_total"] += buy["buy_items_total"]
                            bucket["buy_value_total"] += buy["buy_value_total"]
                        except Exception:
                            pass

                        # --- Trade SELL ---
                        try:
                            sell = _sum_tradesell(fac.tradesell)
                            bucket["sell_items_high"]   += sell["sell_items_high"]
                            bucket["sell_value_high"]   += sell["sell_value_high"]
                            bucket["sell_profit_high"]  += sell["sell_profit_high"]

                            bucket["sell_items_low"]    += sell["sell_items_low"]
                            bucket["sell_value_low"]    += sell["sell_value_low"]
                            bucket["sell_profit_low"]   += sell["sell_profit_low"]

                            bucket["sell_items_zero"]   += sell["sell_items_zero"]
                            bucket["sell_value_zero"]   += sell["sell_value_zero"]
                            bucket["sell_profit_zero"]  += sell["sell_profit_zero"]

                            bucket["sell_items_total"]  += sell["sell_items_total"]
                            bucket["sell_value_total"]  += sell["sell_value_total"]
                            bucket["sell_profit_total"] += sell["sell_profit_total"]
                        except Exception:
                            pass

            rows = list(per.values())
            rows.sort(key=lambda r: (r["system"], r.get("faction",""), r["tickid"]))
            return jsonify(rows), 200

        # ---------------------------
        # Default: group=system (bestehend)
        # ---------------------------
        per = {}  # key = (system, tickid)

        for act in acts:
            for sys in act.systems:
                if system_name and sys.name != system_name:
                    continue

                key = (sys.name, act.tickid)
                if key not in per:
                    per[key] = init_system_bucket(sys.name, act.tickid, act.ticktime)

                bucket = per[key]

                for fac in sys.factions:
                    # INF & generische Zähler
                    bucket["total_inf_primary"]   += int(fac.infprimary or 0)
                    bucket["total_inf_secondary"] += int(fac.infsecondary or 0)
                    bucket["total_trade_bm"]      += int(fac.tradebm or 0)
                    bucket["total_bvs"]           += int(fac.bvs or 0)
                    bucket["total_exploration"]   += int(fac.exploration or 0)
                    bucket["total_exobiology"]    += int(fac.exploration or 0)
                    bucket["total_cbs"]           += int(fac.cbs or 0)
                    bucket["total_mission_fails"] += int(fac.missionfails or 0)
                    bucket["total_murders_ground"] += int(fac.murdersground or 0)
                    bucket["total_murders_ship"]   += int(fac.murdersspace or 0)

                    # S&R
                    try:
                        sandr = fac.sandr
                        if isinstance(sandr, str):
                            import json
                            sandr = json.loads(sandr)
                        bucket["total_sandr"] += _safe_sum_from_json(sandr)
                    except Exception:
                        pass

                    # CZ Space
                    try:
                        czs = fac.czspace
                        if isinstance(czs, str):
                            import json
                            czs = json.loads(czs)
                        l, m, h = _cz_count_levels(czs)
                        bucket["cz_space_L"] += l
                        bucket["cz_space_M"] += m
                        bucket["cz_space_H"] += h
                    except Exception:
                        pass

                    # CZ Ground
                    try:
                        czg = fac.czground
                        if isinstance(czg, str):
                            import json
                            czg = json.loads(czg)
                        l, m, h = _cz_count_levels(czg)
                        bucket["cz_ground_L"] += l
                        bucket["cz_ground_M"] += m
                        bucket["cz_ground_H"] += h
                    except Exception:
                        pass

                    # Trade BUY
                    try:
                        buy = _sum_tradebuy(fac.tradebuy)
                        bucket["buy_items_high"]  += buy["buy_items_high"]
                        bucket["buy_value_high"]  += buy["buy_value_high"]
                        bucket["buy_items_low"]   += buy["buy_items_low"]
                        bucket["buy_value_low"]   += buy["buy_value_low"]
                        bucket["buy_items_total"] += buy["buy_items_total"]
                        bucket["buy_value_total"] += buy["buy_value_total"]
                    except Exception:
                        pass

                    # Trade SELL
                    try:
                        sell = _sum_tradesell(fac.tradesell)
                        bucket["sell_items_high"]   += sell["sell_items_high"]
                        bucket["sell_value_high"]   += sell["sell_value_high"]
                        bucket["sell_profit_high"]  += sell["sell_profit_high"]

                        bucket["sell_items_low"]    += sell["sell_items_low"]
                        bucket["sell_value_low"]    += sell["sell_value_low"]
                        bucket["sell_profit_low"]   += sell["sell_profit_low"]

                        bucket["sell_items_zero"]   += sell["sell_items_zero"]
                        bucket["sell_value_zero"]   += sell["sell_value_zero"]
                        bucket["sell_profit_zero"]  += sell["sell_profit_zero"]

                        bucket["sell_items_total"]  += sell["sell_items_total"]
                        bucket["sell_value_total"]  += sell["sell_value_total"]
                        bucket["sell_profit_total"] += sell["sell_profit_total"]
                    except Exception:
                        pass

        rows = list(per.values())
        rows.sort(key=lambda r: (r["system"], r["tickid"]))
        return jsonify(rows), 200

    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("system-summary failed")
        return jsonify({"error": str(e)}), 500

