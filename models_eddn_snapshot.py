import os
import uuid
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, String, DateTime, Boolean, JSON,
    UniqueConstraint, Index, event
)
from sqlalchemy.orm import declarative_base, sessionmaker


# -------------------------------------------------------------------
# Snapshot DB (global, separate from eddn.db)
# -------------------------------------------------------------------

SNAPSHOT_DB_URI = "sqlite:///db/bgs_eddn_snapshots.db"


def _uuid() -> str:
    return str(uuid.uuid4())


def _is_sqlite(url: str) -> bool:
    return (url or "").startswith("sqlite")


def _create_engine(db_url: str):
    """Create engine with SQLite tuning (WAL + NORMAL) and longer timeout."""
    if _is_sqlite(db_url):
        engine = create_engine(
            db_url,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA temp_store=MEMORY;")
            cur.close()

        event.listen(engine, "connect", _set_sqlite_pragma)
        return engine

    return create_engine(db_url, echo=False, future=True)


engine = _create_engine(SNAPSHOT_DB_URI)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

BaseSnapshot = declarative_base()


class SystemTickSnapshot(BaseSnapshot):
    """
    One row per (system_name, ticktime).

    ticktime is stored as the string provided by fdev_tick_monitor.last_tick['value'].
    """

    __tablename__ = "system_tick_snapshot"

    id = Column(String(36), primary_key=True, default=_uuid)

    # Tick identifier (string) - we only have ticktime
    ticktime = Column(String(64), index=True, nullable=False)

    system_name = Column(String(255), index=True, nullable=False)
    system_address = Column(String(64), index=True, nullable=True)

    payload_json = Column(JSON, nullable=False)

    received_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

    is_settled = Column(Boolean, default=False, index=True)

    __table_args__ = (
        UniqueConstraint("system_name", "ticktime", name="uq_system_tick_snapshot"),
        Index("ix_snapshot_system_ticktime", "system_name", "ticktime"),
        Index("ix_snapshot_ticktime_system", "ticktime", "system_name"),
    )


def init_snapshot_models() -> None:
    BaseSnapshot.metadata.create_all(bind=engine)
