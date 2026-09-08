"""Compact BGS embeds. Alarm facts are historical; EDDN fields are current."""
from __future__ import annotations

import ast
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import create_engine, text


def obj(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def key(value):
    return unicodedata.normalize("NFC", str(value or "").strip()).lower()


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def safe(value, limit=500):
    # Escape user/data markdown and suppress mention-looking names in embeds.
    value = re.sub(r"([\\`*_~|\[\]<>])", r"\\\1", str(value or "—"))
    return value.replace("@", "@\u200b")[:limit]


def label(value):
    return re.sub(r"^\$[^_]+_(.*);$", r"\1", str(value or "—")).replace("_", " ")


def time_label(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    except (ValueError, TypeError):
        return str(value or "—")


def context(alert):
    f = obj(alert.get("facts_json", alert.get("facts")))
    principal = f.get("tenant_faction") or f.get("monitored_faction") or f.get("controlling_faction")
    event = alert.get("event_key")
    entered = [x for x in f.get("entered_factions", []) if isinstance(x, dict)] if isinstance(f.get("entered_factions", []), list) else []
    conflicts = [x for x in f.get("new_conflicts", []) if isinstance(x, dict)] if isinstance(f.get("new_conflicts", []), list) else []
    gap = next((x for x in entered if event and event in (x.get("key"), "gap:" + key(x.get("faction")))), entered[0] if len(entered) == 1 else {})
    conflict = next((x for x in conflicts if event and event in (x.get("key"), "conflict:" + key(x.get("type")) + ":" + ":".join(sorted([key(x.get("faction1")), key(x.get("faction2"))])))), conflicts[0] if len(conflicts) == 1 else {})
    names, summary, threshold = [], "Alarm details", ""
    limit = number(f.get("threshold_pp"))
    change = obj(f.get("strongest_change"))
    if principal and gap.get("faction") and number(gap.get("gap_pp")) is not None:
        names, summary = [principal, gap["faction"]], f"Gap {gap['gap_pp']:.2f} pp"
    elif principal and f.get("competitor") and number(f.get("gap_pp")) is not None:
        names, summary = [principal, f["competitor"]], f"Gap {f['gap_pp']:.2f} pp"
    elif conflict.get("faction1") and conflict.get("faction2"):
        names, summary = [conflict["faction1"], conflict["faction2"]], "New conflict: " + str(conflict.get("type") or conflict.get("war_type") or "Conflict")
    elif principal and number(f.get("loss_pp")) is not None:
        names, summary = [principal], f"Loss {f['loss_pp']:.2f} pp"
    elif change.get("faction") and number(change.get("delta_pp")) is not None:
        delta = change["delta_pp"]
        names, summary = [change["faction"]], f"{'Gain' if delta >= 0 else 'Loss'} {abs(delta):.2f} pp"
    elif not entered and not conflicts and event in (None, "", "condition", "below") and principal and limit is not None:
        influence = number(f.get("tenant_influence_pp"))
        if influence is None:
            influence = number(f.get("controller_influence_pp"))
        if influence is not None and influence < limit:
            names, summary, threshold = [principal], f"Influence {influence:.2f}%", f"Below {limit:.2f}%"
    if names and limit is not None and not threshold:
        threshold = f"Threshold {limit:.2f} pp"
    return list({key(n): str(n) for n in names}.values())[:2], summary, threshold


def current_system(system):
    """Indexed, bounded read; a missing/locked data source must not block alerts."""
    uri = os.getenv("EDDN_DATABASE", "").strip()
    if not uri:
        return {}, []
    engine = create_engine(uri, connect_args={"timeout": 2} if uri.startswith("sqlite") else {})
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM eddn_system_info WHERE system_name=:system ORDER BY updated_at DESC LIMIT 1"), {"system": system}).mappings().first()
            factions = [dict(r) for r in conn.execute(text("SELECT * FROM eddn_faction WHERE system_name=:system ORDER BY updated_at DESC"), {"system": system}).mappings()]
            info = dict(row) if row else {}
            # The raw journal contains government/allegiance absent from eddn_faction.
            if info.get("eddn_message_id"):
                raw = conn.execute(text("SELECT message_json FROM eddn_message WHERE id=:id"), {"id": info["eddn_message_id"]}).scalar()
                payload = obj(raw)
                if not payload and isinstance(raw, str):
                    try:
                        payload = obj(ast.literal_eval(raw))
                    except (ValueError, SyntaxError):
                        pass
                journal = obj(payload.get("message", payload))
                info["government"] = journal.get("SystemGovernment_Localised") or info.get("government")
                info["economy"] = journal.get("SystemEconomy_Localised") or journal.get("SystemEconomy")
                extra = {key(f.get("Name")): f for f in journal.get("Factions", []) if isinstance(f, dict)}
                for faction in factions:
                    metadata = extra.get(key(faction.get("name")), {})
                    faction["government"] = metadata.get("Government_Localised") or metadata.get("Government")
                    faction["allegiance"] = metadata.get("Allegiance")
            return info, factions
    finally:
        engine.dispose()


def states(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return ", ".join(str(x.get("State", x.get("state", ""))) if isinstance(x, dict) else str(x) for x in value) or "None"
    return "None"


def notification(alert, info=None, factions=None):
    info, factions = info or {}, factions or []
    names, reason, threshold = context(alert)
    system = str(alert.get("system_name") or "Unknown system")
    encoded = quote(system, safe="")
    base = os.getenv("BGS_DASHBOARD_URL", "https://valk-elite.de").rstrip("/")
    dashboard = base + "/intelligence/alerts"
    severity = str(alert.get("severity") or "warning").lower()
    status = "Resolved" if alert.get("resolved_at") else "Active"
    fields = []
    if info:
        population = info.get("population")
        population = f"{population:,}" if isinstance(population, int) else population
        fields.append({"name": "System · current data", "value": safe(" · ".join(label(info.get(k)) for k in ("controlling_faction", "allegiance", "government", "economy")), 500) + "\nPopulation: " + safe(population, 50) + " · Updated: " + safe(time_label(info.get("updated_at")), 60)})
    by_name = {}
    for f in factions:
        by_name.setdefault(key(f.get("name")), f)
    for name in names:
        f = by_name.get(key(name))
        value = "Current faction data unavailable"
        if f:
            influence = number(f.get("influence"))
            if influence is not None and abs(influence) <= 1:
                influence *= 100
            value = "**" + (f"{influence:.2f}%" if influence is not None else "—") + " influence**\n"
            value += safe(label(f.get("government")), 80) + " · " + safe(label(f.get("allegiance")), 80)
            value += "\nActive: " + safe(label(states(f.get("active_states") or f.get("state"))), 160)
            value += "\nPending: " + safe(label(states(f.get("pending_states"))), 160)
            value += "\nUpdated: " + safe(time_label(f.get("updated_at")), 60)
        fields.append({"name": "⚠ " + safe(name, 240), "value": value, "inline": True})
    links = [("RC", "https://ravencolonial.com/#sys=" + encoded), ("Inara", "https://inara.cz/elite/starsystem/?search=" + encoded), ("Spansh", base + "/api/system-watchlist/spansh?system=" + encoded), ("EDGIS", "https://elitedangereuse.fr/outils/sysmap.php?system=" + encoded)]
    # Keep complete URLs rather than truncating links for extreme system names.
    for start in (0, 2):
        value = " · ".join(f"[{label}]({url})" for label, url in links[start:start + 2] if len(url) < 450)
        if value:
            fields.append({"name": "System links" if start == 0 else "More links", "value": value})
    fields.append({"name": "Settled tick · alarm values", "value": safe(time_label(alert.get("fired_ticktime")), 80)})
    scope = str(alert.get("owner_scope") or "").upper()
    badges = " · ".join(filter(None, [safe(severity.upper(), 20), safe(scope, 20) if scope else "", status.upper()]))
    description = f"**{badges}**\n\n**⚠ {safe(reason, 160)}**"
    if threshold:
        description += "\n**" + safe(threshold) + " · at trigger**"
    description += "\n" + safe(alert.get("title"), 240) + "\n" + safe(alert.get("message"), 900)
    embed = {"title": safe(system, 256), "url": dashboard, "color": {"critical": 0xE65C72, "info": 0x67B9D3}.get(severity, 0xE3BD59), "description": description, "fields": fields, "footer": {"text": "VALK · BGS Alert Center · faction values at send time"}}
    try:
        embed["timestamp"] = datetime.fromisoformat(str(alert.get("fired_at")).replace("Z", "+00:00")).isoformat()
    except (ValueError, TypeError):
        pass
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}
