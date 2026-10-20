"""Request-local sessions with bounded, reusable tenant connection pools."""
from threading import RLock

from flask import g, has_app_context
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import scoped_session, sessionmaker


class TenantSQLAlchemy(SQLAlchemy):
    @property
    def session(self):
        if has_app_context() and "tenant_session" in g:
            return g.tenant_session
        return self._default_session

    @session.setter
    def session(self, value):
        # Flask-SQLAlchemy initialization and standalone test/background sessions.
        # Requests never replace this process-global fallback.
        self._default_session = value


_engines = {}
_lock = RLock()


def tenant_engine(uri):
    with _lock:
        if uri not in _engines:
            sqlite = make_url(uri).drivername.startswith("sqlite")
            options = {"connect_args": {"check_same_thread": False, "timeout": 5}} if sqlite else {}
            if sqlite and make_url(uri).database not in (None, "", ":memory:"):
                options.update(pool_size=4, max_overflow=0, pool_timeout=5)
            engine = create_engine(uri, **options)
            if sqlite:
                @event.listens_for(engine, "connect")
                def configure(connection, _record):
                    cursor = connection.cursor()
                    try:
                        cursor.execute("PRAGMA journal_mode=WAL")
                        cursor.execute("PRAGMA synchronous=NORMAL")
                        cursor.execute("PRAGMA foreign_keys=ON")
                        cursor.execute("PRAGMA busy_timeout=5000")
                    finally:
                        cursor.close()
            _engines[uri] = engine
        return _engines[uri]


def bind_tenant_session(uri):
    if g.get("tenant_db_uri") == uri and "tenant_session" in g:
        return
    remove_tenant_session()
    engine = tenant_engine(uri)
    g.tenant_db_engine = engine
    g.tenant_db_uri = uri
    g.tenant_session = scoped_session(sessionmaker(bind=engine))


def remove_tenant_session():
    session = g.pop("tenant_session", None)
    g.pop("tenant_db_uri", None)
    g.pop("tenant_db_engine", None)
    if session is not None:
        session.remove()


def snapshot_history_index_hint(connection):
    """Use the measured index if available; older schemas still work."""
    from sqlalchemy import text
    rows = connection.execute(text("PRAGMA index_info('ix_snapshot_system_ticktime')")).all()
    if [row[2] for row in rows] == ["system_name", "ticktime"]:
        return " INDEXED BY ix_snapshot_system_ticktime"
    return ""
