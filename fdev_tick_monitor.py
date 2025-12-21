import requests
import logging
import atexit
import os
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import json
from typing import Optional

load_dotenv()

# Tenant-Konfiguration laden
TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    TENANTS = json.load(f)

last_tick = {"value": None}


def get_discord_webhook_for_tenant(tenant, webhook_type="bgs"):
    """Gibt den konfigurierten Discord-Webhook für einen Tenant zurück."""
    return tenant.get("discord_webhooks", {}).get(webhook_type)


def first_tick_check():
    """
    Checks the Zoys' galtick.json file for the first tick and initializes the last_tick variable.
    """
    try:
        logging.info("[TickPollZoy] Initial tick check...")
        response = requests.get("http://tick.infomancer.uk/galtick.json", timeout=10)
        response.raise_for_status()
        data = response.json()
        new_tick = data.get("lastGalaxyTick")
        if new_tick:
            last_tick["value"] = new_tick
            logging.info(f"[TickPollZoy] Initial tick set to: {new_tick}")
        else:
            logging.error("[TickPollZoy] No tick found in galtick.json")
    except Exception as e:
        logging.error(f"[TickPollZoy] Failed to fetch initial tick: {e}")


def start_tick_watch_scheduler():
    """
    Starts a background scheduler that polls the Zoys' galtick.json file every 5 minutes
    """
    scheduler = BackgroundScheduler(timezone="UTC")

    def poll_tick_info(send_discord_notice=True):
        """
        Polls the Zoys' galtick.json file for the latest tick information.
        send_discord_notice: If True, sends a notification to Discord when a new tick is detected.
        """
        try:
            logging.info("[TickPollZoy] Checking Zoys' galtick.json for tick update...")
            response = requests.get("http://tick.infomancer.uk/galtick.json", timeout=10)
            response.raise_for_status()
            data = response.json()
            new_tick = data.get("lastGalaxyTick")
            if new_tick and new_tick != last_tick["value"]:
                logging.info(f"[TickPollZoy] New tick detected: {last_tick['value']} -> {new_tick}")
                # Capture the previous tick (may be None on first run)
                prev_tick = last_tick.get("value")
                # Update stored tick
                last_tick["value"] = new_tick

                # Send regular discord tick notice if requested
                if send_discord_notice:
                    logging.info("[TickPollZoy] Sending tick notice to Discord...")
                    send_tick_notice(new_tick)

                # Trigger BGS bucket evaluation for the *previous* tick (if available)
                if prev_tick:
                    logging.info(f"[TickPollZoy] Triggering BGS bucket evaluation for previous tick {prev_tick}")
                    send_bucket_eval_for_prev_tick(prev_tick)
                else:
                    logging.info("[TickPollZoy] No previous tick available; skipping bucket evaluation trigger.")
            else:
                logging.info("[TickPollZoy] No change in tick.")
        except Exception as e:
            logging.error(f"[TickPollZoy] Failed to fetch or process galtick.json: {e}")

    def send_tick_notice(tick_time):
        """
        Sends a notification to all tenant Discord webhooks when a new tick is detected.
        """
        message = {
            "content": f"**✅ New FDEV (Zoy) BGS Tick detected!**\nTime: `{tick_time}`\n\u200B"
        }
        for tenant in TENANTS:
            webhook_url = get_discord_webhook_for_tenant(tenant, "bgs")
            if not webhook_url:
                logging.warning(f"[TickPollZoy] Kein BGS-Webhook für Tenant {tenant.get('name')}, überspringe.")
                continue
            try:
                r = requests.post(webhook_url, json=message)
                if r.status_code in (200, 204):
                    logging.info(f"[TickPollZoy] FDEV (Zoy) Tick notification sent to Discord ({tenant.get('name')})")
                else:
                    logging.warning(f"[TickPollZoy] Discord returned status {r.status_code} for {tenant.get('name')}: {r.text}")
            except Exception as e:
                logging.error(f"[TickPollZoy] Exception while sending Discord notification for {tenant.get('name')}: {e}")

    def send_bucket_eval_for_prev_tick(prev_tick: Optional[str]):
        """Call the Flask endpoint /api/bgs/v3/bucket/bounty/discord for each tenant with tickid=prev_tick.
        Uses the tenant's apikey (and api_version) in headers so the Flask app resolves g.tenant server-side.
        """
        logging.info(f"[TickPollZoy] send_bucket_eval_for_prev_tick: triggering bucket eval for ticktime {prev_tick} on all tenants")

        flask_server_url = os.getenv("FLASK_SERVER_URL_PROD")
        if not flask_server_url:
            logging.error("[TickPollZoy] FLASK_SERVER_URL_PROD not configured; cannot call Flask API endpoint.")
            return

        endpoint = "/api/bgs/v3/bucket/bounty/discord"

        for tenant in TENANTS:
            tenant_name = tenant.get('name') or tenant.get('api_key')
            api_key = tenant.get('api_key')
            if not api_key:
                logging.warning(f"[TickPollZoy] Tenant {tenant_name} has no api_key; skipping API call.")
                continue
            api_version = tenant.get('api_version') or os.getenv('API_VERSION_PROD')

            url = f"{flask_server_url}{endpoint}"
            headers = {
                "apikey": api_key,
                "apiversion": api_version
            }
            params = {"ticktime": prev_tick}

            try:
                logging.info(f"[TickPollZoy] POST {url} for tenant {tenant_name} tickid={prev_tick}")
                resp = requests.post(url, headers=headers, params=params, timeout=30)
                if resp.status_code in (200, 204):
                    logging.info(f"[TickPollZoy] Bucket eval API call succeeded for tenant {tenant_name}: {resp.status_code}")
                else:
                    logging.warning(f"[TickPollZoy] Bucket eval API call failed for tenant {tenant_name}: {resp.status_code} {getattr(resp,'text',None)}")
            except Exception as e:
                logging.error(f"[TickPollZoy] Exception while calling bucket eval API for tenant {tenant_name}: {e}")

    scheduler.add_job(poll_tick_info, IntervalTrigger(minutes=5))
    scheduler.start()
    logging.info("[SchedulerTickPoll] FEDV (Zoy) Tick polling started every 5 minutes.")
    atexit.register(lambda: scheduler.shutdown())