"""Warm the persistent Spansh cache for all tenant watchlists."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import sqlite3
from pathlib import Path

from spansh_facility_cache import (
    WATCHLIST_VIEW_KEY,
    _tenant_database_candidates,
    refresh_tenant_watchlists,
)


BASE_DIR = Path(__file__).resolve().parent


def watchlist_diagnostics(tenants: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return non-sensitive cache-warming diagnostics for every tenant DB."""

    diagnostics: list[dict[str, object]] = []
    for tenant in tenants:
        item: dict[str, object] = {
            "tenant": str(tenant.get("name") or "unknown"),
            "database_exists": False,
            "preference_rows": 0,
        }
        try:
            for path in _tenant_database_candidates(
                str(tenant.get("db_uri") or "")
            ):
                if not path.exists():
                    continue
                item["database_exists"] = True
                try:
                    with closing(
                        sqlite3.connect(
                            f"file:{path.as_posix()}?mode=ro", uri=True
                        )
                    ) as connection:
                        item["preference_rows"] = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM dashboard_view_preference "
                                "WHERE view_key = ?",
                                (WATCHLIST_VIEW_KEY,),
                            ).fetchone()[0]
                        )
                    break
                except sqlite3.Error:
                    continue
            else:
                raise ValueError("No tenant database with dashboard preferences found")
        except Exception as exc:
            item["error"] = str(exc)[:300]
        diagnostics.append(item)
    return diagnostics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh entries even when their local cache is still current.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Print non-sensitive tenant database diagnostics with the result.",
    )
    arguments = parser.parse_args()
    tenants = json.loads((BASE_DIR / "tenant.json").read_text(encoding="utf-8"))
    result = refresh_tenant_watchlists(tenants, force=arguments.force)
    if arguments.diagnose:
        result["diagnostics"] = watchlist_diagnostics(tenants)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
