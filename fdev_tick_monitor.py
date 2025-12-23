import requests
import logging
import atexit
import os
import json
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

load_dotenv()

# Tenant-Konfiguration laden
TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    TENANTS = json.load(f)

# Persisted tick state (shared across processes)
TICK_STATE_PATH = os.path.join(os.path.dirname(__file__), "last_tick.json")

last_tick = {"value": None}


def _ensure_parent_dir(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass


def _atomic_write_json(path: str, payload: dict) -> None:
    """
    Atomic write to avoid partially written files being read by other processes.
    """
    _ensure_parent_dir(path)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, path)


def persist_tick_to_file(tick_str: str) -> None:
    """
    Persist the current ticktime string so other processes (e.g. eddn_client.py) can read it.
    """
    if not tick_str:
        return
    payload = {
        "value": tick_str,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    try:
        _atomic_write_json(TICK_STATE_PATH, payload)
        logging.info(f"[TickPollZoy] Persisted tick to file: {TICK_STATE_PATH} value={tick_str}")
    except Exception as e:
        logging.error(f"[TickPollZoy] Failed to persist tick to file '{TICK_STATE_PATH}': {e}")


def load_tick_from_file() -> Optional[str]:
    """
    Load persisted ticktime (best-effort). Helpful for cold-start or temporary network issues.
    """
    try:
        if not os.path.exists(TICK_STATE_PATH):
            return None
        with open(TICK_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        val = data.get("value")
        if isinstance(val, str) and val.strip():
            return val.strip()
    except Exception as e:
        logging.warning(f"[TickPollZoy] Failed to read tick from file '{TICK_STATE_PATH}': {e}")
    return None


# Initialize from persisted tick if available (helps cold-start / network hiccups)
_persisted = load_tick_from_file()
if _persisted:
    last_tick["value"] = _persisted
    logging.info(f"[TickPollZoy] Loaded persisted tick from file: {_persisted}")


def get_discord_webhook_for_tenant(tenant, webhook_type="bgs"):
    """Gibt den konfigurierten Discord-Webhook für einen Tenant zurück."""
    return tenant.get("discord_webhooks", {}).get(webhook_type)


def first_tick_check():
    """
    Checks the Zoys' galtick.json file for the first tick and initializes the last_tick variable.
    Also persists the tick to file so other processes (e.g. eddn_client.py) can read it.
    """
    try:
        logging.info("[TickPollZoy] Initial tick check...")
        response = requests.get("http://tick.infomancer.uk/galtick.json", timeout=10)
        response.raise_for_status()
        data = response.json()
        new_tick = data.get("lastGalaxyTick")
        if new_tick:
            last_tick["value"] = new_tick
            persist_tick_to_file(new_tick)
            logging.info(f"[TickPollZoy] Initial tick set to: {new_tick}")
        else:
            logging.error("[TickPollZoy] No tick found in galtick.json")
    except Exception as e:
        logging.error(f"[TickPollZoy] Failed to fetch initial tick: {e}")


def start_tick_watch_scheduler():
    """
    Starts a background scheduler that polls the Zoys' galtick.json file every 5 minutes.
    On tick change:
      - updates last_tick
      - persists tick to file (shared state)
      - optionally sends Discord notice and triggers bucket eval endpoint
    """
    scheduler = BackgroundScheduler(timezone="UTC")

    def _convert_to_iso8601(tick_str: str) -> str:
        """
        Convert tick string to ISO-8601 if possible; otherwise return original.
        """
        if not tick_str:
            return tick_str
        try:
            # already ISO-like?
            if "T" in tick_str and (tick_str.endswith("Z") or "+" in tick_str):
                return tick_str
            # try common formats
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(tick_str, fmt)
                    return dt.isoformat(timespec="seconds") + "Z"
                except Exception:
                    pass
        except Exception:
            pass
        logging.warning(f"[TickPollZoy] Unable to parse ticktime '{tick_str}', using original value as fallback")
        return tick_str

    def _call_bucket_eval_api(prev_tick: str):
        """
        Calls the bucket evaluation endpoint in the Flask server for all tenants.
        Uses ticktime string as tickid parameter (as required).
        """
        iso_tick = _convert_to_iso8601(prev_tick)

        flask_server_url = os.getenv("FLASK_SERVER_URL_PROD")
        if not flask_server_url:
            logging.error("[TickPollZoy] FLASK_SERVER_URL_PROD not configured; cannot call Flask API endpoint.")
            return

        endpoint = "/api/bgs/v3/bucket/discord"
        base_url = flask_server_url.rstrip("/") + endpoint

        for tenant in TENANTS:
            tenant_name = tenant.get("name") or "UnknownTenant"
            apikey = tenant.get("apikey")
            if not apikey:
                logging.warning(f"[TickPollZoy] No apikey for tenant {tenant_name}, skipping bucket eval API call.")
                continue

            url = base_url
            headers = {"X-API-Key": apikey}
            params = {"tickid": iso_tick}

            try:
                logging.info(f"[TickPollZoy] POST {url} for tenant {tenant_name} tickid={iso_tick}")
                resp = requests.post(url, headers=headers, params=params, timeout=30)
                if resp.status_code in (200, 204):
                    logging.info(f"[TickPollZoy] Bucket eval API call succeeded for tenant {tenant_name}: {resp.status_code}")
                else:
                    logging.warning(
                        f"[TickPollZoy] Bucket eval API call failed for tenant {tenant_name}: "
                        f"{resp.status_code} {getattr(resp, 'text', None)}"
                    )
            except Exception as e:
                logging.error(f"[TickPollZoy] Exception while calling bucket eval API for tenant {tenant_name}: {e}")

    def send_tick_notice(tick_time: str):
        """
        Sends a notification to Discord for each tenant using the configured BGS webhook.
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
                r = requests.post(webhook_url, json=message, timeout=15)
                if r.status_code in (200, 204):
                    logging.info(f"[TickPollZoy] FDEV (Zoy) Tick notification sent to Discord ({tenant.get('name')})")
                else:
                    logging.warning(
                        f"[TickPollZoy] Discord returned status {r.status_code} for {tenant.get('name')}: {r.text}"
                    )
            except Exception as e:
                logging.error(
                    f"[TickPollZoy] Exception while sending Discord tick notification for {tenant.get('name')}: {e}"
                )

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
                prev_tick = last_tick.get("value")
                last_tick["value"] = new_tick

                # Persist first so other processes immediately see the new tick
                persist_tick_to_file(new_tick)

                # Send regular discord tick notice if requested
                if send_discord_notice:
                    logging.info("[TickPollZoy] Sending tick notice to Discord...")
                    send_tick_notice(new_tick)

                # Trigger bucket eval for previous tick (if we have one)
                if prev_tick:
                    _call_bucket_eval_api(prev_tick)

            elif not new_tick:
                logging.error("[TickPollZoy] No tick found in galtick.json")
            else:
                logging.info(f"[TickPollZoy] No new tick. Current tick: {last_tick['value']}")
        except Exception as e:
            logging.error(f"[TickPollZoy] Failed to poll tick: {e}")

    scheduler.add_job(poll_tick_info, IntervalTrigger(minutes=5))
    scheduler.start()
    logging.info("[SchedulerTickPoll] FDEV (Zoy) Tick polling started every 5 minutes.")
    atexit.register(lambda: scheduler.shutdown())
