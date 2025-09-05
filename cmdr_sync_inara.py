from models import Cmdr, Event
from datetime import datetime
import requests
import time
import logging
import os
import json
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

load_dotenv()
logger = logging.getLogger(__name__)

# API key for Inara API access (personal key, not shared)
INARA_API_KEY = os.getenv("INARA_API_KEY")

# Tenant-Konfiguration laden
TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    TENANTS = json.load(f)


def fetch_inara_profile(cmdr_name, inara_api_key):
    payload = {
        "header": {
            "appName": "EICChatBot",
            "appVersion": "1.0",
            "isDeveloped": True,
            "APIkey": inara_api_key
        },
        "events": [
            {
                "eventName": "getCommanderProfile",
                "eventTimestamp": datetime.utcnow().isoformat() + "Z",
                "eventData": {
                    "searchName": cmdr_name
                }
            }
        ]
    }

    try:
        response = requests.post("https://inara.cz/inapi/v1/", json=payload)

        if response.status_code != 200:
            logger.error(f"[Inara] HTTP error for Cmdr '{cmdr_name}': {response.status_code} – {response.text}")
            return None

        json_response = response.json()
        status = json_response.get("header", {}).get("eventStatus", 200)
        status_text = json_response.get("header", {}).get("eventStatusText", "")

        if status == 400:
            logger.warning(f"[Inara] API rate-limited: {status_text}")
            return {"_rate_limited": True}

        data = json_response["events"][0]["eventData"]
        ranks = {r["rankName"]: r["rankValue"] for r in data.get("commanderRanksPilot", [])}
        squadron = data.get("commanderSquadron", {})

        return {
            "rank_combat": ranks.get("combat"),
            "rank_trade": ranks.get("trade"),
            "rank_explore": ranks.get("exploration"),
            "rank_cqc": ranks.get("cqc"),
            "rank_empire": ranks.get("empire"),
            "rank_federation": ranks.get("federation"),
            "rank_power": data.get("preferredPowerName"),
            "credits": None,
            "assets": None,
            "inara_url": data.get("inaraURL"),
            "squadron_name": squadron.get("squadronName"),
            "squadron_rank": squadron.get("squadronMemberRank")
        }

    except Exception as e:
        logger.error(f"[Inara] Unexpected error for Cmdr '{cmdr_name}': {e}")
        return None


def sync_cmdrs_with_inara(db=None, inara_api_key=None):
    logger.info("[Sync] Starting Cmdr sync with Inara...")

    cmdrs = db.session.query(Event.cmdr).filter(Event.cmdr != None).distinct().limit(100).all()

    for (cmdr_name,) in cmdrs:
        if not cmdr_name:
            continue

        logger.info(f"[Sync] Syncing Cmdr: {cmdr_name}")

        existing = Cmdr.query.filter_by(name=cmdr_name).first()
        profile = fetch_inara_profile(cmdr_name, inara_api_key)

        if profile is not None and profile.get("_rate_limited"):
            logger.warning("[Sync] Inara API rate limit reached – sync aborted.")
            break

        if not profile:
            logger.warning(f"[Sync] No data received for Cmdr: {cmdr_name}")
            continue

        if not existing:
            logger.info(f"[Sync] Adding Cmdr: {cmdr_name}")
            db.session.add(Cmdr(name=cmdr_name, **profile))
        else:
            logger.info(f"[Sync] Updating Cmdr: {cmdr_name}")
            for k, v in profile.items():
                setattr(existing, k, v)

        try:
            db.session.commit()
        except Exception as e:
            logger.error(f"[Sync] Commit failed for Cmdr '{cmdr_name}': {e}")
            db.session.rollback()

        logger.info("[Sync] Slowing down 60s to avoid hitting Inara API rate limits...")
        time.sleep(60)

    logger.info("[Sync] Cmdr sync completed.")


def run_cmdr_sync_task(app, db=None):
    import requests as http
    from io import StringIO

    with app.app_context():
        log_buffer = StringIO()
        handler = logging.StreamHandler(log_buffer)
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)

        try:
            # Multi-Tenant: Für jeden Tenant synchronisieren
            for tenant in TENANTS:
                inara_api_key = tenant.get("inara_api_key") or os.getenv("INARA_API_KEY")
                db_uri = tenant.get("db_uri")
                tenant_name = tenant.get("name") or tenant.get("api_key")
                # NEU: Discord Bullis Webhook pro Tenant
                discord_bullis_url = tenant.get("discord_webhooks", {}).get("bullis")
                if not db_uri or not inara_api_key:
                    logger.warning(f"[Sync] Tenant {tenant_name} ohne DB-URI oder INARA_API_KEY, überspringe.")
                    continue
                url = make_url(db_uri)
                is_sqlite = url.drivername == "sqlite"
                connect_args = {"check_same_thread": False} if is_sqlite else {}
                engine = create_engine(db_uri, connect_args=connect_args)
                Session = sessionmaker(bind=engine)
                session = Session()
                logger.info(f"[Sync] Starte Cmdr-Sync für Tenant: {tenant_name}")
                try:
                    sync_cmdrs_with_inara(db=session, inara_api_key=inara_api_key)
                finally:
                    session.close()

                # Discord-Nachricht pro Tenant senden
                log_text = log_buffer.getvalue()
                log_lines = [line for line in log_text.splitlines() if "[Sync]" in line]
                summary = "\n".join(log_lines[-25:]) or "No Cmdrs synced."
                content = f"🧠 **Daily Cmdr Sync für {tenant_name}**\n```text\n{summary}\n```"
                if len(content) > 1900:
                    content = content[:1890] + "\n...```"
                if discord_bullis_url:
                    try:
                        resp = http.post(discord_bullis_url, json={"content": content})
                        if resp.status_code != 204:
                            logger.warning(f"[SyncTask] Discord failed for {tenant_name}: {resp.status_code} {resp.text}")
                    except Exception as e:
                        logger.error(f"[SyncTask] Discord send error for {tenant_name}: {e}")

        except Exception as e:
            logger.error(f"[SyncTask] Unexpected error: {e}")

        logger.removeHandler(handler)


def start_cmdr_sync_scheduler(app, db):
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: run_cmdr_sync_task(app, db),
        trigger="cron",
        hour=3, minute=0,
        id="cmdr_sync_daily",
        replace_existing=True
    )
    scheduler.start()
    logger.info("[SchedulerSync] Cmdr sync scheduled daily at 03:00.")
