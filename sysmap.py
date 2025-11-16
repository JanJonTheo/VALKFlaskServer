# sysmap_blueprint.py
from __future__ import annotations

import os
import time
import hashlib
import urllib.parse
from pathlib import Path
from datetime import timedelta

from flask import Blueprint, request, send_file, jsonify

# --- Blueprint ---
# url_prefix="/api" -> finaler Pfad: /api/sysmap/<system>
sysmap_bp = Blueprint("sysmap", __name__, url_prefix="/api")

# --- Konfiguration (per .env oder Defaults) ---
# Cache-Verzeichnis und TTL:
SYSMAP_CACHE_DIR = Path(os.getenv("SYSMAP_CACHE_DIR", "cache/sysmaps")).resolve()
SYSMAP_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# TTL in Tagen (Standard 7)
SYSMAP_CACHE_TTL_DAYS = int(os.getenv("SYSMAP_CACHE_TTL_DAYS", "7"))
SYSMAP_CACHE_TTL_SECONDS = int(timedelta(days=SYSMAP_CACHE_TTL_DAYS).total_seconds())

# Quelle:
SYSMAP_SOURCE_BASE = os.getenv(
    "SYSMAP_SOURCE_BASE",
    "https://elitedangereuse.fr/outils/sysmap.php"
)


def _clamp_width(w_str: str, default: int = 1280, lo: int = 640, hi: int = 1920) -> int:
    try:
        w = int(w_str)
    except Exception:
        w = default
    return max(lo, min(hi, w))


def _cache_file_for(system: str, width: int, full: bool) -> Path:
    key = f"{system}|{width}|{int(full)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return SYSMAP_CACHE_DIR / f"{digest}.png"


def _is_fresh(p: Path) -> bool:
    if not p.exists():
        return False
    age = time.time() - p.stat().st_mtime
    return age < SYSMAP_CACHE_TTL_SECONDS


@sysmap_bp.get("/sysmap/<path:system>")
def render_sysmap(system: str):
    """
    Rendert die Systemkarte als PNG (WebGL via Chromium + SwiftShader) und cached sie.
    Query:
      - w: Breite (px)
      - h: Höhe  (px)
      - full: '1' Fullpage, '0' nur Canvas
      - r: '1' Render-Button klicken
    """
    # Systemnamen robust normalisieren
    # Pfad kommt ggf. mit %20, oder (fehlerhaft) mit '+' an. Beides in echte Leerzeichen wandeln.
    raw_system = system or ""
    normalized_system = urllib.parse.unquote(raw_system).replace("+", " ").strip()

    width = _clamp_width(request.args.get("w", "1280"))
    try:
        height = max(600, int(request.args.get("h", "900")))
    except Exception:
        height = 900
    full = request.args.get("full", "1") == "1"
    click_render = request.args.get("r", "1") == "1"

    # Nur für die Weiterleitungs-URL zur EDGIS-Seite brauchen wir query-encoding mit '+'
    encoded_system_q = urllib.parse.quote_plus(normalized_system)
    src_url = f"{SYSMAP_SOURCE_BASE}?system={encoded_system_q}"

    # Cache-Schlüssel auf Basis des **normalisierten** Namens
    cache_file = _cache_file_for(normalized_system, width, full)

    if _is_fresh(cache_file):
        return send_file(str(cache_file), mimetype="image/png")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            # WICHTIG: Chromium verwenden, Firefox headless rendert häufig kein WebGL
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--ignore-gpu-blocklist",
                    "--use-gl=swiftshader",
                    "--use-angle=swiftshader",
                ],
            )
            page = browser.new_page(viewport={"width": width, "height": height})

            # Laden bis Ruhe im Netzverkehr, damit Assets/JS da sind
            page.goto(src_url, wait_until="networkidle", timeout=45_000)

            # Optional: "Render"-Button auslösen (UI-abhängig, best effort)
            if click_render:
                try:
                    # verschiedene Selektoren probieren:
                    page.get_by_role("button", name="Render").click(timeout=1500)
                except Exception:
                    try:
                        page.locator("text=Render").first.click(timeout=1500)
                    except Exception:
                        pass

            # Minimale Stabilisierung, damit WebGL zeichnen kann
            page.wait_for_timeout(1200)

            if full:
                # Gesamte Seite ablichten (inkl. Controls/Headline)
                page.screenshot(path=str(cache_file), full_page=True)
            else:
                # Nur das Canvas screenshotten – sauberer und kleiner
                try:
                    canvas = page.locator("canvas").first
                    canvas.wait_for(timeout=5000)
                    canvas.screenshot(path=str(cache_file))
                except Exception:
                    # Fallback auf ganze Seite
                    page.screenshot(path=str(cache_file), full_page=False)

            browser.close()

        return send_file(str(cache_file), mimetype="image/png")

    except Exception as e:
        return jsonify({"error": "render_failed", "detail": str(e), "source": src_url}), 502

