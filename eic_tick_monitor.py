import logging
import requests
import os
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

# Discord webhook URL for sending to Bullis' Discord channel
#DISCORD_CONFLICT_WEBHOOK = os.getenv("DISCORD_BULLIS_WEBHOOK_PROD")

# Discord webhook URL for sending to EICs' BGS Discord channel
DISCORD_CONFLICT_WEBHOOK = os.getenv("DISCORD_CONFLICT_WEBHOOK_PROD")

# API key for authentication
API_KEY = os.getenv("API_KEY_PROD")
API_VERSION = os.getenv("API_VERSION_PROD")

# Flask server URL
FLASK_SERVER_URL = os.getenv("FLASK_SERVER_URL_PROD")

# Options for the scheduler
BGS_TICK_ANNOUNCEMENT = os.getenv("BGS_TICK_ANNOUNCEMENT", "true").lower() == "true"

def on_tick_change():
    """
    Triggered when a new tick is detected. Sends an announcement and a conflict message to Discord
    """
    try:
        if BGS_TICK_ANNOUNCEMENT:
            send_tick_announcement()

        logging.info("[TickTriggerEIC] Tick change detected, sending conflict report to Discord")
        response = requests.post(
            FLASK_SERVER_URL + "/api/discord/eic-in-conflict-current-tick",
            headers={"apikey": API_KEY}
        )
        if response.status_code == 200:
            logging.info("[TickTriggerEIC] Conflict report sent successfully")
        else:
            logging.warning(f"[TickTriggerEIC] Discord conflict response failed: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"[TickTriggerEIC] Exception while sending conflict report: {e}")


def send_tick_announcement():
    """
    Sends a short Discord message announcing the detection of a new BGS tick.
    """
    try:
        message = {
            "content": "**✅ New Events (EIC) Tick Change registered.**"
        }
        # If you want to include the tick time, uncomment the next line
        # message["content"] += f"\nTime: `{last_tick['value']}`"
        logging.info("[TickTriggerEIC] Events Tick change detected, sending tick announcement to Discord")
        response = requests.post(DISCORD_CONFLICT_WEBHOOK, json=message)
        if response.status_code == 204:
            logging.info("[TickTriggerEIC] Events Tick announcement sent")
        else:
            logging.warning(f"[TickTriggerEIC] Failed to send announcement: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"[TickTriggerEIC] Exception during tick announcement: {e}")
