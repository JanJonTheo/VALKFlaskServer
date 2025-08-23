import logging
import requests

# Discord webhook URL for sending to Bullis' Discord channel
#DISCORD_CONFLICT_WEBHOOK = "https://discord.com/api/webhooks/1387032877161779274/jW1H6hEe9P66lkxx96h8uLFCKkJ8lmmm4WWLbMJ5UrUVonAtlVuVWQN5v0vq571XydAA"

# Discord webhook URL for sending to EICs' BGS Discord channel
DISCORD_CONFLICT_WEBHOOK = "https://discord.com/api/webhooks/1387701927852249150/W7YxWFgk0JeAnBmKWSABzzc7SRJj8UQa3aJUhed-Vm5KMg9V8uz7JXf-i88jSLzDW144"


def on_tick_change():
    """
    Triggered when a new tick is detected. Sends an announcement and a conflict message to Discord
    """
    try:
        send_tick_announcement()
        logging.info("[TickTriggerEIC] Tick change detected, sending conflict report to Discord")
        response = requests.post(
            "http://167.235.65.113:5000/api/discord/eic-in-conflict-current-tick",
            headers={"apikey": "churchoficarus"}
        )
        # Change to 167.235.65.113 in production, localhost for testing
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
