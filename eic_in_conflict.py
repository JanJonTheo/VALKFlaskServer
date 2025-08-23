from flask import request, jsonify
from sqlalchemy import text
import json, ast, requests
from datetime import datetime
from fdev_tick_monitor import last_tick
import os
from dotenv import load_dotenv

load_dotenv()

# Discord webhook URL for sending to Bullis' Discord channel
#DISCORD_SHOUTOUT_WEBHOOK = os.getenv("DISCORD_BULLIS_WEBHOOK_PROD")

# Discord webhook URL for sending to EICs' BGS Discord channel
DISCORD_CONFLICT_WEBHOOK = os.getenv("DISCORD_BGS_WEBHOOK_PROD")

def register_eic_conflict_routes(app, db, require_api_key):

    def extract_eic_conflicts(tickid, db):
        results = db.session.execute(text(
            "SELECT raw_json FROM event WHERE tickid = :tick"
        ), {"tick": tickid}).fetchall()

        systems = {}

        for row in results:
            try:
                data = json.loads(row.raw_json)
            except Exception:
                try:
                    data = ast.literal_eval(row.raw_json)
                except Exception:
                    continue

            conflicts = data.get("Conflicts", [])
            if not conflicts:
                continue

            eic_conflict = next((
                c for c in conflicts
                if "East India Company" in (c.get("Faction1", {}).get("Name", "") + c.get("Faction2", {}).get("Name", ""))
            ), None)
            if not eic_conflict:
                continue

            system = data.get("StarSystem")
            if not system:
                continue

            ts = data.get("timestamp")
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            cmdr = data.get("cmdr")
            etype = data.get("event")
            ticktime = data.get("ticktime")

            if system not in systems or dt > systems[system]["last_jump"]:
                f1 = eic_conflict.get("Faction1", {})
                f2 = eic_conflict.get("Faction2", {})
                systems[system] = {
                    "system": system,
                    "last_jump": dt,
                    "event_type": etype,
                    "tickid": tickid,
                    "ticktime": ticktime,
                    "galaxy_tick": last_tick,
                    "war_type": eic_conflict.get("WarType"),
                    "faction1": {
                        "name": f1.get("Name"),
                        "stake": f1.get("Stake"),
                        "won_days": f1.get("WonDays")
                    },
                    "faction2": {
                        "name": f2.get("Name"),
                        "stake": f2.get("Stake"),
                        "won_days": f2.get("WonDays")
                    },
                    "cmdrs": set()
                }

            if cmdr:
                systems[system]["cmdrs"].add(cmdr)

        return systems


    @app.route("/api/eic-in-conflict-current-tick", methods=["GET"])
    @require_api_key
    def get_eic_conflicts():
        tickids = db.session.execute(text(
            "SELECT DISTINCT tickid FROM event ORDER BY timestamp DESC LIMIT 2"
        )).fetchall()

        if not tickids or len(tickids) < 2:
            return jsonify({"error": "Not enough tick data found"}), 404

        tick_current = tickids[0][0]
        tick_previous = tickids[1][0]

        data = {
            "current_tick": [],
            "previous_tick": []
        }

        for label, tickid in [("current_tick", tick_current), ("previous_tick", tick_previous)]:
            systems = extract_eic_conflicts(tickid, db)
            for s in systems.values():
                s["last_jump"] = s["last_jump"].isoformat()
                s["cmdrs"] = sorted(s["cmdrs"])
                data[label].append(s)
            data[label].sort(key=lambda x: x["last_jump"], reverse=True)
            data["galaxy_tick"] = last_tick

        return jsonify(data)


    @app.route("/api/discord/eic-in-conflict-current-tick", methods=["POST"])
    @require_api_key
    def send_eic_conflicts_to_discord():
        tickids = db.session.execute(text(
            "SELECT DISTINCT tickid FROM event ORDER BY timestamp DESC LIMIT 2"
        )).fetchall()

        if not tickids or len(tickids) < 2:
            return jsonify({"error": "Not enough tick data found"}), 404

        tick_current = tickids[0][0]
        tick_previous = tickids[1][0]

        # Extract EIC conflicts for current and previous ticks
        #sections = [
        #    ("Current Tick", extract_eic_conflicts(tick_current, db)),
        #    ("Previous Tick", extract_eic_conflicts(tick_previous, db))
        #]

        # For now, only current tick
        sections = [
             ("Current Tick", extract_eic_conflicts(tick_current, db))
        ]

        message_lines = ["__**🛡️ Detected EIC Conflicts**__", ""]

        for label, systems in sections:
            if not systems:
                continue

            #message_lines.append(f"__**{label}**__")
            #message_lines.append(f"Tick ID: {tick_current if label == 'Current Tick' else tick_previous}\n")
            for entry in sorted(systems.values(), key=lambda x: x["last_jump"], reverse=True):
                message_lines.append(f"**{entry['system']} ({entry['war_type']})**")
                message_lines.append("```")
                message_lines.append(f"Faction 1: {entry['faction1']['name']}")
                message_lines.append(f"  Stake: {entry['faction1']['stake']}")
                message_lines.append(f"  Won Days: {entry['faction1']['won_days']}")
                message_lines.append(f"Faction 2: {entry['faction2']['name']}")
                message_lines.append(f"  Stake: {entry['faction2']['stake']}")
                message_lines.append(f"  Won Days: {entry['faction2']['won_days']}")
                message_lines.append("```")
                message_lines.append(f":abacus: **{entry['faction1']['won_days']} vs {entry['faction2']['won_days']}**")
                message_lines.append(f"📌 Cmdrs: {', '.join(sorted(entry['cmdrs']))}")
                message_lines.append(f":timer: Detected: {entry['last_jump']}")
                message_lines.append("")

        if len(message_lines) <= 2:
            #return jsonify({"status": "No EIC conflicts in current or previous tick"}), 200
            status_message = "No EIC conflicts in current tick (Galaxy Tick: {})".format(last_tick)
            return jsonify({"status": status_message}), 200

        payload = {
            "content": "\n".join(message_lines)
        }

        response = requests.post(DISCORD_CONFLICT_WEBHOOK, json=payload)
        if response.status_code != 204:
            return jsonify({"error": f"Discord responded with {response.status_code}"}), 500

        return jsonify({"status": "Sent to Discord"}), 200
