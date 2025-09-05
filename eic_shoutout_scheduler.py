from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
import atexit
import requests
from sqlalchemy import text
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()
_scheduler_instance = None


def init_logger():
    log_path = Path(__file__).parent / "tick_scheduler.log"
    print(f"Log Path: {log_path.resolve()}")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            RotatingFileHandler(log_path, maxBytes=128 * 1024 * 1024, backupCount=3),
            logging.StreamHandler()
        ],
        force=True
    )


def get_tenants():
    """Lädt die Tenant-Konfiguration."""
    import os, json
    TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
    with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_discord_webhook(tenant, webhook_type="shoutout"):
    """Gibt den Discord-Webhook für den Tenant zurück."""
    return tenant.get("discord_webhooks", {}).get(webhook_type)


def get_engine_for_tenant(tenant):
    """Erstellt eine SQLAlchemy-Engine für den Tenant."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    db_uri = tenant.get("db_uri")
    if not db_uri:
        return None
    url = make_url(db_uri)
    is_sqlite = url.drivername == "sqlite"
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    return create_engine(db_uri, connect_args=connect_args)


def format_discord_summary(app=None, db=None):
    init_logger()
    tenants = get_tenants()
    from sqlalchemy import text

    today = datetime.utcnow().date()
    start = datetime.combine(today - timedelta(days=1), datetime.min.time())
    end = datetime.combine(today - timedelta(days=1), datetime.max.time())
    start_str = start.isoformat()
    end_str = end.isoformat()

    base_queries = {
        "Market Events": {
            "sql": '''
                   SELECT e.cmdr,
                          SUM(COALESCE(mb.value, 0))                              AS total_buy,
                          SUM(COALESCE(ms.value, 0))                              AS total_sell,
                          SUM(COALESCE(mb.value, 0)) + SUM(COALESCE(ms.value, 0)) AS total_volume,
                          SUM(COALESCE(mb.count, 0)) + SUM(COALESCE(ms.count, 0)) AS quantity
                   FROM event e
                            LEFT JOIN market_buy_event mb ON mb.event_id = e.id
                            LEFT JOIN market_sell_event ms ON ms.event_id = e.id
                   WHERE e.cmdr IS NOT NULL
                       AND e.timestamp BETWEEN :start AND :end
                   GROUP BY e.cmdr
                   HAVING total_volume > 0
                   ORDER BY quantity DESC LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. {((r.cmdr[:17] + '...') if r.cmdr and len(r.cmdr) > 20 else (r.cmdr or '')):<20} | Vol: {r.total_volume or 0:>15,} Cr. - {r.quantity or 0:>9,} t"
                for i, r in enumerate(rows)
            )
        },
        "Missions Completed": {
            "sql": '''
                   SELECT e.cmdr, COUNT(*) AS missions_completed
                   FROM mission_completed_event mc
                            JOIN event e ON e.id = mc.event_id
                   WHERE e.cmdr IS NOT NULL
                       AND e.timestamp BETWEEN :start AND :end
                   GROUP BY e.cmdr
                   ORDER BY missions_completed DESC LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. {((r.cmdr[:17] + '...') if r.cmdr and len(r.cmdr) > 20 else (r.cmdr or '')):<20} | {r.missions_completed:>4}"
                for i, r in enumerate(rows)
            )
        },
        "Influence by Faction": {
            "sql": '''
                   SELECT e.cmdr, mci.faction_name, SUM(LENGTH(mci.influence)) AS influence
                   FROM mission_completed_influence mci
                            JOIN mission_completed_event mce ON mce.event_id = mci.mission_id
                            JOIN event e ON e.id = mce.event_id
                   WHERE e.cmdr IS NOT NULL
                       AND e.timestamp BETWEEN :start AND :end
                   GROUP BY e.cmdr, mci.faction_name
                   ORDER BY influence DESC, e.cmdr LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. "
                f"{((r.cmdr[:17] + '...') if r.cmdr and len(r.cmdr) > 20 else (r.cmdr or '')):<20} | "
                f"{((r.faction_name[:22] + '...') if r.faction_name and len(r.faction_name) > 25 else (r.faction_name or '')):<25} | "
                f"+{r.influence:>4}"
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
                       AND e.timestamp BETWEEN :start AND :end
                       AND mci.faction_name LIKE '%East India Company%'
                   GROUP BY e.cmdr, mci.faction_name
                   ORDER BY influence DESC, e.cmdr LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. {((r.cmdr[:17] + '...') if r.cmdr and len(r.cmdr) > 20 else (r.cmdr or '')):<20} | +{r.influence}"
                for i, r in enumerate(rows)
            )
        },
        "Bounty Vouchers": {
            "sql": '''
                   SELECT e.cmdr, SUM(rv.amount) AS bounty_vouchers
                   FROM redeem_voucher_event rv
                            JOIN event e ON e.id = rv.event_id
                   WHERE e.cmdr IS NOT NULL
                       AND e.timestamp BETWEEN :start AND :end
                       AND rv.type = 'bounty'
                   GROUP BY e.cmdr
                   ORDER BY bounty_vouchers DESC LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. {((r.cmdr[:17] + '...') if r.cmdr and len(r.cmdr) > 20 else (r.cmdr or '')):<20} | {r.bounty_vouchers or 0:>15,} Cr."
                for i, r in enumerate(rows)
            )
        },
        "Combat Bonds": {
            "sql": '''
                   SELECT e.cmdr, SUM(rv.amount) AS combat_bonds
                   FROM redeem_voucher_event rv
                            JOIN event e ON e.id = rv.event_id
                   WHERE e.cmdr IS NOT NULL
                       AND e.timestamp BETWEEN :start AND :end
                       AND rv.type = 'CombatBond'
                   GROUP BY e.cmdr
                   ORDER BY combat_bonds DESC LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. {((r.cmdr[:17] + '...') if r.cmdr and len(r.cmdr) > 20 else (r.cmdr or '')):<20} | {r.combat_bonds or 0:>15,} Cr."
                for i, r in enumerate(rows)
            )
        },
        "Exploration Sales": {
            "sql": '''
                   SELECT cmdr, SUM(total_sales) AS total_exploration_sales
                   FROM (SELECT e.cmdr, se.earnings AS total_sales
                         FROM sell_exploration_data_event se
                                  JOIN event e ON e.id = se.event_id
                         WHERE e.cmdr IS NOT NULL
                           AND e.timestamp BETWEEN :start AND :end
                         UNION ALL
                         SELECT e.cmdr, ms.total_earnings AS total_sales
                         FROM multi_sell_exploration_data_event ms
                                  JOIN event e ON e.id = ms.event_id
                         WHERE e.cmdr IS NOT NULL
                           AND e.timestamp BETWEEN :start AND :end)
                   GROUP BY cmdr
                   ORDER BY total_exploration_sales DESC LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. {((r.cmdr[:17] + '...') if r.cmdr and len(r.cmdr) > 20 else (r.cmdr or '')):<20} | {r.total_exploration_sales or 0:>15,} Cr."
                for i, r in enumerate(rows)
            )
        },
        "Bounty Fines": {
            "sql": '''
                   SELECT e.cmdr, SUM(cc.bounty) AS bounty_fines
                   FROM commit_crime_event cc
                            JOIN event e ON e.id = cc.event_id
                   WHERE e.cmdr IS NOT NULL
                       AND e.timestamp BETWEEN :start AND :end
                   GROUP BY e.cmdr
                   ORDER BY bounty_fines DESC LIMIT 5
                   ''',
            "format": lambda rows: "\n".join(
                f"{i + 1}. {((r.cmdr[:17] + '...') if r.cmdr and len(r.cmdr) > 20 else (r.cmdr or '')):<20} | {r.bounty_fines or 0:>15,} Cr."
                for i, r in enumerate(rows)
            )
        }
    }

    for tenant in tenants:
        engine = get_engine_for_tenant(tenant)
        if not engine:
            logging.warning(f"Kein DB-URI für Tenant {tenant.get('name')}, überspringe.")
            continue
        webhook_url = get_discord_webhook(tenant, "shoutout")
        if not webhook_url:
            logging.warning(f"Kein Discord-Webhook für Tenant {tenant.get('name')}, überspringe.")
            continue

        with engine.connect() as conn:
            sections = []
            for title, q in base_queries.items():
                try:
                    rows = conn.execute(text(q["sql"]), {"start": start_str, "end": end_str}).fetchall()
                except Exception as e:
                    logging.error(f"Query-Fehler für {tenant.get('name')} - {title}: {e}")
                    continue
                if not rows:
                    continue
                section = f"**📊 {title}**\n```text\n{q['format'](rows)}\n```"
                sections.append(section)

            if not sections:
                logging.info(f"No data found for Discord summary ({tenant.get('name')}).")
                continue

            full_message = f"📅 Daily Summary for {start.date()} (UTC) - {tenant.get('name')}\n\n" + "\n\n".join(sections)
            response = requests.post(webhook_url, json={"content": full_message})
            if response.status_code == 204:
                logging.info(f"Discord summary sent successfully for {tenant.get('name')}.")
            else:
                logging.error(f"Discord post failed for {tenant.get('name')}: {response.status_code} {response.text}")


def send_syntheticcz_summary_to_discord(app, db, period="all"):
    init_logger()
    tenants = get_tenants()
    from sqlalchemy import text
    import requests

    # Calculate period filter
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

    if start and end:
        date_filter = f"e.timestamp BETWEEN '{start.strftime('%Y-%m-%dT00:00:00Z')}' AND '{end.strftime('%Y-%m-%dT23:59:59Z')}'"
        period_label = f"{start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"
    else:
        date_filter = "1=1"
        period_label = "All Time"

    for tenant in tenants:
        engine = get_engine_for_tenant(tenant)
        if not engine:
            logging.warning(f"Kein DB-URI für Tenant {tenant.get('name')}, überspringe.")
            continue
        webhook_url = get_discord_webhook(tenant, "shoutout")
        if not webhook_url:
            logging.warning(f"Kein Discord-Webhook für Tenant {tenant.get('name')}, überspringe.")
            continue

        with engine.connect() as conn:
            # Query data
            sql = f"""
                SELECT
                    e.starsystem AS system,
                    scz.cz_type,
                    e.cmdr,
                    COUNT(*) AS cz_count
                FROM synthetic_cz scz
                JOIN event e ON e.id = scz.event_id
                WHERE {date_filter}
                GROUP BY e.starsystem, scz.cz_type, e.cmdr
                ORDER BY e.starsystem, scz.cz_type, cz_count DESC
            """
            rows = conn.execute(text(sql)).fetchall()
            # Structure: {system: {cz_type: total, cmdrs: {cmdr: {cz_type: count}}}}
            summary = {}
            for row in rows:
                system = row.system or "Unknown"
                cz_type = row.cz_type or "Unknown"
                cmdr = row.cmdr or "Unknown"
                cz_count = row.cz_count

                if system not in summary:
                    summary[system] = {"low": 0, "medium": 0, "high": 0, "cmdrs": {}}
                if cz_type in ["low", "medium", "high"]:
                    summary[system][cz_type] += cz_count
                else:
                    summary[system][cz_type] = summary[system].get(cz_type, 0) + cz_count

                if cmdr not in summary[system]["cmdrs"]:
                    summary[system]["cmdrs"][cmdr] = {"low": 0, "medium": 0, "high": 0}
                if cz_type in ["low", "medium", "high"]:
                    summary[system]["cmdrs"][cmdr][cz_type] += cz_count

            # Für jedes System eine eigene Discord-Nachricht
            for system, data in summary.items():
                total = data.get("low", 0) + data.get("medium", 0) + data.get("high", 0)
                lines = [f"⚔️:rocket:\n**{system} - Space CZ Summary ({period_label}) - {tenant.get('name')}**"]
                lines.append(f"\nTotal: {total} CZs")
                lines.append("```text")
                lines.append(f"{'Type':<8} | {'Count':>5}")
                lines.append(f"{'-'*9}+{'-'*6}")
                for typ in ["low", "medium", "high"]:
                    lines.append(f"{typ.capitalize():<8} | {data.get(typ,0):>5}")
                lines.append("```")
                # Cmdr distribution
                if data["cmdrs"]:
                    lines.append("Cmdr Distribution:")
                    lines.append("```text")
                    # Spaltenbreiten: Cmdr(17), Low(5), Medium(7), High(5), Total(6)
                    lines.append(f"{'Cmdr':<17} | {'Low':>5} | {'Medium':>7} | {'High':>5} | {'Total':>6}")
                    lines.append(f"{'-'*18}+{'-'*7}+{'-'*9}+{'-'*7}+{'-'*7}")
                    for cmdr, czs in sorted(data["cmdrs"].items(), key=lambda x: sum(x[1].values()), reverse=True):
                        total_cmdr = sum(czs.values())
                        lines.append(f"{cmdr:<17} | {czs['low']:>5} | {czs['medium']:>7} | {czs['high']:>5} | {total_cmdr:>6}")
                    lines.append("```")
                    lines.append("\n")
                msg = "\n".join(lines)
                response = requests.post(webhook_url, json={"content": msg})
                if response.status_code == 204:
                    logging.info(f"SyntheticCZ Discord summary sent for {system} ({tenant.get('name')}).")
                else:
                    logging.error(f"SyntheticCZ Discord post failed for {system} ({tenant.get('name')}): {response.status_code} {response.text}")


def send_syntheticgroundcz_summary_to_discord(app, db, period="all"):
    init_logger()
    tenants = get_tenants()
    from sqlalchemy import text
    import requests

    # Calculate period filter
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

    if start and end:
        date_filter = f"e.timestamp BETWEEN '{start.strftime('%Y-%m-%dT00:00:00Z')}' AND '{end.strftime('%Y-%m-%dT23:59:59Z')}'"
        period_label = f"{start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"
    else:
        date_filter = "1=1"
        period_label = "All Time"

    for tenant in tenants:
        engine = get_engine_for_tenant(tenant)
        if not engine:
            logging.warning(f"Kein DB-URI für Tenant {tenant.get('name')}, überspringe.")
            continue
        webhook_url = get_discord_webhook(tenant, "shoutout")
        if not webhook_url:
            logging.warning(f"Kein Discord-Webhook für Tenant {tenant.get('name')}, überspringe.")
            continue

        with engine.connect() as conn:
            # Query alle relevanten Daten
            sql = f"""
                SELECT
                    e.starsystem AS system,
                    sgcz.settlement,
                    sgcz.cz_type,
                    e.cmdr,
                    COUNT(*) AS cz_count
                FROM synthetic_ground_cz sgcz
                JOIN event e ON e.id = sgcz.event_id
                WHERE {date_filter}
                GROUP BY e.starsystem, sgcz.settlement, sgcz.cz_type, e.cmdr
                ORDER BY e.starsystem, sgcz.settlement, sgcz.cz_type, cz_count DESC
            """
            rows = conn.execute(text(sql)).fetchall()

            # Datenstruktur: {system: {"low": int, "medium": int, "high": int, "settlements": {settlement: int}, "cmdrs": {cmdr: {"low": int, "medium": int, "high": int}}}}
            summary = {}
            for row in rows:
                system = row.system or "Unknown"
                settlement = row.settlement or "Unknown"
                cz_type = row.cz_type or "Unknown"
                cmdr = row.cmdr or "Unknown"
                cz_count = row.cz_count

                if system not in summary:
                    summary[system] = {"low": 0, "medium": 0, "high": 0, "settlements": {}, "cmdrs": {}}
                # CZ-Type-Verteilung
                if cz_type in ["low", "medium", "high"]:
                    summary[system][cz_type] += cz_count
                else:
                    summary[system][cz_type] = summary[system].get(cz_type, 0) + cz_count
                # Settlement-Zählung (nur Gesamt)
                if settlement not in summary[system]["settlements"]:
                    summary[system]["settlements"][settlement] = 0
                summary[system]["settlements"][settlement] += cz_count
                # Cmdr-Verteilung
                if cmdr not in summary[system]["cmdrs"]:
                    summary[system]["cmdrs"][cmdr] = {"low": 0, "medium": 0, "high": 0}
                if cz_type in ["low", "medium", "high"]:
                    summary[system]["cmdrs"][cmdr][cz_type] += cz_count

            # Für jedes System eine Nachricht
            for system, data in summary.items():
                total = data.get("low", 0) + data.get("medium", 0) + data.get("high", 0)
                lines = [f"\n⚔️:gun:\n**{system} - Ground CZ Summary ({period_label}) - {tenant.get('name')}**"]
                lines.append(f"\nTotal Ground CZs: {total}")
                # Typ-Verteilung
                lines.append("```text")
                lines.append(f"{'Type':<8} | {'Count':>5}")
                lines.append(f"{'-'*9}+{'-'*6}")
                for typ in ["low", "medium", "high"]:
                    lines.append(f"{typ.capitalize():<8} | {data.get(typ,0):>5}")
                lines.append("```")
                # Settlement-Liste mit fester Breite und Kürzung
                if data["settlements"]:
                    lines.append("Settlements:")
                    lines.append("```text")
                    lines.append(f"{'Settlement':<35} | {'CZs':>5}")
                    lines.append(f"{'-'*36}+{'-'*6}")
                    for settlement, count in sorted(data["settlements"].items(), key=lambda x: x[1], reverse=True):
                        # Kürzen falls länger als 35 Zeichen
                        s_name = settlement
                        if len(s_name) > 35:
                            s_name = s_name[:32] + "..."
                        lines.append(f"{s_name:<35} | {count:>5}")
                    lines.append("```")
                # Cmdr-Verteilung
                if data["cmdrs"]:
                    lines.append("Cmdr Distribution:")
                    lines.append("```text")
                    lines.append(f"{'Cmdr':<17} | {'Low':>5} | {'Medium':>7} | {'High':>5} | {'Total':>6}")
                    lines.append(f"{'-'*18}+{'-'*7}+{'-'*9}+{'-'*7}+{'-'*7}")
                    for cmdr, czs in sorted(data["cmdrs"].items(), key=lambda x: sum(x[1].values()), reverse=True):
                        total_cmdr = sum(czs.values())
                        lines.append(f"{cmdr:<17} | {czs['low']:>5} | {czs['medium']:>7} | {czs['high']:>5} | {total_cmdr:>6}")
                    lines.append("```")
                msg = "\n".join(lines)
                response = requests.post(webhook_url, json={"content": msg})
                if response.status_code == 204:
                    logging.info(f"SyntheticGroundCZ Discord summary sent for {system} ({tenant.get('name')}).")
                else:
                    logging.error(f"SyntheticGroundCZ Discord post failed for {system} ({tenant.get('name')}): {response.status_code} {response.text}")


def start_scheduler(app, db):
    global _scheduler_instance
    if _scheduler_instance is not None:
        logging.info("Scheduler already started, skipping duplicate.")
        return

    init_logger()
    scheduler = BackgroundScheduler(timezone="UTC")
    # Täglicher Discord-Summary-Job
    scheduler.add_job(
        lambda: format_discord_summary(app, db),
        CronTrigger(hour=0, minute=0, timezone="UTC")
    )
    # SyntheticCZ-Summary direkt nach format_discord_summary
    scheduler.add_job(
        lambda: send_syntheticcz_summary_to_discord(app, db, "ld"),
        CronTrigger(hour=0, minute=1, timezone="UTC")
    )
    # SyntheticGroundCZ-Summary direkt nach SyntheticCZ
    scheduler.add_job(
        lambda: send_syntheticgroundcz_summary_to_discord(app, db, "ld"),
        CronTrigger(hour=0, minute=2, timezone="UTC")
    )
    scheduler.start()
    logging.info("[SchedulerShoutout] Shoutout scheduled with daily interval at 0:00 UTC.")
    atexit.register(lambda: scheduler.shutdown())
