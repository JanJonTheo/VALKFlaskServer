from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
import atexit
import requests
import os
import json


def start_fac_conflict_scheduler(app, db):
    scheduler = BackgroundScheduler(timezone="UTC")

    # Lade alle Tenants aus tenant.json
    tenant_config_path = os.path.join(os.path.dirname(__file__), "tenant.json")
    with open(tenant_config_path, "r", encoding="utf-8") as f:
        tenants = json.load(f)

    flask_server_url = os.getenv("FLASK_SERVER_URL_PROD")

    def make_post_conflict_to_discord(tenant):
        def post_conflict_to_discord():
            try:
                logging.info(f"[Scheduler] Triggering /api/discord/fac-in-conflict-current-tick for tenant {tenant.get('name') or tenant.get('api_key')}")
                api_key = tenant.get("api_key")
                api_version = tenant.get("api_version") or os.getenv("API_VERSION_PROD")
                url = f"{flask_server_url}/api/discord/fac-in-conflict-current-tick"
                response = requests.post(
                    url,
                    headers={
                        "apikey": api_key,
                        "apiversion": api_version
                    }
                )
                if response.status_code == 204:
                    logging.info(f"Faction conflict Discord post success for tenant {tenant.get('name') or tenant.get('api_key')}.")
                else:
                    logging.warning(f"Faction conflict Discord post failed for tenant {tenant.get('name') or tenant.get('api_key')}: {response.status_code}, {response.text}")
            except Exception as e:
                logging.error(f"Error in scheduled Faction conflict Discord post for tenant {tenant.get('name') or tenant.get('api_key')}: {e}")
        return post_conflict_to_discord

    # Für jeden Tenant einen eigenen Job anlegen
    for tenant in tenants:
        job_func = make_post_conflict_to_discord(tenant)
        scheduler.add_job(job_func, CronTrigger(hour='0,6,12,18', minute=0))
        logging.info(f"[SchedulerConflict] Faction Conflict scheduled for tenant {tenant.get('name') or tenant.get('api_key')} at 0:00, 6:00, 12:00, 18:00 UTC.")

    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
