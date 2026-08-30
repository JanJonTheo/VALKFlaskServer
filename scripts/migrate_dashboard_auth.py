"""Back up and idempotently migrate tenant-local dashboard auth tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard_users import ensure_dashboard_schema  # noqa: E402


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def sqlite_path(uri: str, database_root: Path) -> Path:
    url = make_url(uri)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Only file-backed SQLite tenant databases are supported")
    path = Path(url.database)
    return path if path.is_absolute() else (database_root / path).resolve()


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as backup_db:
        source_db.backup(backup_db)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", action="append", help="Exact tenant name; repeat for multiple tenants")
    parser.add_argument("--apply", action="store_true", help="Create backups and apply migrations")
    parser.add_argument("--tenant-file", default=str(ROOT / "tenant.json"))
    parser.add_argument("--backup-dir", default=str(ROOT / "backup" / "dashboard-auth"))
    parser.add_argument("--database-root", default=str(ROOT), help="Base directory used for relative sqlite:/// URIs")
    args = parser.parse_args()

    tenants = json.loads(Path(args.tenant_file).read_text(encoding="utf-8"))
    requested = set(args.tenant or [])
    selected = [tenant for tenant in tenants if not requested or tenant.get("name") in requested]
    if requested - {tenant.get("name") for tenant in selected}:
        parser.error("At least one requested tenant was not found")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for tenant in selected:
        source = sqlite_path(tenant["db_uri"], Path(args.database_root).resolve())
        if not source.exists():
            raise FileNotFoundError(source)
        print(f"{tenant['name']}: {source} sha256={digest(source)}")
        if not args.apply:
            continue
        backup = Path(args.backup_dir) / stamp / source.name
        backup_database(source, backup)
        print(f"  backup={backup} sha256={digest(backup)}")
        # Use the exact file that was resolved and backed up above. Passing the
        # tenant's relative URI again would make SQLAlchemy resolve it against
        # the process working directory, which can differ between CLI and
        # systemd deployments.
        engine = create_engine(
            URL.create(drivername="sqlite", database=str(source)),
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        ensure_dashboard_schema(engine)
        engine.dispose()
        print(f"  migrated sha256={digest(source)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
