from functools import wraps
from types import SimpleNamespace

import bcrypt
from flask import Flask, g
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker

from dashboard_users import (
    PROTECTED_FACTION_EARLY_WARNING_TEMPLATE_ID,
    SCHEMA_VERSION,
    TENANT_EARLY_WARNING_TEMPLATE_ID,
    ensure_dashboard_schema,
    register_dashboard_user_routes,
)


def base_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            )
        """))
        admin_hash = bcrypt.hashpw(b"existing-password", bcrypt.gensalt()).decode()
        conn.execute(text("INSERT INTO users(username, password_hash, is_admin, active) VALUES ('admin', :password_hash, 1, 1)"), {"password_hash": admin_hash})
    return engine, admin_hash


def test_schema_is_idempotent_and_preserves_legacy_credentials():
    engine, admin_hash = base_engine()
    ensure_dashboard_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dashboard_bgs_rule_template SET description = 'preserved' "
                "WHERE id = :id"
            ),
            {"id": PROTECTED_FACTION_EARLY_WARNING_TEMPLATE_ID},
        )
    ensure_dashboard_schema(engine)
    with engine.connect() as conn:
        user = conn.execute(text("SELECT password_hash, is_admin, active, role, auth_email FROM users WHERE username = 'admin'")) .first()
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
        assert user == (admin_hash, 1, 1, "admin", "dashboard-user-1@tenant.invalid")
        assert {
            "dashboard_session",
            "dashboard_account",
            "dashboard_verification",
            "dashboard_rate_limit",
            "dashboard_view_preference",
            "dashboard_audit_event",
            "dashboard_schema_migration",
            "dashboard_bgs_rule",
            "dashboard_bgs_rule_state",
            "dashboard_bgs_rule_template",
            "dashboard_bgs_rule_package",
            "dashboard_bgs_alert",
            "dashboard_bgs_alert_user_state",
            "dashboard_notification_delivery",
            "dashboard_bgs_ai_report",
            "protected_faction",
        } <= tables
        templates = conn.execute(
            text(
                "SELECT id, target_kind, description FROM dashboard_bgs_rule_template "
                "ORDER BY id"
            )
        ).all()
        assert (TENANT_EARLY_WARNING_TEMPLATE_ID, "watchlist") in {
            (row[0], row[1]) for row in templates
        }
        assert (
            PROTECTED_FACTION_EARLY_WARNING_TEMPLATE_ID,
            "protected_faction",
            "preserved",
        ) in templates
        package_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(dashboard_bgs_rule_package)"))
        }
        assert {"protected_faction_id", "protected_faction_name"} <= package_columns
        migrations = conn.execute(
            text(
                "SELECT COUNT(*) FROM dashboard_schema_migration WHERE version = :version"
            ),
            {"version": SCHEMA_VERSION},
        ).scalar_one()
        assert migrations == 1
        credential = conn.execute(
            text(
                "SELECT issuer, accountId, providerId, userId, password "
                "FROM dashboard_account WHERE providerId = 'credential'"
            )
        ).one()
        assert credential == ("local:credential", "1", "credential", 1, admin_hash)
        issuer_column = next(
            row for row in conn.execute(text("PRAGMA table_info(dashboard_account)"))
            if row[1] == "issuer"
        )
        assert issuer_column[3] == 1


def test_schema_migrates_better_auth_1_6_accounts_to_issuer_identity():
    engine, admin_hash = base_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE dashboard_account (
                id TEXT PRIMARY KEY,
                accountId TEXT NOT NULL,
                providerId TEXT NOT NULL,
                userId INTEGER NOT NULL,
                accessToken TEXT,
                refreshToken TEXT,
                idToken TEXT,
                accessTokenExpiresAt TEXT,
                refreshTokenExpiresAt TEXT,
                scope TEXT,
                password TEXT,
                createdAt TEXT NOT NULL,
                updatedAt TEXT NOT NULL,
                FOREIGN KEY(userId) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(providerId, accountId)
            )
        """))
        conn.execute(
            text("""
                INSERT INTO dashboard_account(
                    id, accountId, providerId, userId, password, createdAt, updatedAt
                ) VALUES (
                    'legacy-credential', '1', 'credential', 1, :password,
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                )
            """),
            {"password": admin_hash},
        )

    ensure_dashboard_schema(engine)
    ensure_dashboard_schema(engine)

    with engine.connect() as conn:
        account = conn.execute(
            text(
                "SELECT id, issuer, accountId, providerId, userId, password "
                "FROM dashboard_account"
            )
        ).one()
        assert account == (
            "legacy-credential",
            "local:credential",
            "1",
            "credential",
            1,
            admin_hash,
        )
        issuer_column = next(
            row for row in conn.execute(text("PRAGMA table_info(dashboard_account)"))
            if row[1] == "issuer"
        )
        assert issuer_column[3] == 1
        unique_indexes = {
            row[1]
            for row in conn.execute(text("PRAGMA index_list(dashboard_account)"))
            if row[2] == 1
        }
        assert "uq_dashboard_account_issuer_account" in unique_indexes
        user = conn.execute(
            text("SELECT password_hash, is_admin, active FROM users WHERE id = 1")
        ).one()
        assert user == (admin_hash, 1, 1)


def configured_client():
    engine, _ = base_engine()
    ensure_dashboard_schema(engine)
    session = scoped_session(sessionmaker(bind=engine))
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO dashboard_session(id, expiresAt, token, createdAt, updatedAt, lastVerifiedAt, userId)
            VALUES ('session-admin', '2999-01-01T00:00:00+00:00', 'hash', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 1)
        """))
    app = Flask(__name__)
    db = SimpleNamespace(session=session)

    def require_api_key(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            from flask import request
            if request.headers.get("authorization") == "Bearer valid":
                g.dashboard_identity = {
                    "sub": "1",
                    "role": "admin",
                    "capabilities": ["users:manage", "dashboard:read"],
                    "sid": "session-admin",
                }
            return view(*args, **kwargs)
        return wrapped

    register_dashboard_user_routes(app, db, require_api_key, lambda current: current.commit(), app.logger)
    return app.test_client(), session


def test_api_key_alone_cannot_manage_users_and_last_admin_is_protected():
    client, session = configured_client()
    denied = client.get("/api/admin/users", headers={"apikey": "legacy"})
    assert denied.status_code == 401

    locked = client.patch(
        "/api/admin/users/1",
        headers={"authorization": "Bearer valid"},
        json={"active": False},
    )
    assert locked.status_code == 409
    assert locked.get_json()["error"]["code"] == "SELF_LOCK"
    session.remove()


def test_create_reset_and_delete_user_revokes_related_dashboard_data():
    client, session = configured_client()
    created = client.post(
        "/api/admin/users",
        headers={"authorization": "Bearer valid"},
        json={"username": "temporary-member", "role": "member"},
    )
    assert created.status_code == 201
    payload = created.get_json()
    assert payload["one_time_password"]
    assert payload["user"]["must_change_password"] is True
    assert payload["user"]["auth_email"].endswith("@tenant.invalid")
    user_id = payload["user"]["id"]
    account = session.execute(
        text(
            "SELECT issuer, accountId, providerId FROM dashboard_account "
            "WHERE userId = :user_id"
        ),
        {"user_id": user_id},
    ).one()
    assert account == ("local:credential", str(user_id), "credential")

    reset = client.post(f"/api/admin/users/{user_id}/reset-password", headers={"authorization": "Bearer valid"})
    assert reset.status_code == 200
    assert reset.get_json()["one_time_password"] != payload["one_time_password"]

    deleted = client.delete(f"/api/admin/users/{user_id}", headers={"authorization": "Bearer valid"})
    assert deleted.status_code == 200
    assert session.execute(text("SELECT 1 FROM users WHERE id = :id"), {"id": user_id}).first() is None
    session.remove()
