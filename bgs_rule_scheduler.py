"""Idempotent settled-snapshot evaluator and Discord outbox worker."""

from __future__ import annotations

import atexit
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from threading import Lock
from typing import Any
import uuid

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dateutil import parser as date_parser
import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from bgs_rules import (
    DELTA_RULE_TYPES,
    TENANT_RULE_TYPES,
    _tenant_faction_aliases,
    decrypt_webhook,
    utc_now,
    validate_discord_webhook,
    watchlist_systems,
)
from dashboard_users import ensure_dashboard_schema
from bgs_discord import current_system, notification


logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None
_run_lock = Lock()


def _engine(uri: str):
    url = make_url(uri)
    connect_args = (
        {"check_same_thread": False, "timeout": 30}
        if url.drivername.startswith("sqlite")
        else {}
    )
    return create_engine(uri, connect_args=connect_args)


def _parse_ticktime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = date_parser.isoparse(raw)
    except (TypeError, ValueError):
        parsed = None
        for pattern in ("%y-%m-%dT%H:%M:%S.%fZ", "%y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _percentage_points(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if abs(number) <= 1:
        number *= 100
    return round(number, 4)


def _snapshot_values(snapshot: dict[str, Any]) -> tuple[str, dict[str, float]]:
    controlling = snapshot.get("SystemFaction")
    controlling_name = ""
    if isinstance(controlling, dict):
        controlling_name = str(controlling.get("Name") or "").strip()
    factions: dict[str, float] = {}
    for faction in snapshot.get("Factions", []) or []:
        if not isinstance(faction, dict):
            continue
        name = str(faction.get("Name") or "").strip()
        influence = _percentage_points(faction.get("Influence"))
        if name and influence is not None:
            factions[name] = influence
    return controlling_name, factions


def _condition(rule: dict[str, Any]) -> dict[str, Any]:
    configured = _payload(rule.get("condition_json"))
    if configured.get("type"):
        return configured
    result = {
        "type": rule["condition_type"],
        "threshold_pp": float(rule["threshold_pp"]),
    }
    if rule["condition_type"] in DELTA_RULE_TYPES:
        result["window_days"] = int(rule.get("window_days") or 1)
    return result


def _tenant_faction(
    factions: dict[str, float], configured_name: str
) -> tuple[str, float] | None:
    aliases = {name.casefold() for name in _tenant_faction_aliases(configured_name)}
    for name, influence in factions.items():
        if name.casefold() in aliases:
            return name, influence
    return None


def _token(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .strip("$;")
        .replace(" ", "_")
        .casefold()
    )


def _snapshot_conflicts(
    snapshot: dict[str, Any], configured_name: str, allowed_types: set[str]
) -> dict[str, dict[str, Any]]:
    aliases = {name.casefold() for name in _tenant_faction_aliases(configured_name)}
    result: dict[str, dict[str, Any]] = {}
    for conflict in snapshot.get("Conflicts", []) or []:
        if not isinstance(conflict, dict):
            continue
        faction1 = conflict.get("Faction1")
        faction2 = conflict.get("Faction2")
        faction1 = (
            str(faction1.get("Name") or "").strip()
            if isinstance(faction1, dict)
            else str(conflict.get("faction1") or faction1 or "").strip()
        )
        faction2 = (
            str(faction2.get("Name") or "").strip()
            if isinstance(faction2, dict)
            else str(conflict.get("faction2") or faction2 or "").strip()
        )
        conflict_type = _token(conflict.get("WarType") or conflict.get("war_type") or conflict.get("type"))
        if conflict_type not in allowed_types or not faction1 or not faction2:
            continue
        if faction1.casefold() not in aliases and faction2.casefold() not in aliases:
            continue
        pair = sorted((faction1.casefold(), faction2.casefold()))
        key = f"conflict:{conflict_type}:{pair[0]}:{pair[1]}"
        result[key] = {
            "faction1": faction1,
            "faction2": faction2,
            "type": conflict_type,
            "status": _token(conflict.get("Status") or conflict.get("status")),
        }
    return result


def _baseline(
    snapshots: list[dict[str, Any]], latest_time: datetime, days: int
) -> dict[str, Any] | None:
    target = latest_time - timedelta(days=days)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for snapshot in snapshots[1:]:
        parsed = _parse_ticktime(snapshot.get("ticktime"))
        if parsed is not None and parsed <= target:
            candidates.append((parsed, snapshot))
    if not candidates:
        return None
    _, selected = max(candidates, key=lambda item: item[0])
    return selected


def evaluate_rule(
    rule: dict[str, Any],
    snapshots: list[dict[str, Any]],
    tenant_faction: str = "",
) -> dict[str, Any]:
    """Evaluate one rule. ``active=None`` means the available data is insufficient."""

    if not snapshots:
        return {"status": "insufficient_data", "active": None, "reason": "No settled snapshot"}
    ordered = sorted(
        snapshots,
        key=lambda item: _parse_ticktime(item.get("ticktime")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    latest = ordered[0]
    latest_time = _parse_ticktime(latest.get("ticktime"))
    current = _payload(latest.get("payload_json", latest.get("payload")))
    controller, factions = _snapshot_values(current)
    controller_influence = factions.get(controller)
    condition_config = _condition(rule)
    condition = condition_config["type"]
    if condition in TENANT_RULE_TYPES:
        # Compare observations of this system, even when several ticks were missed.
        if len(ordered) >= 2:
            previous = ordered[1]
            previous_payload = _payload(
                previous.get("payload_json", previous.get("payload"))
            )
            _, previous_factions = _snapshot_values(previous_payload)
        else:
            return {
                "status": "insufficient_data",
                "active": None,
                "reason": "Two settled system snapshots are required",
                "facts": {"ticktime": latest.get("ticktime")},
            }
        current_tenant = _tenant_faction(factions, tenant_faction)
        previous_tenant = _tenant_faction(previous_factions, tenant_faction)
        facts: dict[str, Any] = {
            "ticktime": latest.get("ticktime"),
            "baseline_ticktime": previous.get("ticktime"),
            "tenant_faction": tenant_faction,
            "monitored_faction": tenant_faction,
        }
        if not tenant_faction or current_tenant is None or previous_tenant is None:
            return {
                "status": "insufficient_data",
                "active": None,
                "reason": "The monitored faction is not comparable across the available snapshots",
                "facts": facts,
            }
        current_name, current_influence = current_tenant
        previous_name, previous_influence = previous_tenant
        facts.update(
            {
                "tenant_faction": current_name,
                "tenant_influence_pp": current_influence,
                "baseline_influence_pp": previous_influence,
            }
        )
        if condition == "tenant_faction_loss":
            threshold = float(condition_config["threshold_pp"])
            loss = round(previous_influence - current_influence, 4)
            active = loss >= threshold
            event_key = f"loss:{latest.get('ticktime')}"
            event_keys = [event_key] if active else []
            return {
                "status": "ok",
                "active": active,
                "facts": {
                    **facts,
                    "threshold_pp": threshold,
                    "loss_pp": loss,
                    "event_keys": event_keys,
                },
                "events": event_keys,
                "active_event_keys": event_keys,
            }
        if condition == "tenant_faction_below":
            threshold = float(condition_config["threshold_pp"])
            active = current_influence < threshold
            crossed = previous_influence >= threshold and active
            event_keys = ["below"] if crossed else []
            return {
                "status": "ok",
                "active": active,
                "facts": {
                    **facts,
                    "threshold_pp": threshold,
                    "event_keys": event_keys,
                },
                "events": event_keys,
                "active_event_keys": ["below"] if active else [],
            }
        if condition == "tenant_faction_gap":
            threshold = float(condition_config["threshold_pp"])
            current_by_key = {name.casefold(): (name, value) for name, value in factions.items()}
            previous_by_key = {
                name.casefold(): (name, value) for name, value in previous_factions.items()
            }
            tenant_aliases = {
                value.casefold() for value in _tenant_faction_aliases(tenant_faction)
            }
            active_items = []
            entered = []
            for key in sorted(set(current_by_key) & set(previous_by_key)):
                if key in tenant_aliases:
                    continue
                name, current_value = current_by_key[key]
                _, previous_value = previous_by_key[key]
                current_gap = round(abs(current_influence - current_value), 4)
                previous_gap = round(abs(previous_influence - previous_value), 4)
                if current_gap <= threshold:
                    item = {
                        "key": f"gap:{key}",
                        "faction": name,
                        "gap_pp": current_gap,
                        "previous_gap_pp": previous_gap,
                    }
                    active_items.append(item)
                    if previous_gap > threshold:
                        entered.append(item)
            event_keys = [item["key"] for item in entered]
            return {
                "status": "ok",
                "active": bool(active_items),
                "facts": {
                    **facts,
                    "threshold_pp": threshold,
                    "entered_factions": entered,
                    "active_factions": active_items,
                    "event_keys": event_keys,
                },
                "events": event_keys,
                "active_event_keys": [item["key"] for item in active_items],
            }
        allowed_types = {
            _token(value)
            for value in condition_config.get("conflict_types", ["election", "war"])
        }
        current_conflicts = _snapshot_conflicts(current, tenant_faction, allowed_types)
        previous_conflicts = _snapshot_conflicts(
            previous_payload, tenant_faction, allowed_types
        )
        event_keys = sorted(set(current_conflicts) - set(previous_conflicts))
        return {
            "status": "ok",
            "active": bool(current_conflicts),
            "facts": {
                **facts,
                "conflict_types": sorted(allowed_types),
                "new_conflicts": [current_conflicts[key] for key in event_keys],
                "active_conflicts": list(current_conflicts.values()),
                "event_keys": event_keys,
            },
            "events": event_keys,
            "active_event_keys": sorted(current_conflicts),
        }
    base_facts: dict[str, Any] = {
        "ticktime": latest.get("ticktime"),
        "controlling_faction": controller,
        "controller_influence_pp": controller_influence,
        "threshold_pp": float(rule["threshold_pp"]),
    }
    if latest_time is None or not controller or controller_influence is None:
        return {
            "status": "insufficient_data",
            "active": None,
            "reason": "The settled snapshot has no comparable controlling faction",
            "facts": base_facts,
        }

    condition = rule["condition_type"]
    threshold = float(rule["threshold_pp"])
    if condition == "controller_below":
        active = controller_influence < threshold
        return {"status": "ok", "active": active, "facts": base_facts}
    if condition == "controller_gap":
        competitors = [(name, value) for name, value in factions.items() if name != controller]
        if not competitors:
            return {
                "status": "insufficient_data",
                "active": None,
                "reason": "No competing faction is available",
                "facts": base_facts,
            }
        rival, rival_influence = max(competitors, key=lambda item: item[1])
        gap = round(controller_influence - rival_influence, 4)
        facts = {
            **base_facts,
            "competitor": rival,
            "competitor_influence_pp": rival_influence,
            "gap_pp": gap,
        }
        return {"status": "ok", "active": gap <= threshold, "facts": facts}

    baseline = _baseline(ordered, latest_time, int(rule.get("window_days") or 1))
    if baseline is None:
        return {
            "status": "insufficient_data",
            "active": None,
            "reason": "No settled baseline at or before the requested window is available",
            "facts": base_facts,
        }
    baseline_payload = _payload(baseline.get("payload_json", baseline.get("payload")))
    _, baseline_factions = _snapshot_values(baseline_payload)
    base_facts.update(
        {
            "baseline_ticktime": baseline.get("ticktime"),
            "window_days": int(rule.get("window_days") or 1),
            "comparison_days": round((latest_time - _parse_ticktime(baseline.get("ticktime"))).total_seconds() / 86400, 2),
        }
    )
    if condition == "controller_loss":
        before = baseline_factions.get(controller)
        if before is None:
            return {
                "status": "insufficient_data",
                "active": None,
                "reason": "The current controller is absent from the baseline",
                "facts": base_facts,
            }
        loss = round(before - controller_influence, 4)
        return {
            "status": "ok",
            "active": loss >= threshold,
            "facts": {**base_facts, "baseline_influence_pp": before, "loss_pp": loss},
        }

    changes = []
    for name, current_value in factions.items():
        if name == controller or name not in baseline_factions:
            continue
        delta = round(current_value - baseline_factions[name], 4)
        changes.append(
            {
                "faction": name,
                "baseline_influence_pp": baseline_factions[name],
                "current_influence_pp": current_value,
                "delta_pp": delta,
            }
        )
    if not changes:
        return {
            "status": "insufficient_data",
            "active": None,
            "reason": "No non-controlling faction is comparable across both snapshots",
            "facts": base_facts,
        }
    if condition == "competitor_gain":
        matching = [item for item in changes if item["delta_pp"] >= threshold]
        strongest = max(changes, key=lambda item: item["delta_pp"])
    else:
        matching = [item for item in changes if -item["delta_pp"] >= threshold]
        strongest = min(changes, key=lambda item: item["delta_pp"])
    return {
        "status": "ok",
        "active": bool(matching),
        "facts": {**base_facts, "strongest_change": strongest, "matching_factions": matching},
    }


def _alert_copy(rule: dict[str, Any], system: str, facts: dict[str, Any]) -> tuple[str, str]:
    condition = rule["condition_type"]
    threshold = float(rule["threshold_pp"])
    if condition == "tenant_faction_loss":
        message = (
            f"{facts.get('tenant_faction')} lost {facts.get('loss_pp'):.2f} percentage "
            f"points since the previous available settled system snapshot."
        )
    elif condition == "tenant_faction_below":
        message = (
            f"{facts.get('tenant_faction')} fell to "
            f"{facts.get('tenant_influence_pp'):.2f}% (below {threshold:.2f}%)."
        )
    elif condition == "tenant_faction_gap":
        factions = facts.get("entered_factions") or []
        details = ", ".join(
            f"{item.get('faction')} ({float(item.get('gap_pp') or 0):.2f} pp)"
            for item in factions
        )
        message = (
            f"Faction influence moved within an absolute {threshold:.2f} percentage-point "
            f"gap of {facts.get('tenant_faction')}: {details}."
        )
    elif condition == "tenant_faction_new_conflict":
        conflicts = facts.get("new_conflicts") or []
        details = ", ".join(
            f"{item.get('faction1')} vs {item.get('faction2')} ({str(item.get('type') or '').title()})"
            for item in conflicts
        )
        message = f"{facts.get('tenant_faction')} entered a new conflict: {details}."
    elif condition == "controller_below":
        message = f"{facts.get('controlling_faction')} is at {facts.get('controller_influence_pp'):.2f}% (below {threshold:.2f}%)."
    elif condition == "controller_gap":
        message = f"{facts.get('competitor')} is only {facts.get('gap_pp'):.2f} percentage points behind {facts.get('controlling_faction')}."
    elif condition == "controller_loss":
        message = f"{facts.get('controlling_faction')} lost {facts.get('loss_pp'):.2f} percentage points over {facts.get('comparison_days', facts.get('window_days'))} day(s)."
    else:
        strongest = facts.get("strongest_change") or {}
        verb = "gained" if condition == "competitor_gain" else "lost"
        amount = abs(float(strongest.get("delta_pp") or 0))
        message = f"{strongest.get('faction', 'A competing faction')} {verb} {amount:.2f} percentage points over {facts.get('comparison_days', facts.get('window_days'))} day(s)."
    return f"{rule['name']} · {system}", message


def _event_facts(
    condition: str, facts: dict[str, Any], event_key: str
) -> dict[str, Any]:
    """Narrow multi-event evaluation facts to one independently stored alert."""

    selected = {**facts, "event_keys": [event_key]}
    all_event_keys = list(facts.get("event_keys") or [])
    try:
        event_index = all_event_keys.index(event_key)
    except ValueError:
        event_index = -1
    if condition == "tenant_faction_gap":
        entered = list(facts.get("entered_factions") or [])
        selected["entered_factions"] = (
            [entered[event_index]] if 0 <= event_index < len(entered) else []
        )
    elif condition == "tenant_faction_new_conflict":
        conflicts = list(facts.get("new_conflicts") or [])
        selected["new_conflicts"] = (
            [conflicts[event_index]] if 0 <= event_index < len(conflicts) else []
        )
    return selected


def _snapshots_for_system(snapshot_engine, system: str) -> list[dict[str, Any]]:
    with snapshot_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT ticktime, payload_json FROM system_tick_snapshot "
                "WHERE system_name = :system AND is_settled = 1 "
                "ORDER BY ticktime DESC LIMIT 40"
            ),
            {"system": system},
        ).mappings().all()
    return [dict(row) for row in rows]


def _global_tenant_systems(eddn_engine, tenant_faction: str) -> list[str]:
    if eddn_engine is None or not tenant_faction:
        return []
    aliases = _tenant_faction_aliases(tenant_faction)
    predicates = " OR ".join(
        f"lower(name) = lower(:alias_{index})" for index, _ in enumerate(aliases)
    )
    params = {f"alias_{index}": value for index, value in enumerate(aliases)}
    with eddn_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT system_name FROM eddn_faction WHERE system_name IS NOT NULL AND ("
                + predicates
                + ") ORDER BY system_name COLLATE NOCASE"
            ),
            params,
        ).all()
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]


def _monitored_faction(
    session, rule: dict[str, Any], tenant: dict[str, Any]
) -> str:
    faction_id = rule.get("protected_faction_id")
    if faction_id is None:
        return str(tenant.get("faction_name") or "").strip()
    row = session.execute(
        text(
            "SELECT name FROM protected_faction "
            "WHERE id = :id AND protected = 1"
        ),
        {"id": faction_id},
    ).first()
    return str(row[0] if row else "").strip()


def _targets(
    session,
    rule: dict[str, Any],
    tenant: dict[str, Any],
    eddn_engine=None,
    monitored_faction: str | None = None,
) -> list[str]:
    if rule["target_scope"] == "system":
        requested = str(rule.get("target_system") or "").strip()
        available = watchlist_systems(
            session,
            int(rule["owner_user_id"]) if rule["owner_scope"] == "personal" else None,
        )
        return [requested] if requested.casefold() in {value.casefold() for value in available} else []
    if rule["owner_scope"] == "tenant" and rule.get("package_id"):
        return _global_tenant_systems(
            eddn_engine,
            monitored_faction
            if monitored_faction is not None
            else str(tenant.get("faction_name") or "").strip(),
        )
    return watchlist_systems(
        session,
        int(rule["owner_user_id"]) if rule["owner_scope"] == "personal" else None,
    )


def _queue_delivery(session, tenant: dict[str, Any], rule: dict[str, Any], alert_id: str, now: str):
    destinations: list[tuple[str, str, int | None]] = []
    if rule["owner_scope"] == "personal" and rule["personal_discord"]:
        row = session.execute(
            text("SELECT discord_webhook_ciphertext FROM users WHERE id = :id"),
            {"id": rule["owner_user_id"]},
        ).first()
        if row and decrypt_webhook(row[0]):
            destinations.append(("personal_discord", f"user:{rule['owner_user_id']}", int(rule["owner_user_id"])))
    if (
        rule["owner_scope"] == "tenant"
        and rule["tenant_discord"]
        and rule.get("protected_faction_id") is not None
    ):
        row = session.execute(
            text(
                "SELECT webhook_url FROM protected_faction "
                "WHERE id = :id AND protected = 1"
            ),
            {"id": rule["protected_faction_id"]},
        ).first()
        try:
            validate_discord_webhook(row[0] if row else None)
            configured = True
        except ValueError:
            configured = False
        if configured:
            destinations.append(
                (
                    "protected_faction_discord",
                    f"protected-faction:{rule['protected_faction_id']}",
                    None,
                )
            )
    elif rule["owner_scope"] == "tenant" and rule["tenant_discord"]:
        configured = (tenant.get("discord_webhooks") or {}).get("bgs")
        try:
            validate_discord_webhook(configured)
        except ValueError:
            configured = None
        if configured:
            destinations.append(("tenant_discord", "tenant:bgs", None))
    for channel, destination, recipient in destinations:
        session.execute(
            text(
                "INSERT OR IGNORE INTO dashboard_notification_delivery(id, alert_id, channel, destination_key, "
                "recipient_user_id, status, attempts, next_attempt_at, created_at, updated_at) "
                "VALUES (:id, :alert_id, :channel, :destination, :recipient, 'pending', 0, :now, :now, :now)"
            ),
            {
                "id": str(uuid.uuid4()),
                "alert_id": alert_id,
                "channel": channel,
                "destination": destination,
                "recipient": recipient,
                "now": now,
            },
        )


def _resolve_inactive_alerts(
    session,
    rule: dict[str, Any],
    system_key: str,
    result: dict[str, Any],
    now: str,
) -> int:
    if result.get("active") is None:
        return 0
    condition = rule["condition_type"]
    if condition not in TENANT_RULE_TYPES:
        if result.get("active") is not False:
            return 0
        changed = session.execute(
            text(
                "UPDATE dashboard_bgs_alert SET resolved_at=:now WHERE rule_id=:rule_id "
                "AND system_key=:system_key AND resolved_at IS NULL"
            ),
            {"now": now, "rule_id": rule["id"], "system_key": system_key},
        )
        return max(0, changed.rowcount or 0)
    active_keys = set(result.get("active_event_keys") or [])
    open_alerts = session.execute(
        text(
            "SELECT id, event_key, fired_ticktime, facts_json FROM dashboard_bgs_alert "
            "WHERE rule_id=:rule_id AND system_key=:system_key AND resolved_at IS NULL"
        ),
        {"rule_id": rule["id"], "system_key": system_key},
    ).mappings().all()
    resolved = 0
    for alert in open_alerts:
        facts = _payload(alert["facts_json"])
        alert_keys = set(facts.get("event_keys") or [alert["event_key"]])
        should_resolve = not (alert_keys & active_keys)
        if condition == "tenant_faction_loss":
            should_resolve = alert["fired_ticktime"] != (result.get("facts") or {}).get("ticktime")
        if should_resolve:
            changed = session.execute(
                text("UPDATE dashboard_bgs_alert SET resolved_at=:now WHERE id=:id AND resolved_at IS NULL"),
                {"now": now, "id": alert["id"]},
            )
            resolved += max(0, changed.rowcount or 0)
    return resolved


def _evaluate_tenant(tenant: dict[str, Any], snapshot_engine, eddn_engine=None) -> dict[str, int]:
    tenant_engine = _engine(tenant["db_uri"])
    counts = {"rules": 0, "systems": 0, "alerts": 0, "resolved": 0, "insufficient": 0}
    try:
        ensure_dashboard_schema(tenant_engine)
        with tenant_engine.begin() as conn:
            rules = [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT r.*, p.protected_faction_id, p.protected_faction_name "
                        "FROM dashboard_bgs_rule r "
                        "LEFT JOIN dashboard_bgs_rule_package p ON p.id = r.package_id "
                        "WHERE r.enabled = 1"
                    )
                ).mappings().all()
            ]
            counts["rules"] = len(rules)
            snapshot_cache: dict[str, list[dict[str, Any]]] = {}
            for rule in rules:
                monitored_faction = _monitored_faction(conn, rule, tenant)
                targets = _targets(
                    conn,
                    rule,
                    tenant,
                    eddn_engine,
                    monitored_faction=monitored_faction,
                )
                target_keys = {target.casefold() for target in targets}
                existing_states = {
                    state["system_key"]: state
                    for state in conn.execute(
                        text("SELECT * FROM dashboard_bgs_rule_state WHERE rule_id = :rule_id"),
                        {"rule_id": rule["id"]},
                    ).mappings().all()
                }
                now = utc_now()
                for missing_key in set(existing_states) - target_keys:
                    resolved = conn.execute(
                        text(
                            "UPDATE dashboard_bgs_alert SET resolved_at=:now WHERE rule_id=:rule_id "
                            "AND system_key=:system_key AND resolved_at IS NULL"
                        ),
                        {"now": now, "rule_id": rule["id"], "system_key": missing_key},
                    )
                    counts["resolved"] += max(0, resolved.rowcount or 0)
                    conn.execute(
                        text(
                            "UPDATE dashboard_bgs_rule_state SET status='paused_target_missing', "
                            "condition_active=NULL, observations_json='{}', updated_at=:now "
                            "WHERE rule_id=:rule_id AND system_key=:system_key"
                        ),
                        {"now": now, "rule_id": rule["id"], "system_key": missing_key},
                    )
                if not targets:
                    conn.execute(
                        text(
                            "UPDATE dashboard_bgs_rule_state SET status = 'paused_target_missing', "
                            "last_error = NULL, updated_at = :now WHERE rule_id = :rule_id"
                        ),
                        {"now": utc_now(), "rule_id": rule["id"]},
                    )
                    continue
                for system in targets:
                    counts["systems"] += 1
                    key = system.casefold()
                    if key not in snapshot_cache:
                        snapshot_cache[key] = _snapshots_for_system(snapshot_engine, system)
                    snapshots = snapshot_cache[key]
                    result = evaluate_rule(
                        rule,
                        snapshots,
                        tenant_faction=monitored_faction,
                    )
                    ticktime = (result.get("facts") or {}).get("ticktime")
                    previous = existing_states.get(key)
                    now = utc_now()
                    previous_active = None if previous is None or previous["condition_active"] is None else bool(previous["condition_active"])
                    active = result.get("active")
                    baseline_only = bool(rule.get("package_id")) and (
                        previous is None or previous["status"] == "paused_target_missing"
                    )
                    previous_observations = _payload(previous["observations_json"]) if previous else {}
                    baseline_ticktime = previous_observations.get("baseline_ticktime")
                    if baseline_only:
                        baseline_ticktime = ticktime if active is not None else None
                    if active is None:
                        counts["insufficient"] += 1
                    events = list(result.get("events") or [])
                    should_alert = False
                    if rule["condition_type"] in TENANT_RULE_TYPES:
                        # Re-evaluate late or corrected observations of the same tick.
                        # The alert's unique (rule, system, event, tick) identity
                        # makes retries idempotent without dropping late events.
                        should_alert = bool(events) and not baseline_only and ticktime != baseline_ticktime
                    else:
                        should_alert = bool(active) and previous_active is not True
                    counts["resolved"] += _resolve_inactive_alerts(
                        conn, rule, key, result, now
                    )
                    if should_alert:
                        for event_key in events or ["condition"]:
                            alert_id = str(uuid.uuid4())
                            alert_facts = _event_facts(
                                rule["condition_type"],
                                result.get("facts") or {},
                                event_key,
                            )
                            title, message = _alert_copy(rule, system, alert_facts)
                            inserted = conn.execute(
                                text(
                                    "INSERT OR IGNORE INTO dashboard_bgs_alert(id, rule_id, rule_name, owner_scope, "
                                    "owner_user_id, system_key, system_name, severity, title, message, facts_json, "
                                    "event_key, fired_ticktime, fired_at) VALUES (:id, :rule_id, :rule_name, :owner_scope, "
                                    ":owner_user_id, :system_key, :system_name, :severity, :title, :message, "
                                    ":facts, :event_key, :ticktime, :now)"
                                ),
                                {
                                    "id": alert_id,
                                    "rule_id": rule["id"],
                                    "rule_name": rule["name"],
                                    "owner_scope": rule["owner_scope"],
                                    "owner_user_id": rule["owner_user_id"],
                                    "system_key": key,
                                    "system_name": system,
                                    "severity": rule["severity"],
                                    "title": title,
                                    "message": message,
                                    "facts": json.dumps(
                                        alert_facts, separators=(",", ":")
                                    ),
                                    "event_key": event_key,
                                    "ticktime": ticktime or "unknown",
                                    "now": now,
                                },
                            )
                            if inserted.rowcount:
                                counts["alerts"] += 1
                                _queue_delivery(conn, tenant, rule, alert_id, now)
                    stored_active = previous["condition_active"] if active is None and previous else (None if active is None else int(active))
                    observations = {
                        "facts": result.get("facts") or {},
                        "active_event_keys": result.get("active_event_keys") or [],
                        "baseline_ticktime": baseline_ticktime,
                    }
                    conn.execute(
                        text(
                            "INSERT INTO dashboard_bgs_rule_state(rule_id, system_key, system_name, "
                            "last_evaluated_ticktime, condition_active, status, observations_json, last_error, updated_at) "
                            "VALUES (:rule_id, :system_key, :system_name, :ticktime, :active, :status, :observations, :error, :now) "
                            "ON CONFLICT(rule_id, system_key) DO UPDATE SET system_name=excluded.system_name, "
                            "last_evaluated_ticktime=excluded.last_evaluated_ticktime, condition_active=excluded.condition_active, "
                            "status=excluded.status, observations_json=excluded.observations_json, last_error=excluded.last_error, updated_at=excluded.updated_at"
                        ),
                        {
                            "rule_id": rule["id"],
                            "system_key": key,
                            "system_name": system,
                            "ticktime": (previous["last_evaluated_ticktime"] if previous else None) if active is None else ticktime,
                            "active": stored_active,
                            "status": result["status"],
                            "observations": json.dumps(observations, separators=(",", ":")),
                            "error": result.get("reason"),
                            "now": now,
                        },
                    )
    finally:
        tenant_engine.dispose()
    return counts


def evaluate_all_tenants(tenants: list[dict[str, Any]]) -> None:
    if not _run_lock.acquire(blocking=False):
        logger.info("BGS rule evaluation skipped because a previous run is active")
        return
    snapshot_engine = _engine(os.getenv("SNAPSHOT_DB_URL", "sqlite:///db/bgs_eddn_snapshots.db"))
    eddn_uri = os.getenv("EDDN_DATABASE", "").strip()
    eddn_engine = _engine(eddn_uri) if eddn_uri else None
    try:
        for tenant in tenants:
            if not tenant.get("db_uri"):
                continue
            try:
                result = _evaluate_tenant(tenant, snapshot_engine, eddn_engine)
                logger.info("BGS rule evaluation completed for %s: %s", tenant.get("name"), result)
            except Exception:
                logger.exception("BGS rule evaluation failed for %s", tenant.get("name"))
    finally:
        snapshot_engine.dispose()
        if eddn_engine is not None:
            eddn_engine.dispose()
        _run_lock.release()


def _delivery_webhook(conn, tenant: dict[str, Any], row: dict[str, Any]) -> str | None:
    if row["channel"] == "tenant_discord":
        configured = (tenant.get("discord_webhooks") or {}).get("bgs")
        try:
            return validate_discord_webhook(configured)
        except ValueError:
            return None
    if row["channel"] == "protected_faction_discord":
        try:
            faction_id = int(str(row["destination_key"]).rsplit(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            return None
        protected = conn.execute(
            text(
                "SELECT webhook_url FROM protected_faction "
                "WHERE id = :id AND protected = 1"
            ),
            {"id": faction_id},
        ).first()
        try:
            return validate_discord_webhook(protected[0] if protected else None)
        except ValueError:
            return None
    user = conn.execute(
        text("SELECT discord_webhook_ciphertext FROM users WHERE id = :id"),
        {"id": row["recipient_user_id"]},
    ).first()
    return decrypt_webhook(user[0] if user else None)


def _dispatch_tenant(tenant: dict[str, Any]) -> None:
    engine = _engine(tenant["db_uri"])
    try:
        ensure_dashboard_schema(engine)
        with engine.connect() as conn:
            due = [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT d.*, a.title, a.message, a.severity, a.system_name, a.fired_ticktime, a.fired_at, a.resolved_at, a.facts_json, a.event_key, a.owner_scope "
                        "FROM dashboard_notification_delivery d JOIN dashboard_bgs_alert a ON a.id = d.alert_id "
                        "WHERE d.status IN ('pending','retry') AND d.attempts < 3 "
                        "AND d.next_attempt_at <= :now AND (d.lease_until IS NULL OR d.lease_until < :now) "
                        "ORDER BY d.next_attempt_at LIMIT 25"
                    ),
                    {"now": utc_now()},
                ).mappings().all()
            ]
        for delivery in due:
            now_dt = datetime.now(timezone.utc)
            lease = (now_dt + timedelta(minutes=2)).isoformat()
            with engine.begin() as conn:
                claimed = conn.execute(
                    text(
                        "UPDATE dashboard_notification_delivery SET status='processing', lease_until=:lease, updated_at=:now "
                        "WHERE id=:id AND status IN ('pending','retry') AND (lease_until IS NULL OR lease_until < :now)"
                    ),
                    {"id": delivery["id"], "lease": lease, "now": now_dt.isoformat()},
                )
                if not claimed.rowcount:
                    continue
            error: str | None = None
            with engine.connect() as conn:
                webhook = _delivery_webhook(conn, tenant, delivery)
            if not webhook:
                error = "Discord webhook is no longer configured or decryptable"
            else:
                try:
                    current_data = current_system(delivery["system_name"])
                except Exception:
                    logger.warning("Current BGS data unavailable for Discord alert %s", delivery["alert_id"])
                    current_data = ({}, [])
                try:
                    response = requests.post(
                        webhook,
                        json=notification(delivery, *current_data),
                        timeout=8,
                    )
                    if response.status_code not in (200, 204):
                        error = f"Discord returned HTTP {response.status_code}"
                except requests.RequestException as exc:
                    error = str(exc)[:500]
            with engine.begin() as conn:
                if error is None:
                    conn.execute(
                        text(
                            "UPDATE dashboard_notification_delivery SET status='delivered', attempts=attempts+1, "
                            "delivered_at=:now, lease_until=NULL, last_error=NULL, updated_at=:now WHERE id=:id"
                        ),
                        {"id": delivery["id"], "now": utc_now()},
                    )
                else:
                    attempts = int(delivery["attempts"] or 0) + 1
                    terminal = attempts >= 3
                    delay = (1, 5, 15)[min(attempts - 1, 2)]
                    conn.execute(
                        text(
                            "UPDATE dashboard_notification_delivery SET status=:status, attempts=:attempts, "
                            "next_attempt_at=:next_attempt, lease_until=NULL, last_error=:error, updated_at=:now WHERE id=:id"
                        ),
                        {
                            "id": delivery["id"],
                            "status": "failed" if terminal else "retry",
                            "attempts": attempts,
                            "next_attempt": (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat(),
                            "error": error,
                            "now": utc_now(),
                        },
                    )
    finally:
        engine.dispose()


def dispatch_all_tenants(tenants: list[dict[str, Any]]) -> None:
    for tenant in tenants:
        if not tenant.get("db_uri"):
            continue
        try:
            _dispatch_tenant(tenant)
        except Exception:
            logger.exception("BGS Discord outbox failed for %s", tenant.get("name"))


def start_bgs_rule_scheduler(tenants: list[dict[str, Any]]) -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        evaluate_all_tenants,
        IntervalTrigger(minutes=10),
        args=[tenants],
        id="bgs-rule-evaluation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    scheduler.add_job(
        dispatch_all_tenants,
        IntervalTrigger(minutes=1),
        args=[tenants],
        id="bgs-alert-discord-outbox",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=45),
    )
    scheduler.start()
    _scheduler = scheduler
    atexit.register(lambda: scheduler.shutdown(wait=False) if scheduler.running else None)
    logger.info("BGS rule scheduler started (10-minute evaluation, 1-minute outbox)")
    return scheduler
