import requests
import logging
import atexit
import os
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import json

load_dotenv()

# Tenant-Konfiguration laden
TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    tenant_data = json.load(f)
    # Ensure TENANTS is always a list for consistent iteration
    TENANTS = [tenant_data] if isinstance(tenant_data, dict) else tenant_data
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
                last_tick["value"] = new_tick
                if send_discord_notice:
                    logging.info("[TickPollZoy] Sending tick notice to Discord...")
                    send_tick_notice(new_tick)
            else:
                logging.info("[TickPollZoy] No change in tick.")
        except Exception as e:
            logging.error(f"[TickPollZoy] Failed to fetch or process galtick.json: {e}")

    def send_tick_notice(tick_time):
        """
        Sends a notification to all tenant Discord webhooks when a new tick is detected.
        """
        message = {
            "content": f"**✅ New FDEV (Zoy) BGS Tick detected!**\nTime: `{tick_time}`"
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

    scheduler.add_job(poll_tick_info, IntervalTrigger(minutes=5))
    scheduler.start()
    logging.info("[SchedulerTickPoll] FEDV (Zoy) Tick polling started every 5 minutes.")
    atexit.register(lambda: scheduler.shutdown())
