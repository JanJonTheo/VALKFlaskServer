from flask import Flask, request, jsonify, g
from sqlalchemy.exc import OperationalError
from sqlalchemy import create_engine, text, func, desc
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.engine import make_url
from models import *
import logging
import logging.handlers
from functools import wraps
import bcrypt
from sqlalchemy import text
import requests as http_requests
from cmdr_sync_inara import sync_cmdrs_with_inara
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import os
import json
import ast

# Lade Umgebungsvariablen aus .env
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
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logfile_path = os.path.join(LOG_DIR, "app.log")
logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.handlers.RotatingFileHandler(logfile_path, maxBytes=128 * 1024 * 1024, backupCount=10),
        logging.StreamHandler()
    ],
    format='%(asctime)s %(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger(__name__)

# Mutable container to hold last known tickid
last_known_tickid = {"value": None}

app = Flask(__name__)

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

        return jsonify({"status": "success"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Event processing error: {str(e)}")
        logger.error(f"Event Request Json: {str(request.get_json())}")
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
            AND mci.faction_name LIKE :faction_name_like
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

    params = {}
    if "faction_name LIKE :faction_name_like" in sql:
        params["faction_name_like"] = f"%{g.tenant['faction_name']}%"

    try:
        result = db.session.execute(text(sql), params).fetchall()
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
            WHERE e.cmdr IS NOT NULL 
            AND mci.faction_name LIKE :faction_name_like
            AND {date_filter}
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

    params = {}
    if "faction_name LIKE :faction_name_like" in sql:
        params["faction_name_like"] = f"%{g.tenant['faction_name']}%"

    try:
        result = db.session.execute(text(sql), params).fetchall()
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


@app.route("/api/fsdjump-factions", methods=["GET"])
@require_api_key
def fsdjump_factions():
    """
    Gibt für jedes in den letzten 24h besuchte StarSystem (FSDJump, jeweils aktuellster Datensatz pro System)
    eine Liste aller Factions im System mit deren aktuellen Daten zurück.
    """
    try:
        since = datetime.utcnow() - timedelta(hours=24)
        subq = (
            db.session.query(
                func.max(Event.id).label("id")
            )
            .filter(Event.event == "FSDJump")
            .filter(Event.timestamp >= since)
            .group_by(Event.starsystem)
            .subquery()
        )
        events = (
            db.session.query(Event)
            .filter(Event.id.in_(subq))
            .all()
        )
        result = []
        for event in events:
            raw = event.raw_json
            if not raw:
                continue
            raw_json = None
            try:
                raw_json = ast.literal_eval(raw)
            except Exception as ex:
                logger.warning(f"Error parsing raw_json for Event {event.id}: {ex}")
                continue
            if not raw_json or "Factions" not in raw_json or not raw_json.get("StarSystem"):
                continue
            factions = []
            for fac in raw_json["Factions"]:
                factions.append({
                    "Name": fac.get("Name"),
                    "FactionState": fac.get("FactionState"),
                    "Government": fac.get("Government"),
                    "Influence": fac.get("Influence"),
                    "Allegiance": fac.get("Allegiance"),
                    "Happiness": fac.get("Happiness"),
                    "MyReputation": fac.get("MyReputation"),
                    "PendingStates": fac.get("PendingStates", []),
                    "RecoveringStates": fac.get("RecoveringStates", []),
                })
            result.append({
                "StarSystem": raw_json.get("StarSystem"),
                "SystemAddress": raw_json.get("SystemAddress"),
                "Timestamp": raw_json.get("timestamp"),
                "Factions": factions
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"FSDJump-Factions-API Error: {str(e)}")
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
                WHERE e.cmdr IS NOT NULL 
                AND mci.faction_name LIKE :faction_name_like
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
        tenant = g.tenant
        db_uri = tenant.get("db_uri")
        tenant_name = tenant.get("name") or tenant.get("api_key")
        webhook_url = tenant.get("discord_webhooks", {}).get("shoutout")
        if not db_uri or not webhook_url:
            results.append({"tenant": tenant_name, "status": "skipped", "reason": "No DB URI or webhook"})
            return jsonify(results), 200
        url = make_url(db_uri)
        is_sqlite = url.drivername == "sqlite"
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        engine = create_engine(db_uri, connect_args=connect_args)
        with engine.connect() as conn:
            sections = []
            for title, q in base_queries.items():
                params = {}
                # Dynamischer LIKE-Parameter für "Influence EIC"
                if title == "Influence EIC":
                    faction_name = tenant.get("faction_name")
                    params["faction_name_like"] = f"%{faction_name}%"
                rows = conn.execute(text(q["sql"]), params).fetchall()
                if not rows:
                    continue
                section = f"**📊 {title}**\n```text\n{q['format'](rows)}\n```"
                sections.append(section)
            if not sections:
                results.append({"tenant": tenant_name, "status": "no data"})
                return jsonify(results), 200
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
        from fac_shoutout_scheduler import format_discord_summary
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
        from fac_shoutout_scheduler import send_syntheticcz_summary_to_discord
        # Nur für den aktuellen Tenant senden
        send_syntheticcz_summary_to_discord(app, db, period, tenant=g.tenant)
        tenant_name = g.tenant.get("name") or g.tenant.get("api_key")
        return jsonify({"status": f"SyntheticCZ-Summary für Tenant: {tenant_name} via Discord gesendet ({period})"}), 200
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
        from fac_shoutout_scheduler import send_syntheticgroundcz_summary_to_discord
        # Nur für den aktuellen Tenant senden
        send_syntheticgroundcz_summary_to_discord(app, db, period, tenant=g.tenant)
        tenant_name = g.tenant.get("name") or g.tenant.get("api_key")
        return jsonify({"status": f"SyntheticGroundCZ-Summary für Tenant: {tenant_name} via Discord gesendet ({period})"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Register EIC Conflict routes
from fac_in_conflict import register_fac_conflict_routes
register_fac_conflict_routes(app, db, require_api_key)


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
        apikey = request.headers.get("apikey")
        tenant = get_tenant_by_apikey(apikey)
        if not tenant:
            logger.warning(f"Invalid API-Key received for login: {apikey}")
            return jsonify({"error": "Unauthorized: Invalid API key"}), 401

        g.tenant = tenant
        set_tenant_db_config(tenant)

        if hasattr(g, "tenant_db_error"):
            logger.error(f"Tenant-Datenbankfehler beim Login: {g.tenant_db_error}")
            return jsonify({"error": f"Tenant-Datenbank nicht gefunden oder nicht erreichbar: {g.tenant_db_error}"}), 500

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
                "is_admin": bool(is_admin),
                "tenant_name": tenant.get("name")
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
                 WHERE ex.cmdr = e.cmdr
                 AND mci.faction_name LIKE :faction_name_like
                 AND {date_filter_sub}
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

        params = {}
        if "faction_name LIKE :faction_name_like" in sql:
            params["faction_name_like"] = f"%{g.tenant['faction_name']}%"

        result = db.session.execute(text(sql), params).fetchall()
        data = [dict(row._mapping) for row in result]
        return jsonify(data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system-summary/", defaults={"system_name": None}, methods=["GET"])
@app.route("/api/system-summary/<system_name>", methods=["GET"])
@require_api_key
def system_summary(system_name):
    """
    Gibt eine Systemzusammenfassung aus der in .env konfigurierten bgs_data_eddn zurück.
    Optional können Query-Parameter genutzt werden:
    - faction: alle Systeme, in denen diese Faction präsent ist
    - controlling_faction: alle Systeme, die von dieser Faction kontrolliert werden
    - controlling_power: alle Systeme, die von dieser Power kontrolliert werden
    - power: alle Systeme, die von diesem Power beeinflusst werden
    - state: alle Systeme, in denen eine Faction mit diesem State (state oder active_states) präsent ist
    - recovering_state: alle Systeme, in denen eine Faction mit diesem Recovering State präsent ist
    - pending_state: alle Systeme, in denen eine Faction mit diesem Pending State präsent ist
    - has_conflict: true/1 → alle Systeme mit mindestens einem Conflict
    - population: alle Systeme mit dieser Population (exakt oder Bereich, z.B. 1000000-2000000)
    - powerplay_state: alle Systeme mit diesem Powerplay-State (aus eddn_powerplay)
    - system_name: expliziter Systemname (optional)
    """
    try:
        eddn_db_uri = os.getenv("EDDN_DATABASE")
        if not eddn_db_uri:
            return jsonify({"error": "EDDN_DATABASE not configured in .env"}), 500

        eddn_engine = create_engine(eddn_db_uri)
        params = request.args
        faction = params.get("faction")
        controlling_faction = params.get("controlling_faction")
        controlling_power = params.get("controlling_power")
        power = params.get("power")
        state = params.get("state")
        recovering_state = params.get("recovering_state")
        pending_state = params.get("pending_state")
        has_conflict = params.get("has_conflict")
        population = params.get("population")
        powerplay_state = params.get("powerplay_state")

        with eddn_engine.connect() as conn:
            # Falls einer der Filter gesetzt ist oder kein Systemname angegeben ist, Systemliste liefern
            if any([faction, controlling_faction, controlling_power, power, state, recovering_state, pending_state, has_conflict, population, powerplay_state]) or not system_name:
                systems = None

                # Faction-Präsenz
                if faction:
                    rows = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_faction WHERE name = :faction COLLATE NOCASE"),
                        {"faction": faction}
                    ).fetchall()
                    systems = set(r[0] for r in rows) if systems is None else systems & set(r[0] for r in rows)

                # Kontrollierende Faction
                if controlling_faction:
                    rows = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_system_info WHERE controlling_faction = :cf COLLATE NOCASE"),
                        {"cf": controlling_faction}
                    ).fetchall()
                    systems = set(r[0] for r in rows) if systems is None else systems & set(r[0] for r in rows)

                # Kontrollierende Power
                if controlling_power:
                    rows = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_system_info WHERE controlling_power = :cp COLLATE NOCASE"),
                        {"cp": controlling_power}
                    ).fetchall()
                    systems = set(r[0] for r in rows) if systems is None else systems & set(r[0] for r in rows)

                # Power (aus SystemInfo oder Powerplay)
                if power:
                    # SystemInfo
                    rows_si = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_system_info WHERE controlling_power = :power COLLATE NOCASE"),
                        {"power": power}
                    ).fetchall()
                    # Powerplay (JSON-Array)
                    rows_pp = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_powerplay WHERE json_extract(power, '$[0]') = :power COLLATE NOCASE OR power LIKE :power_like"),
                        {"power": power, "power_like": f'%{power}%'}
                    ).fetchall()
                    power_systems = set(r[0] for r in rows_si) | set(r[0] for r in rows_pp)
                    systems = power_systems if systems is None else systems & power_systems

                # State (aus state oder active_states)
                if state:
                    # state
                    rows_state = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_faction WHERE state = :state COLLATE NOCASE"),
                        {"state": state}
                    ).fetchall()
                    # active_states (JSON-Array)
                    rows_active = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_faction WHERE active_states LIKE :state_like"),
                        {"state_like": f'%{state}%'}
                    ).fetchall()
                    state_systems = set(r[0] for r in rows_state) | set(r[0] for r in rows_active)
                    systems = state_systems if systems is None else systems & state_systems

                # Recovering State (JSON-Array)
                if recovering_state:
                    rows = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_faction WHERE recovering_states LIKE :rec_like"),
                        {"rec_like": f'%{recovering_state}%'}
                    ).fetchall()
                    rec_systems = set(r[0] for r in rows)
                    systems = rec_systems if systems is None else systems & rec_systems

                # Pending State (JSON-Array)
                if pending_state:
                    rows = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_faction WHERE pending_states LIKE :pending_like"),
                        {"pending_like": f'%{pending_state}%'}
                    ).fetchall()
                    pending_systems = set(r[0] for r in rows)
                    systems = pending_systems if systems is None else systems & pending_systems

                # Conflict
                if has_conflict and has_conflict.lower() in ("1", "true", "yes"):
                    rows = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_conflict")
                    ).fetchall()
                    conflict_systems = set(r[0] for r in rows)
                    systems = conflict_systems if systems is None else systems & conflict_systems

                # Population (exakt oder Bereich)
                if population:
                    pop_systems = set()
                    if "-" in population:
                        try:
                            pop_min, pop_max = map(int, population.split("-", 1))
                            rows = conn.execute(
                                text("SELECT DISTINCT system_name FROM eddn_system_info WHERE population >= :pop_min AND population <= :pop_max"),
                                {"pop_min": pop_min, "pop_max": pop_max}
                            ).fetchall()
                            pop_systems = set(r[0] for r in rows)
                        except Exception:
                            pass
                    else:
                        try:
                            pop_val = int(population)
                            rows = conn.execute(
                                text("SELECT DISTINCT system_name FROM eddn_system_info WHERE population = :pop_val"),
                                {"pop_val": pop_val}
                            ).fetchall()
                            pop_systems = set(r[0] for r in rows)
                        except Exception:
                            pass
                    systems = pop_systems if systems is None else systems & pop_systems

                # Powerplay State (aus eddn_powerplay)
                if powerplay_state:
                    rows = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_powerplay WHERE powerplay_state = :pps COLLATE NOCASE"),
                        {"pps": powerplay_state}
                    ).fetchall()
                    pps_systems = set(r[0] for r in rows)
                    systems = pps_systems if systems is None else systems & pps_systems

                # Wenn kein Filter gesetzt und kein Systemname: Alle Systeme
                if systems is None:
                    rows = conn.execute(
                        text("SELECT DISTINCT system_name FROM eddn_system_info")
                    ).fetchall()
                    systems = set(r[0] for r in rows)

                # Wenn mehr als 100 Systeme gefunden wurden, Hinweis und keine Details laden
                if len(systems) > 100:
                    return jsonify({
                        "error": "Zu viele Systeme gefunden. Bitte schränken Sie die Filter weiter ein.",
                        "count": len(systems),
                        "systems": sorted(list(systems))[:100]
                    }), 400

                # Für alle gefundenen Systeme Details liefern
                result = []
                for sysname in systems:
                    sys_row = conn.execute(
                        text("SELECT * FROM eddn_system_info WHERE system_name = :name COLLATE NOCASE"),
                        {"name": sysname}
                    ).mappings().first()
                    if not sys_row:
                        continue
                    sys_result = {"system_info": dict(sys_row)}
                    # Details
                    for table, key in [
                        ("eddn_conflict", "conflicts"),
                        ("eddn_faction", "factions"),
                        ("eddn_powerplay", "powerplays"),
                    ]:
                        rows = conn.execute(
                            text(f"SELECT * FROM {table} WHERE system_name = :name COLLATE NOCASE"),
                            {"name": sysname}
                        ).mappings().all()
                        sys_result[key] = [dict(r) for r in rows]
                    result.append(sys_result)
                return jsonify(result)

            # Standard: Einzelnes System
            sys_row = conn.execute(
                text("SELECT * FROM eddn_system_info WHERE system_name = :name COLLATE NOCASE"),
                {"name": system_name}
            ).mappings().first()
            if not sys_row:
                return jsonify({"error": f"System '{system_name}' not found"}), 404

            result = {"system_info": dict(sys_row)}
            for table, key in [
                ("eddn_conflict", "conflicts"),
                ("eddn_faction", "factions"),
                ("eddn_powerplay", "powerplays"),
            ]:
                rows = conn.execute(
                    text(f"SELECT * FROM {table} WHERE system_name = :name COLLATE NOCASE"),
                    {"name": system_name}
                ).mappings().all()
                result[key] = [dict(r) for r in rows]

            return jsonify(result)
    except Exception as e:
        logger.error(f"system_summary error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/protected-faction", methods=["GET"])
@require_api_key
def get_protected_factions():
    """Liefert alle geschützten Factions für den aktuellen Tenant."""
    try:
        factions = ProtectedFaction.query.all()
        return jsonify([{
            "id": f.id,
            "name": f.name,
            "webhook_url": f.webhook_url,
            "description": f.description,
            "protected": f.protected
        } for f in factions])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/protected-faction/<int:faction_id>", methods=["GET"])
@require_api_key
def get_protected_faction(faction_id):
    """Liefert eine einzelne geschützte Faction anhand der ID."""
    try:
        faction = ProtectedFaction.query.get(faction_id)
        if not faction:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "id": faction.id,
            "name": faction.name,
            "webhook_url": faction.webhook_url,
            "description": faction.description,
            "protected": faction.protected
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/protected-faction", methods=["POST"])
@require_api_key
def create_protected_faction():
    """Erstellt eine neue geschützte Faction."""
    try:
        data = request.get_json()
        faction = ProtectedFaction(
            name=data["name"],
            webhook_url=data.get("webhook_url"),
            description=data.get("description"),
            protected=bool(data.get("protected", True))
        )
        db.session.add(faction)
        db.session.commit()
        return jsonify({"id": faction.id}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400

@app.route("/api/protected-faction/<int:faction_id>", methods=["PUT"])
@require_api_key
def update_protected_faction(faction_id):
    """Aktualisiert eine geschützte Faction."""
    try:
        data = request.get_json()
        faction = ProtectedFaction.query.get(faction_id)
        if not faction:
            return jsonify({"error": "Not found"}), 404
        faction.name = data.get("name", faction.name)
        faction.webhook_url = data.get("webhook_url", faction.webhook_url)
        faction.description = data.get("description", faction.description)
        if "protected" in data:
            faction.protected = bool(data["protected"])
        db.session.commit()
        return jsonify({"status": "updated"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400

@app.route("/api/protected-faction/<int:faction_id>", methods=["DELETE"])
@require_api_key
def delete_protected_faction(faction_id):
    """Löscht eine geschützte Faction."""
    try:
        faction = ProtectedFaction.query.get(faction_id)
        if not faction:
            return jsonify({"error": "Not found"}), 404
        db.session.delete(faction)
        db.session.commit()
        return jsonify({"status": "deleted"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400


@app.route("/api/protected-faction/systems", methods=["GET"])
@require_api_key
def get_all_system_names():
    """
    Gibt eine Liste aller Systemnamen aus der Tabelle eddn_system_info zurück.
    Die Datenbank wird aus der .env-Variable EDDN_DATABASE gelesen.
    """
    try:
        eddn_db_uri = os.getenv("EDDN_DATABASE")
        if not eddn_db_uri:
            return jsonify({"error": "EDDN_DATABASE not configured in .env"}), 500

        engine = create_engine(eddn_db_uri)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT DISTINCT system_name FROM eddn_system_info ORDER BY system_name ASC")).fetchall()
            system_names = [row[0] for row in rows if row[0]]
        return jsonify(system_names)
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Systemnamen: {str(e)}")
        return jsonify({"error": str(e)}), 500


def initialize_all_tenant_databases():
    """
    Prüft beim Start für alle Tenants, ob die SQLite-DB-Datei existiert.
    Falls nicht, wird sie samt Tabellenstruktur angelegt.
    Zusätzlich wird die Tabelle protected_faction angelegt, falls sie fehlt.
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy import create_engine
    for tenant in TENANTS:
        db_uri = tenant.get("db_uri")
        if not db_uri:
            continue
        url = make_url(db_uri)
        if url.drivername == "sqlite" and url.database not in (None, "", ":memory:"):
            sqlite_file_path = url.database
            abs_path = os.path.abspath(sqlite_file_path)
            dir_name = os.path.dirname(abs_path)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name, exist_ok=True)
            if not os.path.exists(abs_path):
                # Engine mit passenden connect_args für SQLite
                engine = create_engine(db_uri, connect_args={"check_same_thread": False})
                # Tabellenstruktur anlegen
                with engine.begin() as conn:
                    db.Model.metadata.create_all(bind=conn)
                logger.info(f"Tenant-DB initialisiert: {abs_path}")
        # Update existing Tenant DB
        from update_db_tenant import ensure_protected_faction_table
        ensure_protected_faction_table(db_uri)


if __name__ == "__main__":
    print("Starting BGS Data API...")
    with app.app_context():
        # Multi-Tenant: Prüfe und initialisiere alle Tenant-DBs und protected_faction-Tabellen
        initialize_all_tenant_databases()
        # Initialisiere den Tick-Wert
        get_latest_tickid()

    # EDDN-Client als Subprozess starten
    import multiprocessing
    from eddn_client import main as eddn_main
    print("Starting EDDN Client...")
    eddn_process = multiprocessing.Process(target=eddn_main, daemon=True)
    eddn_process.start()

    # Shoutout Scheduler starten
    from fac_shoutout_scheduler import start_scheduler
    start_scheduler(app, db)

    # Tick-Watch Scheduler starten
    from fdev_tick_monitor import start_tick_watch_scheduler, first_tick_check
    first_tick_check()
    start_tick_watch_scheduler()

    # TODO: Multi-Tenant: Conflict Scheduler für jeden Tenant starten
    from fac_conflict_scheduler import start_fac_conflict_scheduler
    start_fac_conflict_scheduler(app, db)

    # Inara Cmdr Sync Scheduler starten
    from cmdr_sync_inara import start_cmdr_sync_scheduler
    start_cmdr_sync_scheduler(app, db)
    app.run(host='0.0.0.0', port=5555, debug=False, use_reloader=False)
