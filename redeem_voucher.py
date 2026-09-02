"""Shared RedeemVoucher normalization and legacy raw-payload helpers."""

from __future__ import annotations

import ast
import json
from typing import Any, Mapping


def parse_event_payload(raw_payload: Any) -> dict[str, Any] | None:
    """Parse current JSON and legacy ``str(dict)`` event payloads."""
    if isinstance(raw_payload, dict):
        return dict(raw_payload)
    if not isinstance(raw_payload, str) or not raw_payload.strip():
        return None

    for loader in (json.loads, ast.literal_eval):
        try:
            parsed = loader(raw_payload)
        except (TypeError, ValueError, SyntaxError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def redeem_voucher_factions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return valid faction allocations from a RedeemVoucher payload.

    Frontier bounty vouchers normally provide ``Factions``. Manual/legacy
    bounty records can instead contain the lossless single-faction form
    ``Faction`` + ``Amount``; only that form is synthesized here.
    """
    raw_factions = payload.get("Factions")
    factions: list[dict[str, Any]] = []
    if isinstance(raw_factions, list):
        for entry in raw_factions:
            if not isinstance(entry, Mapping):
                continue
            faction_name = entry.get("Faction")
            if not isinstance(faction_name, str) or not faction_name.strip():
                continue
            normalized = dict(entry)
            normalized["Faction"] = faction_name.strip()
            factions.append(normalized)
        if factions:
            if len(factions) == 1 and factions[0].get("Amount") is None:
                factions[0]["Amount"] = payload.get("Amount")
            return factions

    voucher_type = str(payload.get("Type") or "").casefold()
    faction_name = payload.get("Faction")
    if (
        voucher_type == "bounty"
        and isinstance(faction_name, str)
        and faction_name.strip()
    ):
        return [{"Faction": faction_name.strip(), "Amount": payload.get("Amount")}]

    return []


def primary_redeem_voucher_faction(
    payload: Mapping[str, Any],
    factions: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return the unambiguous faction used by legacy single-faction APIs."""
    singular = payload.get("Faction")
    if isinstance(singular, str) and singular.strip():
        return singular.strip()

    resolved = factions if factions is not None else redeem_voucher_factions(payload)
    if len(resolved) == 1:
        faction_name = resolved[0].get("Faction")
        if isinstance(faction_name, str) and faction_name.strip():
            return faction_name.strip()
    return None


def normalize_redeem_voucher_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    """Normalize an incoming voucher without inventing multi-faction data."""
    normalized = dict(payload)
    factions = redeem_voucher_factions(normalized)
    has_valid_faction_list = bool(redeem_voucher_factions({
        "Factions": normalized.get("Factions"),
    }))
    if factions and not has_valid_faction_list:
        normalized["Factions"] = factions
    primary_faction = primary_redeem_voucher_faction(normalized, factions)
    return normalized, factions, primary_faction


def encode_factions(factions: list[dict[str, Any]]) -> str | None:
    if not factions:
        return None
    return json.dumps(factions, ensure_ascii=False, separators=(",", ":"))
