from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
import atexit
import requests


def start_eic_conflict_scheduler(app, db):
    scheduler = BackgroundScheduler(timezone="UTC")

    def post_conflict_to_discord():
        try:
            logging.info("[Scheduler] Triggering /api/discord/eic-in-conflict-current-tick")
            response = requests.post(
                "http://167.235.65.113:5000/api/discord/eic-in-conflict-current-tick",
                headers={
                    "apikey": "churchoficarus",
                    "apiversion": "1.6.0"  # Header hinzugefügt
                }
            )
            # Change to 167.235.65.113 in production, localhost for testing
            if response.status_code == 204:
                logging.info("EIC conflict Discord post success.")
            else:
                logging.warning(f"EIC conflict Discord post failed: {response.status_code}, {response.text}")
        except Exception as e:
            logging.error(f"Error in scheduled conflict Discord post: {e}")

    # Run every 6 hours at 0:00, 6:00, 12:00, 18:00 UTC
    scheduler.add_job(post_conflict_to_discord, CronTrigger(hour='0,6,12,18', minute=0))
    scheduler.start()
    logging.info("[SchedulerConflict] EIC Conflict scheduled at 0:00, 6:00, 12:00, 18:00 UTC.")
    atexit.register(lambda: scheduler.shutdown())
