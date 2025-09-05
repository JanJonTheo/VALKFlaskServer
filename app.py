from flask import Flask, request, jsonify, g
from sqlalchemy.exc import OperationalError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.engine import make_url
from models import db, Event, MarketBuyEvent, MarketSellEvent, MissionCompletedEvent, MissionCompletedInfluence, Activity, System, Faction, Objective, ObjectiveTarget, ObjectiveTargetSettlement
from models import SyntheticCZ, SyntheticGroundCZ
import logging
from functools import wraps
import bcrypt
from sqlalchemy import text
import requests as http_requests
from eic_tick_monitor import on_tick_change
from cmdr_sync_inara import sync_cmdrs_with_inara
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import os
import json
from dotenv import load_dotenv

load_dotenv()

# Hilfsfunktion zum Abrufen des Discord-Webhooks aus dem Tenant
def get_discord_webhook(webhook_type):
    tenant = getattr(g, "tenant", None)
    if tenant and "discord_webhooks" in tenant:
        return tenant["discord_webhooks"].get(webhook_type)
    return None

# Default API version
API_VERSION = os.getenv("API_VERSION_PROD", "1.6.0")

# Tenant-Konfiguration laden
TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    TENANTS = json.load(f)

def get_tenant_by_apikey(apikey):
    for tenant in TENANTS:
        if tenant["api_key"] == apikey:
            return tenant
    return None

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Mutable container to hold last known tickid
last_known_tickid = {"value": None}

app = Flask(__name__)

# Fallback-DB: Initialisiere DB nur einmal mit Standardkonfiguration
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///bgs_data.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)


def set_tenant_db_config(tenant):
    """Setzt die DB-Konfiguration für den aktuellen Tenant im Request-Kontext.
    - Bei SQLite: Falls die Datei fehlt, wird sie inkl. Tabellenstruktur angelegt.
    - Bei anderen DBs: Verbindung wird getestet, Fehler werden geloggt & im Request-Kontext hinterlegt.
    """
    if not tenant or not tenant.get("db_uri"):
        error_msg = "Tenant oder Datenbank-URI nicht gefunden. Kein Fallback erlaubt."
        logger.error(error_msg)
        g.tenant_db_error = error_msg
        return

    db_uri = tenant["db_uri"]

    try:
        url = make_url(db_uri)
        is_sqlite = url.drivername == "sqlite"

        # Pfad zur SQLite-Datei ermitteln (kein Memory-DB)
        sqlite_file_path = None
        if is_sqlite:
            # memory-DBs nicht anfassen
            if url.database in (None, "", ":memory:"):
                sqlite_file_path = None
            else:
                # Bei relativen Pfaden lässt SQLAlchemy sie relativ zum CWD auflösen.
                sqlite_file_path = url.database
                # Eventuell Verzeichnisse erstellen
                dir_name = os.path.dirname(os.path.abspath(sqlite_file_path))
                if dir_name and not os.path.exists(dir_name):
                    os.makedirs(dir_name, exist_ok=True)

        # Engine neu erstellen, falls URI sich geändert hat
        engine_changed = not hasattr(g, "tenant_db_engine") or getattr(g, "tenant_db_uri", None) != db_uri

        if engine_changed:
            # Für SQLite empfehlenswerte connect_args setzen
            connect_args = {"check_same_thread": False} if is_sqlite else {}
            engine = create_engine(db_uri, connect_args=connect_args)
            db.session = scoped_session(sessionmaker(bind=engine))
            g.tenant_db_engine = engine
            g.tenant_db_uri = db_uri
        else:
            engine = g.tenant_db_engine

        # --- Speziell für SQLite: Datei + Schema erzeugen, wenn Datei fehlt ---
        if is_sqlite and sqlite_file_path and not os.path.exists(sqlite_file_path):
            logger.info(f"SQLite-Datei für Tenant nicht gefunden. Erzeuge neue DB und Tabellen: {sqlite_file_path}")
            # Wichtig: Alle Models müssen importiert/registriert sein, damit metadata vollständig ist!
            # Beispiel (falls nötig): from yourapp.models import *  # noqa
            with engine.begin() as conn:
                # Falls du Flask-SQLAlchemy nutzt:
                db.Model.metadata.create_all(bind=conn)
                # Falls du eine eigene Declarative Base verwendest, stattdessen:
                # Base.metadata.create_all(bind=conn)

        # Verbindung testen
        db.session.execute(text("SELECT 1"))

    except OperationalError as e:
        logger.error(f"Tenant-Datenbank nicht gefunden oder nicht erreichbar: {db_uri} ({str(e)})")
        g.tenant_db_error = str(e)
    except Exception as e:
        # Generischer Fallback für unerwartete Fehler
        logger.exception(f"Fehler beim Setzen der Tenant-DB-Konfiguration für {db_uri}: {e}")
        g.tenant_db_error = str(e)


@app.teardown_appcontext
def remove_session(exception=None):
    db.session.remove()


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        apikey = request.headers.get("apikey")
        tenant = get_tenant_by_apikey(apikey)
        if not tenant:
            logger.warning(f"Invalid API-Key received: {apikey}")
            return jsonify({"error": "Unauthorized: Invalid API key"}), 401

        g.tenant = tenant
        set_tenant_db_config(tenant)

        # Prüfe, ob ein DB-Fehler vorliegt
        if hasattr(g, "tenant_db_error"):
            logger.error(f"Tenant-Datenbankfehler: {g.tenant_db_error}")
            return jsonify({"error": f"Tenant-Datenbank nicht gefunden oder nicht erreichbar: {g.tenant_db_error}"}), 500

        api_version = request.headers.get("apiversion")
        if not api_version:
            return jsonify({"error": "Missing required header: apiversion"}), 400

        import re
        if not re.match(r'^\d+\.\d+\.\d+$', api_version):
            return jsonify({"error": "Invalid apiversion format. Expected x.y.z notation"}), 400

        if api_version != tenant.get("api_version", API_VERSION):
            logger.warning(f"Client using different API version: {api_version} (tenant: {tenant.get('api_version', API_VERSION)})")

        return f(*args, **kwargs)
    return decorated


def get_latest_tickid():
    """
    Holt für alle Tenants das aktuellste tickid aus deren Datenbank
    und speichert es im last_known_tickid-Dict unter dem Tenant-Namen.
    """
    logging.info("[TickTriggerEIC] Get latest tickid für alle Tenants...")
    last_known_tickid.clear()
    for tenant in TENANTS:
        db_uri = tenant.get("db_uri")
        tenant_name = tenant.get("name") or tenant.get("api_key")
        if not db_uri:
            logging.warning(f"Tenant {tenant_name} hat keine db_uri, überspringe.")
            continue
        try:
            url = make_url(db_uri)
            is_sqlite = url.drivername == "sqlite"
            connect_args = {"check_same_thread": False} if is_sqlite else {}
            engine = create_engine(db_uri, connect_args=connect_args)
            with engine.connect() as conn:
                sql = text("SELECT tickid FROM event WHERE tickid IS NOT NULL ORDER BY timestamp DESC LIMIT 1")
                latest = conn.execute(sql).fetchone()
                last_known_tickid[tenant_name] = latest[0] if latest else None
                logging.info(f"[TickTriggerEIC] {tenant_name}: tickid = {last_known_tickid[tenant_name]}")
        except Exception as e:
            logging.error(f"[TickTriggerEIC] Fehler bei Tenant {tenant_name}: {e}")
            last_known_tickid[tenant_name] = None


@app.route("/events", methods=["POST"])
@require_api_key
def post_events():
    try:
        events_data = request.get_json()
        for event_dict in events_data:
            event = Event.from_dict(event_dict)
            db.session.add(event)
            db.session.flush()

            if event.event == "MarketBuy":
                db.session.add(MarketBuyEvent(
                    event_id=event.id,
                    stock=event_dict.get("Stock"),
                    stock_bracket=event_dict.get("StockBracket"),
                    value=event_dict.get("TotalCost"),
                    count=event_dict.get("Count")
                ))
            elif event.event == "MarketSell":
                db.session.add(MarketSellEvent(
                    event_id=event.id,
                    demand=event_dict.get("Demand"),
                    demand_bracket=event_dict.get("DemandBracket"),
                    profit=event_dict.get("Profit"),
                    value=event_dict.get("TotalSale"),
                    count=event_dict.get("Count")
                ))
            elif event.event == "MissionCompleted":
                db.session.add(MissionCompletedEvent(
                    event_id=event.id,
                    awarding_faction=event_dict.get("AwardingFaction"),
                    mission_name=event_dict.get("Name"),
                    reward=event_dict.get("Reward")
                ))
                faction_effects = event_dict.get("FactionEffects", [])
                for effect in faction_effects:
                    faction_name = effect.get("Faction")
                    reputation = effect.get("Reputation")
                    reputation_trend = effect.get("ReputationTrend")
                    effect_entries = effect.get("Effects", [])
                    influence_entries = effect.get("Influence", [])
                    for infl in influence_entries:
                        db.session.add(MissionCompletedInfluence(
                            mission_id=event.id,
                            system=infl.get("SystemAddress"),
                            influence=infl.get("Influence"),
                            trend=infl.get("Trend"),
                            faction_name=faction_name,
                            reputation=reputation,
                            reputation_trend=reputation_trend,
                            effect=effect_entries[0].get("Effect") if effect_entries else None,
                            effect_trend=effect_entries[0].get("Trend") if effect_entries else None
                        ))
            elif event.event == "FactionKillBond":
                from models import FactionKillBondEvent
                db.session.add(FactionKillBondEvent(
                    event_id=event.id,
                    killer_ship=event_dict.get("KillerShip"),
                    awarding_faction=event_dict.get("AwardingFaction"),
                    victim_faction=event_dict.get("VictimFaction"),
                    reward = event_dict.get("Reward")
                ))
            elif event.event == "MissionFailed":
                from models import MissionFailedEvent
                db.session.add(MissionFailedEvent(
                    event_id=event.id,
                    mission_name=event_dict.get("Name"),
                    awarding_faction=event_dict.get("AwardingFaction"),
                    fine=event_dict.get("Fine")
                ))
            elif event.event == "MultiSellExplorationData":
                from models import MultiSellExplorationDataEvent
                db.session.add(MultiSellExplorationDataEvent(
                    event_id=event.id,
                    total_earnings=event_dict.get("TotalEarnings")
                ))
            elif event.event == "RedeemVoucher":
                from models import RedeemVoucherEvent
                db.session.add(RedeemVoucherEvent(
                    event_id=event.id,
                    amount=event_dict.get("Amount"),
                    faction=event_dict.get("Faction"),
                    type=event_dict.get("Type")
                ))
            elif event.event == "SellExplorationData":
                from models import SellExplorationDataEvent
                db.session.add(SellExplorationDataEvent(
                    event_id=event.id,
                    earnings=event_dict.get("TotalEarnings")
                ))
            elif event.event == "CommitCrime":
                from models import CommitCrimeEvent
                db.session.add(CommitCrimeEvent(
                    event_id=event.id,
                    crime_type=event_dict.get("CrimeType"),
                    faction=event_dict.get("Faction"),
                    victim=event_dict.get("Victim"),
                    bounty=event_dict.get("Bounty")
                ))
            elif event.event == "SyntheticCZ":
                def extract_cz_type(data):
                    for cz in ["low", "medium", "high"]:
                        if data.get(cz) == 1:
                            return cz
                    return None
                cz_type = extract_cz_type(event_dict)
                # Faction robust extrahieren
                faction = event_dict.get("faction") or event_dict.get("Faction")
                db.session.add(SyntheticCZ(
                    event_id=event.id,
                    cz_type=cz_type,
                    faction=faction,
                    cmdr=event_dict.get("cmdr"),
                    station_faction_name=event_dict.get("station_faction_name")
                ))
            elif event.event == "SyntheticGroundCZ":
                def extract_cz_type(data):
                    for cz in ["low", "medium", "high"]:
                        if data.get(cz) == 1:
                            return cz
                    return None
                cz_type = extract_cz_type(event_dict)
                # Faction robust extrahieren
                faction = event_dict.get("faction") or event_dict.get("Faction")
                db.session.add(SyntheticGroundCZ(
                    event_id=event.id,
                    cz_type=cz_type,
                    settlement=event_dict.get("settlement"),
                    faction=faction,
                    cmdr=event_dict.get("cmdr"),
                    station_faction_name=event_dict.get("station_faction_name")
                ))

        db.session.commit()

        # Detect tickid change
        incoming_tickids = {event.get("tickid") for event in events_data if event.get("tickid")}
        current_tickid = next(iter(incoming_tickids), None)

        if current_tickid and last_known_tickid["value"] != current_tickid:
            logger.info(f"Tick changed: {last_known_tickid['value']} → {current_tickid}")
            last_known_tickid["value"] = current_tickid
            on_tick_change()

        return jsonify({"status": "success"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Event processing error: {str(e)}")
        return jsonify({"error": str(e)}), 400

@app.route("/activities", methods=["PUT"])
@require_api_key
def put_activities():
    try:
        activity_data = request.get_json()
        validated = activity_data

        activity = Activity(
            tickid=validated['tickid'],
            ticktime=validated['ticktime'],
            timestamp=validated['timestamp'],
            cmdr=validated.get('cmdr')
        )

        for sys in validated['systems']:
            system = System(name=sys['name'], address=sys['address'])
            for fac in sys['factions']:
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
            activity.systems.append(system)

        db.session.add(activity)
        db.session.commit()

        return jsonify({"status": "activity saved"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Activity processing error: {str(e)}")
        return jsonify({"error": str(e)}), 400


@app.route("/api/summary/<key>", methods=["GET"])
@require_api_key
def summary_api(key):

    queries = {
        "market-events": """
            SELECT e.cmdr,
                SUM(COALESCE(mb.value, 0)) AS total_buy,
                SUM(COALESCE(ms.value, 0)) AS total_sell,
                SUM(COALESCE(mb.value, 0)) + SUM(COALESCE(ms.value, 0)) AS total_transaction_volume,
                SUM(COALESCE(mb.count, 0)) + SUM(COALESCE(ms.count, 0)) AS total_trade_quantity
            FROM event e
            LEFT JOIN market_buy_event mb ON mb.event_id = e.id
            LEFT JOIN market_sell_event ms ON ms.event_id = e.id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            HAVING total_transaction_volume > 0
            ORDER BY total_trade_quantity DESC
            """,
        "missions-completed": """
            SELECT e.cmdr, COUNT(*) AS missions_completed
            FROM mission_completed_event mc
            JOIN event e ON e.id = mc.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY missions_completed DESC
            """,
        "missions-failed": """
            SELECT e.cmdr, COUNT(*) AS missions_failed
            FROM mission_failed_event mf
            JOIN event e ON e.id = mf.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY missions_failed DESC
            """,
        "bounty-vouchers": """
            SELECT e.cmdr, SUM(rv.amount) AS bounty_vouchers
            FROM redeem_voucher_event rv
            JOIN event e ON e.id = rv.event_id
            WHERE e.cmdr IS NOT NULL AND rv.type = 'bounty' AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY bounty_vouchers DESC
            """,
        "combat-bonds": """
            SELECT e.cmdr, SUM(rv.amount) AS combat_bonds
            FROM redeem_voucher_event rv
            JOIN event e ON e.id = rv.event_id
            WHERE e.cmdr IS NOT NULL AND rv.type = 'CombatBond' AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY combat_bonds DESC
            """,
        "influence-by-faction": """
            SELECT e.cmdr, mci.faction_name, SUM(LENGTH(mci.influence)) AS influence
            FROM mission_completed_influence mci
            JOIN mission_completed_event mce ON mce.event_id = mci.mission_id
            JOIN event e ON e.id = mce.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr, mci.faction_name
            ORDER BY influence DESC, e.cmdr
            """,
        "influence-eic": """
            SELECT e.cmdr, mci.faction_name, SUM(LENGTH(mci.influence)) AS influence
            FROM mission_completed_influence mci
            JOIN mission_completed_event mce ON mce.event_id = mci.mission_id
            JOIN event e ON e.id = mce.event_id
            WHERE e.cmdr IS NOT NULL
            AND mci.faction_name LIKE '%East India Company%'
            AND {date_filter}
            GROUP BY e.cmdr, mci.faction_name
            ORDER BY influence DESC, e.cmdr
            """,
        "exploration-sales": """
            SELECT cmdr, SUM(total_sales) AS total_exploration_sales
            FROM (SELECT e.cmdr, se.earnings AS total_sales
            FROM sell_exploration_data_event se
            JOIN event e ON e.id = se.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            UNION ALL
            SELECT e.cmdr, ms.total_earnings AS total_sales
            FROM multi_sell_exploration_data_event ms
            JOIN event e
            ON e.id = ms.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter})
            GROUP BY cmdr
            ORDER BY total_exploration_sales DESC
            """,
        "bounty-fines": """
            SELECT e.cmdr, SUM(cc.bounty) AS bounty_fines
            FROM commit_crime_event cc
            JOIN event e ON e.id = cc.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY bounty_fines DESC
            """
    }

    sql_template = queries.get(key)
    if not sql_template:
        return jsonify({"error": "Unknown summary key"}), 404

    # Zeitraum filtern (wie bei leaderboard)
    period = request.args.get("period", "all")
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

    if start and end:
        date_filter = f"e.timestamp BETWEEN '{start.strftime('%Y-%m-%dT00:00:00Z')}' AND '{end.strftime('%Y-%m-%dT23:59:59Z')}'"
    else:
        date_filter = "1=1"

    sql = sql_template.replace("{date_filter}", date_filter)

    try:
        result = db.session.execute(text(sql)).fetchall()
        data = [dict(row._mapping) for row in result]
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary/top5/<key>", methods=["GET"])
@require_api_key
def summary_top5_api(key):

    def get_date_filter(period: str):
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

        if start and end:
            return f"e.timestamp BETWEEN '{start.strftime('%Y-%m-%dT00:00:00Z')}' AND '{end.strftime('%Y-%m-%dT23:59:59Z')}'"
        return "1=1"

    base_queries = {
        "market-events": """
            SELECT e.cmdr,
                   SUM(COALESCE(mb.value, 0)) AS total_buy,
                   SUM(COALESCE(ms.value, 0)) AS total_sell,
                   SUM(COALESCE(mb.value, 0)) + SUM(COALESCE(ms.value, 0)) AS total_transaction_volume,
                   SUM(COALESCE(mb.count, 0)) + SUM(COALESCE(ms.count, 0)) AS total_trade_quantity
            FROM event e
            LEFT JOIN market_buy_event mb ON mb.event_id = e.id
            LEFT JOIN market_sell_event ms ON ms.event_id = e.id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            HAVING total_transaction_volume > 0
            ORDER BY total_trade_quantity DESC
            LIMIT 5
        """,
        "missions-completed": """
            SELECT e.cmdr, COUNT(*) AS missions_completed
            FROM mission_completed_event mc
            JOIN event e ON e.id = mc.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY missions_completed DESC
            LIMIT 5
        """,
        "missions-failed": """
            SELECT e.cmdr, COUNT(*) AS missions_failed
            FROM mission_failed_event mf
            JOIN event e ON e.id = mf.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY missions_failed DESC
            LIMIT 5
        """,
        "bounty-vouchers": """
            SELECT e.cmdr, SUM(rv.amount) AS bounty_vouchers
            FROM redeem_voucher_event rv
            JOIN event e ON e.id = rv.event_id
            WHERE e.cmdr IS NOT NULL AND rv.type = 'bounty' AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY bounty_vouchers DESC
            LIMIT 5
        """,
        "combat-bonds": """
            SELECT e.cmdr, SUM(rv.amount) AS combat_bonds
            FROM redeem_voucher_event rv
            JOIN event e ON e.id = rv.event_id
            WHERE e.cmdr IS NOT NULL AND rv.type = 'CombatBond' AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY combat_bonds DESC
            LIMIT 5
        """,
        "influence-by-faction": """
            SELECT e.cmdr, mci.faction_name, SUM(LENGTH(mci.influence)) AS influence
            FROM mission_completed_influence mci
            JOIN mission_completed_event mce ON mce.event_id = mci.mission_id
            JOIN event e ON e.id = mce.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr, mci.faction_name
            ORDER BY influence DESC, e.cmdr
            LIMIT 5
        """,
        "influence-eic": """
            SELECT e.cmdr, mci.faction_name, SUM(LENGTH(mci.influence)) AS influence
            FROM mission_completed_influence mci
            JOIN mission_completed_event mce ON mce.event_id = mci.mission_id
            JOIN event e ON e.id = mce.event_id
            WHERE e.cmdr IS NOT NULL AND mci.faction_name LIKE '%East India Company%' AND {date_filter}
            GROUP BY e.cmdr, mci.faction_name
            ORDER BY influence DESC, e.cmdr
            LIMIT 5
        """,
        "exploration-sales": """
            SELECT cmdr,
                   SUM(total_sales) AS total_exploration_sales
            FROM (
                SELECT e.cmdr, se.earnings AS total_sales
                FROM sell_exploration_data_event se
                JOIN event e ON e.id = se.event_id
                WHERE e.cmdr IS NOT NULL AND {date_filter}
                UNION ALL
                SELECT e.cmdr, ms.total_earnings AS total_sales
                FROM multi_sell_exploration_data_event ms
                JOIN event e ON e.id = ms.event_id
                WHERE e.cmdr IS NOT NULL AND {date_filter}
            )
            GROUP BY cmdr
            ORDER BY total_exploration_sales DESC
            LIMIT 5
        """,
        "bounty-fines": """
            SELECT e.cmdr, SUM(cc.bounty) AS bounty_fines
            FROM commit_crime_event cc
            JOIN event e ON e.id = cc.event_id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY bounty_fines DESC
            LIMIT 5
        """
    }

    sql_template = base_queries.get(key)
    if not sql_template:
        return jsonify({"error": "Unknown summary key"}), 404

    period = request.args.get("period", "all")
    date_filter = get_date_filter(period)
    sql = sql_template.replace("{date_filter}", date_filter)

    try:
        result = db.session.execute(text(sql)).fetchall()
        data = [dict(row._mapping) for row in result]
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/table/<tablename>", methods=["GET"])
@require_api_key
def query_table(tablename):
    try:
        # Security check: Ensure the table name is valid and exists
        result = db.session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:name"
        ), {"name": tablename}).fetchone()

        if not result:
            return jsonify({"error": f"Table '{tablename}' not found."}), 404

        # Daten abfragen
        rows = db.session.execute(text(f"SELECT * FROM {tablename}")).fetchall()
        data = [dict(row._mapping) for row in rows]
        return jsonify(data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary/discord/top5all", methods=["POST"])
@require_api_key
def send_all_top5_to_discord():
    base_queries = {
        "Market Events": {
            "sql": '''
                SELECT e.cmdr,
                       SUM(COALESCE(mb.value, 0)) AS total_buy,
                       SUM(COALESCE(ms.value, 0)) AS total_sell,
                       SUM(COALESCE(mb.value, 0)) + SUM(COALESCE(ms.value, 0)) AS total_volume,
                       SUM(COALESCE(mb.count, 0)) + SUM(COALESCE(ms.count, 0)) AS quantity
                FROM event e
                LEFT JOIN market_buy_event mb ON mb.event_id = e.id
                LEFT JOIN market_sell_event ms ON ms.event_id = e.id
                WHERE e.cmdr IS NOT NULL
                GROUP BY e.cmdr
                HAVING total_volume > 0
                ORDER BY quantity DESC
                LIMIT 5
            ''',
            "format": lambda rows: "\n".join(
                f"{i+1}. {(r.cmdr or ''):<15} | Vol: {r.total_volume or 0:>15,} Cr. - {r.quantity or 0:>9,} t"
                for i, r in enumerate(rows)
            )
        },
        "Missions Completed": {
            "sql": '''
                SELECT e.cmdr, COUNT(*) AS missions_completed
                FROM mission_completed_event mc
                JOIN event e ON e.id = mc.event_id
                WHERE e.cmdr IS NOT NULL
                GROUP BY e.cmdr
                ORDER BY missions_completed DESC
                LIMIT 5
            ''',
            "format": lambda rows: "\n".join(
                f"{i+1}. {(r.cmdr or 0):<15} | {r.missions_completed:>4}"
                for i, r in enumerate(rows)
            )
        },
        # "Missions Failed": {
        #     "sql": '''
        #         SELECT e.cmdr, COUNT(*) AS missions_failed
        #         FROM mission_failed_event mf
        #         JOIN event e ON e.id = mf.event_id
        #         WHERE e.cmdr IS NOT NULL
        #         GROUP BY e.cmdr
        #         ORDER BY missions_failed DESC
        #         LIMIT 5
        #     ''',
        #     "format": lambda rows: "\n".join(
        #         f"{i+1}. {(r.cmdr or 0):<15} | Failed: {r.missions_failed}"
        #         for i, r in enumerate(rows)
        #     )
        # },
        "Influence by Faction": {
            "sql": '''
                SELECT e.cmdr, mci.faction_name, SUM(LENGTH(mci.influence)) AS influence
                FROM mission_completed_influence mci
                JOIN mission_completed_event mce ON mce.event_id = mci.mission_id
                JOIN event e ON e.id = mce.event_id
                WHERE e.cmdr IS NOT NULL
                GROUP BY e.cmdr, mci.faction_name
                ORDER BY influence DESC, e.cmdr
                LIMIT 5
            ''',
            "format": lambda rows: "\n".join(
                f"{i+1}. {(r.cmdr or 0):<15} | {(r.faction_name or 0):<30} | +{r.influence:>4}"
                for i, r in enumerate(rows)
            )
        },
        "Influence EIC": {
            "sql": '''
                SELECT e.cmdr, mci.faction_name, SUM(LENGTH(mci.influence)) AS influence
                FROM mission_completed_influence mci
                JOIN mission_completed_event mce ON mce.event_id = mci.mission_id
                JOIN event e ON e.id = mce.event_id
                WHERE e.cmdr IS NOT NULL AND mci.faction_name LIKE '%East India Company%' AND {date_filter}
                GROUP BY e.cmdr, mci.faction_name
                ORDER BY influence DESC, e.cmdr
                LIMIT 5
            ''',
            "format": lambda rows: "\n".join(
                f"{i+1}. {(r.cmdr or 0):<15} | +{r.influence}"
                for i, r in enumerate(rows)
            )
        },
        "Bounty Vouchers": {
            "sql": '''
                SELECT e.cmdr, SUM(rv.amount) AS bounty_vouchers
                FROM redeem_voucher_event rv
                JOIN event e ON e.id = rv.event_id
                WHERE e.cmdr IS NOT NULL AND rv.type = 'bounty'
                GROUP BY e.cmdr
                ORDER BY bounty_vouchers DESC
                LIMIT 5
            ''',
            "format": lambda rows: "\n".join(
                f"{i+1}. {(r.cmdr or 0):<15} | {r.bounty_vouchers or 0:>15,} Cr."
                for i, r in enumerate(rows)
            )
        },
        "Combat Bonds": {
            "sql": '''
                SELECT e.cmdr, SUM(rv.amount) AS combat_bonds
                FROM redeem_voucher_event rv
                JOIN event e ON e.id = rv.event_id
                WHERE e.cmdr IS NOT NULL AND rv.type = 'CombatBond'
                GROUP BY e.cmdr
                ORDER BY combat_bonds DESC
                LIMIT 5
            ''',
            "format": lambda rows: "\n".join(
                f"{i+1}. {(r.cmdr or 0):<15} | {r.combat_bonds or 0:>15,} Cr."
                for i, r in enumerate(rows)
            )
        },
        "Exploration Sales": {
            "sql": '''
                SELECT cmdr,
                   SUM(total_sales) AS total_exploration_sales
                FROM (
                    SELECT e.cmdr, se.earnings AS total_sales
                    FROM sell_exploration_data_event se
                    JOIN event e ON e.id = se.event_id
                    WHERE e.cmdr IS NOT NULL
                    UNION ALL
                    SELECT e.cmdr, ms.total_earnings AS total_sales
                    FROM multi_sell_exploration_data_event ms
                    JOIN event e ON e.id = ms.event_id
                    WHERE e.cmdr IS NOT NULL
                )
                GROUP BY cmdr
                ORDER BY total_exploration_sales DESC
                LIMIT 5
            ''',
            "format": lambda rows: "\n".join(
                f"{i+1}. {(r.cmdr or 0):<15} | {r.total_exploration_sales or 0:>15,} Cr."
                for i, r in enumerate(rows)
            )
        },
        "Bounty Fines": {
            "sql": '''
                   SELECT e.cmdr, SUM(cc.bounty) AS bounty_fines
                   FROM commit_crime_event cc
                   JOIN event e ON e.id = cc.event_id
                   WHERE e.cmdr IS NOT NULL
                   GROUP BY e.cmdr
                   ORDER BY bounty_fines DESC LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. {(r.cmdr or 0):<15} | {r.bounty_fines or 0:>15,} Cr."
                for i, r in enumerate(rows)
            )
        }
    }

    try:
        results = []
        for tenant in TENANTS:
            db_uri = tenant.get("db_uri")
            tenant_name = tenant.get("name") or tenant.get("api_key")
            webhook_url = tenant.get("discord_webhooks", {}).get("shoutout")
            if not db_uri or not webhook_url:
                results.append({"tenant": tenant_name, "status": "skipped", "reason": "No DB URI or webhook"})
                continue
            url = make_url(db_uri)
            is_sqlite = url.drivername == "sqlite"
            connect_args = {"check_same_thread": False} if is_sqlite else {}
            engine = create_engine(db_uri, connect_args=connect_args)
            with engine.connect() as conn:
                sections = []
                for title, q in base_queries.items():
                    rows = conn.execute(text(q["sql"])).fetchall()
                    if not rows:
                        continue
                    section = f"**📊 {title}**\n```text\n{q['format'](rows)}\n```"
                    sections.append(section)
                if not sections:
                    results.append({"tenant": tenant_name, "status": "no data"})
                    continue
                full_message = f"**{tenant_name}**\n\n" + "\n\n".join(sections)
                resp = http_requests.post(webhook_url, json={"content": full_message})
                if resp.status_code != 204:
                    results.append({"tenant": tenant_name, "status": "discord error", "response": resp.text})
                else:
                    results.append({"tenant": tenant_name, "status": "sent"})
        return jsonify(results), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary/discord/tick", methods=["POST"])
@require_api_key
def trigger_daily_tick_summary():
    try:
        from eic_shoutout_scheduler import format_discord_summary
        format_discord_summary(app, db)
        return jsonify({"status": "Daily summary triggered"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary/discord/syntheticcz", methods=["POST"])
@require_api_key
def send_syntheticcz_summary_to_discord_api():
    """
    Triggert die Discord-Space-CZ-Summary für einen angegebenen Zeitraum.
    Query-Parameter: period (z.B. 'cw', 'lw', 'cm', 'lm', '2m', 'y', 'cd', 'ld', 'all')
    """
    try:
        period = request.args.get("period", "all")
        from eic_shoutout_scheduler import send_syntheticcz_summary_to_discord
        send_syntheticcz_summary_to_discord(app, db, period)
        return jsonify({"status": f"SyntheticCZ-Summary für Discord gesendet ({period})"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary/discord/syntheticgroundcz", methods=["POST"])
@require_api_key
def send_syntheticgroundcz_summary_to_discord_api():
    """
    Triggert die Discord-GroundCZ-Summary für einen angegebenen Zeitraum.
    Query-Parameter: period (z.B. 'cw', 'lw', 'cm', 'lm', '2m', 'y', 'cd', 'ld', 'all')
    """
    try:
        period = request.args.get("period", "all")
        from eic_shoutout_scheduler import send_syntheticgroundcz_summary_to_discord
        send_syntheticgroundcz_summary_to_discord(app, db, period)
        return jsonify({"status": f"SyntheticGroundCZ-Summary für Discord gesendet ({period})"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Register EIC Conflict routes
from eic_in_conflict import register_eic_conflict_routes
register_eic_conflict_routes(app, db, require_api_key)


@app.route("/api/debug/tick-change", methods=["POST"])
@require_api_key
def debug_tick_change():
    try:
        on_tick_change()
        return jsonify({"status": "Tick change hook triggered"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/cmdrs", methods=["POST"])
@require_api_key
def sync_cmdrs_api():
    try:
        sync_cmdrs_with_inara(db)
        return jsonify({"status": "Cmdr sync complete"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/login", methods=["POST"])
def login_api():
    try:
        data = request.get_json()
        username = data.get("username")
        password = data.get("password")

        if not username or not password:
            return jsonify({"error": "Missing credentials"}), 400

        query = text("SELECT id, password_hash, is_admin FROM users WHERE username = :username AND active = 1")
        result = db.session.execute(query, {"username": username}).fetchone()

        if not result:
            return jsonify({"error": "Invalid credentials"}), 401

        uid, hashed, is_admin = result
        if bcrypt.checkpw(password.encode(), hashed.encode()):
            return jsonify({
                "id": uid,
                "username": username,
                "is_admin": bool(is_admin)
            })

        return jsonify({"error": "Invalid credentials"}), 401

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary/leaderboard", methods=["GET"])
@require_api_key
def leaderboard_summary():
    try:
        period = request.args.get("period", "all")
        today = datetime.utcnow()
        start = end = None

        if period == "cw":  # current week
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
        elif period == "lw":  # last week
            end = today - timedelta(days=today.weekday() + 1)
            start = end - timedelta(days=6)
        elif period == "cm":  # current month
            start = today.replace(day=1)
            end = (start + relativedelta(months=1)) - timedelta(days=1)
        elif period == "lm":  # last month
            this_month_start = today.replace(day=1)
            start = this_month_start - relativedelta(months=1)
            end = this_month_start - timedelta(days=1)
        elif period == "2m":  # last two full months
            this_month_start = today.replace(day=1)
            start = this_month_start - relativedelta(months=2)
            end = this_month_start - timedelta(days=1)
        elif period == "y":  # year-to-date
            start = today.replace(month=1, day=1)
            end = today.replace(month=12, day=31)
        elif period == "cd":  # current day (today)
            start = end = today
        elif period == "ld":  # last day (yesterday)
            start = end = today - timedelta(days=1)

        if start and end:
            date_filter = f"e.timestamp BETWEEN '{start.strftime('%Y-%m-%dT00:00:00Z')}' AND '{end.strftime('%Y-%m-%dT23:59:59Z')}'"
            date_filter_sub = f"ex.timestamp BETWEEN '{start.strftime('%Y-%m-%dT00:00:00Z')}' AND '{end.strftime('%Y-%m-%dT23:59:59Z')}'"
        else:
            date_filter = "1=1"
            date_filter_sub = "1=1"

        sql = f"""
            SELECT e.cmdr,
               c.squadron_rank AS rank,
               SUM(CASE WHEN mb.event_id IS NOT NULL THEN mb.value ELSE 0 END) AS total_buy,
               SUM(CASE WHEN ms.event_id IS NOT NULL THEN ms.value ELSE 0 END) AS total_sell,
               CASE
                   WHEN SUM(CASE WHEN ms.event_id IS NOT NULL THEN ms.value ELSE 0 END) > 0
                   THEN SUM(CASE WHEN ms.event_id IS NOT NULL THEN ms.value ELSE 0 END)
                        - SUM(CASE WHEN mb.event_id IS NOT NULL THEN mb.value ELSE 0 END)
                   ELSE 0
               END AS profit,
               ROUND(
                 CASE
                   WHEN SUM(CASE WHEN ms.event_id IS NOT NULL THEN ms.value ELSE 0 END) > 0 AND
                        SUM(CASE WHEN mb.event_id IS NOT NULL THEN mb.value ELSE 0 END) > 0
                   THEN (SUM(CASE WHEN ms.event_id IS NOT NULL THEN ms.value ELSE 0 END)
                         - SUM(CASE WHEN mb.event_id IS NOT NULL THEN mb.value ELSE 0 END)) * 100.0
                        / SUM(CASE WHEN mb.event_id IS NOT NULL THEN mb.value ELSE 0 END)
                   ELSE 0
                 END, 2
               ) AS profitability,

               SUM(CASE WHEN mb.event_id IS NOT NULL THEN mb.count ELSE 0 END) +
               SUM(CASE WHEN ms.event_id IS NOT NULL THEN ms.count ELSE 0 END) AS total_quantity,

               SUM(CASE WHEN mb.event_id IS NOT NULL THEN mb.value ELSE 0 END) +
               SUM(CASE WHEN ms.event_id IS NOT NULL THEN ms.value ELSE 0 END) AS total_volume,

               (
                 SELECT COUNT(*)
                 FROM mission_completed_event mc
                 JOIN event ex ON ex.id = mc.event_id
                 WHERE ex.cmdr = e.cmdr AND {date_filter_sub}
               ) AS missions_completed,

               (
                 SELECT COUNT(*)
                 FROM mission_failed_event mf
                 JOIN event ex ON ex.id = mf.event_id
                 WHERE ex.cmdr = e.cmdr AND {date_filter_sub}
               ) AS missions_failed,

               (
                 SELECT SUM(rv.amount)
                 FROM redeem_voucher_event rv
                 JOIN event ex ON ex.id = rv.event_id
                 WHERE ex.cmdr = e.cmdr AND rv.type = 'bounty' AND {date_filter_sub}
               ) AS bounty_vouchers,

               (
                 SELECT SUM(rv.amount)
                 FROM redeem_voucher_event rv
                 JOIN event ex ON ex.id = rv.event_id
                 WHERE ex.cmdr = e.cmdr AND rv.type = 'CombatBond' AND {date_filter_sub}
               ) AS combat_bonds,

               (
                 SELECT SUM(t.total_sales)
                 FROM (
                   SELECT se.earnings AS total_sales
                   FROM sell_exploration_data_event se
                   JOIN event ex ON ex.id = se.event_id
                   WHERE ex.cmdr = e.cmdr AND {date_filter_sub}
                   UNION ALL
                   SELECT me.total_earnings AS total_sales
                   FROM multi_sell_exploration_data_event me
                   JOIN event ex ON ex.id = me.event_id
                   WHERE ex.cmdr = e.cmdr AND {date_filter_sub}
                 ) t
               ) AS exploration_sales,

               (
                 SELECT SUM(LENGTH(mci.influence))
                 FROM mission_completed_influence mci
                 JOIN mission_completed_event mce ON mce.event_id = mci.mission_id
                 JOIN event ex ON ex.id = mce.event_id
                 WHERE ex.cmdr = e.cmdr AND mci.faction_name LIKE '%East India Company%' AND {date_filter_sub}
               ) AS influence_eic,

               (
                 SELECT SUM(cc.bounty)
                 FROM commit_crime_event cc
                 JOIN event ex ON ex.id = cc.event_id
                 WHERE ex.cmdr = e.cmdr AND {date_filter_sub}
               ) AS bounty_fines

            FROM event e
            LEFT JOIN cmdr c ON c.name = e.cmdr
            LEFT JOIN market_buy_event mb ON mb.event_id = e.id
            LEFT JOIN market_sell_event ms ON ms.event_id = e.id
            WHERE e.cmdr IS NOT NULL AND {date_filter}
            GROUP BY e.cmdr
            ORDER BY e.cmdr
        """

        result = db.session.execute(text(sql)).fetchall()
        data = [dict(row._mapping) for row in result]
        return jsonify(data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary/recruits", methods=["GET"])
@require_api_key
def summary_recruits():
    try:
        sql = """
              SELECT e.cmdr                                                      AS commander, \
                     CASE WHEN COUNT(e.id) > 0 THEN 'Yes' ELSE 'No' END          AS has_data, \
                     MAX(e.timestamp)                                            AS last_active, \
                     CAST(julianday('now') - julianday(MIN(e.timestamp)) AS INT) AS days_since_join, \
                     (SELECT COALESCE(SUM(mb.count), 0) + COALESCE((SELECT SUM(ms.count)
                                                                    FROM market_sell_event ms
                                                                             JOIN event e2 ON e2.id = ms.event_id
                                                                    WHERE e2.cmdr = e.cmdr), 0)
                      FROM market_buy_event mb
                               JOIN event e1 ON e1.id = mb.event_id
                      WHERE e1.cmdr = e.cmdr)                                    AS tonnage, \
                     (SELECT COUNT(*)
                      FROM mission_completed_event mc
                               JOIN event ev ON ev.id = mc.event_id
                      WHERE ev.cmdr = e.cmdr)                                    AS mission_count, \
                     (SELECT SUM(rv.amount)
                      FROM redeem_voucher_event rv
                               JOIN event ev ON ev.id = rv.event_id
                      WHERE ev.cmdr = e.cmdr \
                        AND rv.type = 'bounty')                                  AS bounty_claims, \
                     (SELECT SUM(total)
                      FROM (SELECT se.earnings AS total \
                            FROM sell_exploration_data_event se \
                                     JOIN event ev ON ev.id = se.event_id \
                            WHERE ev.cmdr = e.cmdr \
                            UNION ALL \
                            SELECT me.total_earnings AS total \
                            FROM multi_sell_exploration_data_event me \
                                     JOIN event ev ON ev.id = me.event_id \
                            WHERE ev.cmdr = e.cmdr))                             AS exp_value, \
                     (SELECT SUM(rv.amount)
                      FROM redeem_voucher_event rv
                               JOIN event ev ON ev.id = rv.event_id
                      WHERE ev.cmdr = e.cmdr \
                        AND rv.type = 'CombatBond')                              AS combat_bonds, \
                     (SELECT SUM(cc.bounty)
                      FROM commit_crime_event cc
                               JOIN event ev ON ev.id = cc.event_id
                      WHERE ev.cmdr = e.cmdr)                                    AS bounty_fines
              FROM event e
                       JOIN cmdr c ON c.name = e.cmdr
              WHERE e.cmdr IS NOT NULL \
                AND c.squadron_rank = 'Recruit'
              GROUP BY e.cmdr
              ORDER BY days_since_join ASC \
              """
        result = db.session.execute(text(sql)).fetchall()
        data = [dict(row._mapping) for row in result]
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/discovery", methods=["GET"])
def discovery():
    """Discovery endpoint providing server capabilities and information"""
    try:
        discovery_response = {
            "name": os.getenv("SERVER_NAME_PROD"),
            "description": os.getenv("SERVER_DESCRIPTION_PROD"),
            "url": os.getenv("SERVER_URL_PROD"),
            "endpoints": {
                "events": {
                    "path": "/events",
                    "minPeriod": "10",
                    "maxBatch": "100"
                },
                "activities": {
                    "path": "/activities",
                    "minPeriod": "60",
                    "maxBatch": "10"
                },
                "objectives": {
                    "path": "/objectives",
                    "minPeriod": "30",
                    "maxBatch": "20"
                }
            },
            "headers": {
                "apikey": {
                    "required": True,
                    "description": "API key for authentication"
                },
                "apiversion": {
                    "required": True,
                    "description": "The version of the API in x.y.z notation",
                    "current": API_VERSION
                }
            }
        }

        return jsonify(discovery_response), 200

    except Exception as e:
        logger.error(f"Discovery endpoint error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET"])
def root():
    """Root endpoint providing basic server information"""
    try:
        return jsonify({
            "message": "VALK Flask Server is running",
            "version": os.getenv("API_VERSION_PROD", "1.0.0"),
            "name": os.getenv("SERVER_NAME_PROD", "VALK Flask Server"),
            "endpoints": {
                "discovery": "/discovery",
                "api": "/api/"
            }
        }), 200
    except Exception as e:
        logger.error(f"Root endpoint error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/objectives", methods=["POST"])
@app.route("/objectives", methods=["POST"])
@require_api_key
def create_objective():
    try:
        data = request.get_json()

        # Validierung der Pflichtfelder
        if not data.get("title"):
            return jsonify({"error": "Title is required"}), 400

        objective = Objective(
            title=data.get("title"),
            priority=data.get("priority"),
            type=data.get("type"),
            system=data.get("system"),
            faction=data.get("faction"),
            description=data.get("description"),
            startdate=datetime.fromisoformat(data["startdate"]) if data.get("startdate") else None,
            enddate=datetime.fromisoformat(data["enddate"]) if data.get("enddate") else None
        )

        for target_data in data.get("targets", []):
            target = ObjectiveTarget(
                type=target_data.get("type"),
                station=target_data.get("station"),
                system=target_data.get("system"),
                faction=target_data.get("faction"),
                progress=target_data.get("progress", 0),
                targetindividual=target_data.get("targetindividual"),
                targetoverall=target_data.get("targetoverall")
            )

            for s in target_data.get("settlements", []):
                settlement = ObjectiveTargetSettlement(
                    name=s.get("name"),
                    targetindividual=s.get("targetindividual"),
                    targetoverall=s.get("targetoverall"),
                    progress=s.get("progress", 0)
                )
                target.settlements.append(settlement)

            objective.targets.append(target)

        db.session.add(objective)
        db.session.commit()

        return jsonify({
            "status": "Objective created successfully",
            "id": objective.id
        }), 201

    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": f"Invalid date format: {str(e)}"}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Objective creation error: {str(e)}")
        return jsonify({"error": str(e)}), 400


@app.route("/objectives", methods=["GET"])
@require_api_key
def get_objectives():
    try:
        system_filter = request.args.get("system")
        faction_filter = request.args.get("faction")
        active_only = request.args.get("active", "false").lower() == "true"

        query = Objective.query
        if system_filter:
            query = query.filter_by(system=system_filter)
        if faction_filter:
            query = query.filter_by(faction=faction_filter)
        if active_only:
            now = datetime.utcnow()
            query = query.filter(
                Objective.startdate <= now,
                Objective.enddate >= now
            )

        objectives = query.all()

        def serialize_objective(obj):
            return {
                "title": obj.title or "",
                "priority": str(obj.priority) if obj.priority is not None else "0",
                "startdate": obj.startdate.isoformat() + "Z" if obj.startdate else None,
                "enddate": obj.enddate.isoformat() + "Z" if obj.enddate else None,
                "type": obj.type or "",
                "system": obj.system or "",
                "faction": obj.faction or "",
                "targets": [
                    {
                        "type": t.type or "",
                        "station": t.station or "",
                        "progress": t.progress or 0,
                        "system": t.system or "",
                        "faction": t.faction or "",
                        "settlements": [
                            {
                                "name": s.name or "",
                                "targetindividual": s.targetindividual or 0,
                                "targetoverall": s.targetoverall or 0,
                                "progress": s.progress or 0
                            } for s in t.settlements
                        ],
                        "targetindividual": t.targetindividual or 0,
                        "targetoverall": t.targetoverall or 0
                    } for t in obj.targets
                ],
                "description": obj.description or ""
            }

        result = [serialize_objective(o) for o in objectives]
        return jsonify(result)

    except Exception as e:
        logger.error(f"Get objectives error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/objectives", methods=["GET"])
@require_api_key
def get_objectives_streamlit():
    """
    Streamlit-optimierte Version des Objectives-Endpunkts
    Enthält IDs und ist für die UI-Darstellung optimiert
    """
    try:
        system_filter = request.args.get("system")
        faction_filter = request.args.get("faction")
        active_only = request.args.get("active", "false").lower() == "true"

        query = Objective.query
        if system_filter:
            query = query.filter_by(system=system_filter)
        if faction_filter:
            query = query.filter_by(faction=faction_filter)
        if active_only:
            now = datetime.utcnow()
            query = query.filter(
                Objective.startdate <= now,
                Objective.enddate >= now
            )

        objectives = query.all()

        def serialize_objective_streamlit(obj):
            return {
                "id": obj.id,
                "title": obj.title or "",
                "priority": str(obj.priority) if obj.priority is not None else "0",
                "startdate": obj.startdate.isoformat() if obj.startdate else None,
                "enddate": obj.enddate.isoformat() if obj.enddate else None,
                "type": obj.type or "",
                "system": obj.system or "",
                "faction": obj.faction or "",
                "targets": [
                    {
                        "id": t.id,
                        "type": t.type or "",
                        "station": t.station or "",
                        "progress": t.progress or 0,
                        "system": t.system or "",
                        "faction": t.faction or "",
                        "settlements": [
                            {
                                "id": s.id,
                                "name": s.name or "",
                                "targetindividual": s.targetindividual or 0,
                                "targetoverall": s.targetoverall or 0,
                                "progress": s.progress or 0
                            } for s in t.settlements
                        ],
                        "targetindividual": t.targetindividual or 0,
                        "targetoverall": t.targetoverall or 0
                    } for t in obj.targets
                ],
                "description": obj.description or ""
            }

        result = [serialize_objective_streamlit(o) for o in objectives]
        return jsonify(result)

    except Exception as e:
        logger.error(f"Get objectives streamlit error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/objectives/<int:objective_id>', methods=['DELETE'])
@app.route('/objectives/<int:objective_id>', methods=['DELETE'])
@require_api_key
def delete_objective(objective_id):
    """
    Löscht ein Objective und alle zugehörigen Child-Datensätze.
    """
    from sqlalchemy.exc import SQLAlchemyError
    try:
        # Hole das Objective
        objective = Objective.query.get(objective_id)
        if not objective:
            return jsonify({'error': 'Objective not found'}), 404

        # Lösche zugehörige Child-Datensätze - verwende die korrekten Beziehungen
        # Lösche zuerst die settlements der targets
        for target in objective.targets:
            for settlement in target.settlements:
                db.session.delete(settlement)
            db.session.delete(target)

        # Lösche das Objective selbst
        db.session.delete(objective)
        db.session.commit()

        logger.info(f"Objective {objective_id} and related data deleted successfully")
        return jsonify({'message': f'Objective {objective_id} und zugehörige Daten gelöscht'}), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error deleting objective {objective_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting objective {objective_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route("/api/bounty-vouchers", methods=["GET"])
@require_api_key
def get_bounty_vouchers():
    """
    Gibt alle Bounty Vouchers mit den Spalten Cmdr, Squadron Rank, System, timestamp, tick-id, amount, type, faction zurück.
    Unterstützt Filter über Query-Parameter: cmdr, system, tickid, type, faction, squadron_rank, period.
    """
    try:
        # Filter-Parameter auslesen
        cmdr = request.args.get("cmdr")
        system = request.args.get("system")
        tickid = request.args.get("tickid")
        voucher_type = request.args.get("type", "bounty")
        faction = request.args.get("faction")
        squadron_rank = request.args.get("squadron_rank")
        period = request.args.get("period", "all")

        # Zeitraum-Filter
        from datetime import datetime, timedelta
        from dateutil.relativedelta import relativedelta
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

        where_clauses = ["rv.type = :voucher_type"]
        params = {"voucher_type": voucher_type}

        if cmdr:
            where_clauses.append("e.cmdr = :cmdr")
            params["cmdr"] = cmdr
        if system:
            where_clauses.append("e.starsystem = :system")
            params["system"] = system
        if tickid:
            where_clauses.append("e.tickid = :tickid")
            params["tickid"] = tickid
        if faction:
            where_clauses.append("rv.faction = :faction")
            params["faction"] = faction
        if squadron_rank:
            where_clauses.append("c.squadron_rank = :squadron_rank")
            params["squadron_rank"] = squadron_rank
        if start and end:
            where_clauses.append("e.timestamp BETWEEN :start AND :end")
            params["start"] = start.strftime('%Y-%m-%dT00:00:00Z')
            params["end"] = end.strftime('%Y-%m-%dT23:59:59Z')

        where_sql = " AND ".join(where_clauses)

        sql = f"""
            SELECT
                e.cmdr,
                c.squadron_rank,
                e.starsystem AS system,
                e.timestamp,
                e.tickid,
                rv.amount,
                rv.type,
                rv.faction
            FROM redeem_voucher_event rv
            JOIN event e ON e.id = rv.event_id
            LEFT JOIN cmdr c ON c.name = e.cmdr
            WHERE {where_sql}
            ORDER BY e.timestamp DESC
        """

        result = db.session.execute(text(sql), params).fetchall()
        data = [dict(row._mapping) for row in result]
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/syntheticcz-summary", methods=["GET"])
@require_api_key
def syntheticcz_summary():
    """
    Gibt SyntheticCZ-Events gruppiert nach StarSystem, Faction, CZ-Type und Cmdr zurück, mit Zeitfilter.
    """
    try:
        period = request.args.get("period", "all")
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

        if start and end:
            date_filter = f"e.timestamp BETWEEN '{start.strftime('%Y-%m-%dT00:00:00Z')}' AND '{end.strftime('%Y-%m-%dT23:59:59Z')}'"
        else:
            date_filter = "1=1"

        sql = f"""
            SELECT
                e.starsystem AS starsystem,
                scz.faction,
                scz.cz_type,
                e.cmdr,
                COUNT(*) AS cz_count
            FROM synthetic_cz scz
            JOIN event e ON e.id = scz.event_id
            WHERE {date_filter}
            GROUP BY e.starsystem, scz.faction, scz.cz_type, e.cmdr
            ORDER BY cz_count DESC
        """

        result = db.session.execute(text(sql)).fetchall()
        data = [dict(row._mapping) for row in result]
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/syntheticgroundcz-summary", methods=["GET"])
@require_api_key
def syntheticgroundcz_summary():
    """
    Gibt SyntheticGroundCZ-Events gruppiert nach StarSystem, Faction, Settlement, CZ-Type und Cmdr zurück, mit Zeitfilter.
    """
    try:
        period = request.args.get("period", "all")
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

        if start and end:
            date_filter = f"e.timestamp BETWEEN '{start.strftime('%Y-%m-%dT00:00:00Z')}' AND '{end.strftime('%Y-%m-%dT23:59:59Z')}'"
        else:
            date_filter = "1=1"

        sql = f"""
            SELECT
                e.starsystem AS starsystem,
                sgcz.faction,
                sgcz.settlement,
                sgcz.cz_type,
                e.cmdr,
                COUNT(*) AS cz_count
            FROM synthetic_ground_cz sgcz
            JOIN event e ON e.id = sgcz.event_id
            WHERE {date_filter}
            GROUP BY e.starsystem, sgcz.faction, sgcz.settlement, sgcz.cz_type, e.cmdr
            ORDER BY cz_count DESC
        """

        result = db.session.execute(text(sql)).fetchall()
        data = [dict(row._mapping) for row in result]
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("Starting BGS Data API...")
    with app.app_context():
        # Fallback-DB: Erstelle alle Tabellen, wenn sie nicht existieren
        db.create_all()
        # Initialisiere den Tick-Wert
        get_latest_tickid()

    from eic_shoutout_scheduler import start_scheduler
    start_scheduler(app, db)
    from fdev_tick_monitor import start_tick_watch_scheduler, first_tick_check
    first_tick_check()
    start_tick_watch_scheduler()

    # TODO: Multi-Tenant: Conflict Scheduler für jeden Tenant starten
    from eic_conflict_scheduler import start_eic_conflict_scheduler
    start_eic_conflict_scheduler(app, db)

    from cmdr_sync_inara import start_cmdr_sync_scheduler
    start_cmdr_sync_scheduler(app, db)
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
