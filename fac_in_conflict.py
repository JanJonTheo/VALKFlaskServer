from flask import request, jsonify
from sqlalchemy import text
import json, ast, requests
from datetime import datetime
from fdev_tick_monitor import last_tick
import os
from dotenv import load_dotenv

# Tenant-Konfiguration laden
TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    TENANTS = json.load(f)

load_dotenv()


def get_tenant_by_api_key(api_key):
    for tenant in TENANTS:
        if tenant["api_key"] == api_key:
            return tenant
    return None


def register_fac_conflict_routes(app, db, require_api_key):

    def extract_tenant_conflicts(tickid, db, faction_name):
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

            # Suche Konflikt für die Fraktion des Tenants
            tenant_conflict = next((
                c for c in conflicts
                if faction_name in (c.get("Faction1", {}).get("Name", "") + c.get("Faction2", {}).get("Name", ""))
            ), None)
            if not tenant_conflict:
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
                f1 = tenant_conflict.get("Faction1", {})
                f2 = tenant_conflict.get("Faction2", {})
                systems[system] = {
                    "system": system,
                    "last_jump": dt,
                    "event_type": etype,
                    "tickid": tickid,
                    "ticktime": ticktime,
                    "galaxy_tick": last_tick,
                    "war_type": tenant_conflict.get("WarType"),
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


    @app.route("/api/fac-in-conflict-current-tick", methods=["GET"])
    @require_api_key
    def get_fac_conflicts():
        # Tenant anhand API-Key bestimmen
        api_key = request.headers.get("apikey")
        tenant = get_tenant_by_api_key(api_key)
        if not tenant:
            return jsonify({"error": "Invalid API key"}), 403
        faction_name = tenant["faction_name"]

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
            systems = extract_tenant_conflicts(tickid, db, faction_name)
            for s in systems.values():
                s["last_jump"] = s["last_jump"].isoformat()
                s["cmdrs"] = sorted(s["cmdrs"])
                data[label].append(s)
            data[label].sort(key=lambda x: x["last_jump"], reverse=True)
            data["galaxy_tick"] = last_tick

        return jsonify(data)


    @app.route("/api/discord/fac-in-conflict-current-tick", methods=["POST"])
    @require_api_key
    def send_fac_conflicts_to_discord():
        # Tenant anhand API-Key bestimmen
        api_key = request.headers.get("apikey")
        tenant = get_tenant_by_api_key(api_key)
        if not tenant:
            return jsonify({"error": "Invalid API key"}), 403
        faction_name = tenant["faction_name"]
        discord_webhook = tenant["discord_webhooks"].get("bgs")

        tickids = db.session.execute(text(
            "SELECT DISTINCT tickid FROM event ORDER BY timestamp DESC LIMIT 2"
        )).fetchall()

        if not tickids or len(tickids) < 2:
            return jsonify({"error": "Not enough tick data found"}), 404

        tick_current = tickids[0][0]
        tick_previous = tickids[1][0]

        sections = [
             ("Current Tick", extract_tenant_conflicts(tick_current, db, faction_name))
        ]

        message_lines = [f"__**🛡️ Detected {faction_name} Conflicts**__", ""]

        for label, systems in sections:
            if not systems:
                continue

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
            status_message = f"No {faction_name} conflicts in current tick (Galaxy Tick: {last_tick})"
            return jsonify({"status": status_message}), 200

        payload = {
            "content": "\n".join(message_lines)
        }

        response = requests.post(discord_webhook, json=payload)
        if response.status_code != 204:
            return jsonify({"error": f"Discord responded with {response.status_code}"}), 500

        return jsonify({"status": "Sent to Discord"}), 200
