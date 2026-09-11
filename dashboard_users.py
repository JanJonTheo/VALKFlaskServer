"""Tenant-local dashboard identity, preferences and user administration.

The historic ``users`` table remains the source of truth.  This module only
adds dashboard-prefixed support tables and additive user columns so Streamlit
and the Discord bot keep using the same credentials and IDs.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
from flask import g, jsonify, request
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError


ROLES = {"member", "leadership", "admin"}
ROLE_CAPABILITIES = {
    "member": {"dashboard:read", "rules:write"},
    "leadership": {
        "dashboard:read",
        "rules:write",
        "tenant-rules:write",
        "bgs-ai:run",
        "objectives:write",
        "reports:send",
        "assessment:run",
    },
    "admin": {
        "dashboard:read",
        "rules:write",
        "tenant-rules:write",
        "bgs-ai:run",
        "objectives:write",
        "reports:send",
        "assessment:run",
        "admin:read",
        "users:read",
        "users:manage",
        "protected-factions:manage",
    },
}
SCHEMA_VERSION = 7
SESSION_HOURS = 12

TENANT_EARLY_WARNING_TEMPLATE_ID = "bgs-tenant-faction-early-warning"
TENANT_EARLY_WARNING_DEFINITION = {
    "items": [
        {
            "key": "tenant-influence-loss",
            "name": "Tenant faction influence loss",
            "condition": {
                "type": "tenant_faction_loss",
                "threshold_pp": 3,
                "comparison": "previous_settled_tick",
            },
            "severity": "warning",
        },
        {
            "key": "tenant-new-conflict",
            "name": "Tenant faction entered a new conflict",
            "condition": {
                "type": "tenant_faction_new_conflict",
                "conflict_types": ["election", "war"],
            },
            "severity": "warning",
        },
        {
            "key": "tenant-influence-below",
            "name": "Tenant faction below 5% influence",
            "condition": {
                "type": "tenant_faction_below",
                "threshold_pp": 5,
            },
            "severity": "critical",
        },
        {
            "key": "tenant-influence-gap",
            "name": "Faction closes to a 2 pp gap",
            "condition": {
                "type": "tenant_faction_gap",
                "threshold_pp": 2,
                "gap_mode": "absolute",
            },
            "severity": "warning",
        },
    ]
}

PROTECTED_FACTION_EARLY_WARNING_TEMPLATE_ID = "bgs-protected-faction-early-warning"
PROTECTED_FACTION_EARLY_WARNING_DEFINITION = {
    "items": [
        {
            "key": "protected-influence-loss",
            "name": "Protected faction influence loss",
            "condition": {
                "type": "tenant_faction_loss",
                "threshold_pp": 3,
                "comparison": "previous_settled_tick",
            },
            "severity": "warning",
        },
        {
            "key": "protected-new-conflict",
            "name": "Protected faction entered a new conflict",
            "condition": {
                "type": "tenant_faction_new_conflict",
                "conflict_types": ["election", "war"],
            },
            "severity": "warning",
        },
        {
            "key": "protected-influence-below",
            "name": "Protected faction below 5% influence",
            "condition": {
                "type": "tenant_faction_below",
                "threshold_pp": 5,
            },
            "severity": "critical",
        },
        {
            "key": "protected-influence-gap",
            "name": "Faction closes to a 2 pp gap",
            "condition": {
                "type": "tenant_faction_gap",
                "threshold_pp": 2,
                "gap_mode": "absolute",
            },
            "severity": "warning",
        },
    ]
}


def dashboard_identity_email(user_id) -> str:
    """Return a stable tenant-local identity for Better Auth credential sessions.

    Historic VALK users do not necessarily have an email address. Better Auth
    requires one even for an explicitly linked provider flow, so this internal
    address bridges the classic username/password login without pretending to
    be a deliverable email address. A verified social address replaces it after
    the user links Discord or Google themselves.
    """

    return f"dashboard-user-{user_id}@tenant.invalid"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sqlite_pragmas(conn) -> None:
    if conn.dialect.name != "sqlite":
        return
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA foreign_keys=ON"))
    conn.execute(text("PRAGMA busy_timeout=5000"))


def _execute_retry(operation, retries: int = 4, base_delay: float = 0.08):
    for attempt in range(retries):
        try:
            return operation()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            time.sleep(base_delay * (attempt + 1))


def _ensure_bgs_alert_event_identity(conn) -> None:
    """Replace the legacy per-tick alert key with a per-event identity.

    SQLite cannot drop the inline legacy UNIQUE constraint in place. Rebuild the
    alert tables together so notification and user-state rows remain intact.
    """

    if conn.dialect.name != "sqlite":
        return
    table_sql = conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='dashboard_bgs_alert'"
        )
    ).scalar_one_or_none()
    normalised = "".join(str(table_sql or "").lower().split())
    desired_key = "unique(rule_id,system_key,event_key,fired_ticktime)"
    legacy_key = "unique(rule_id,system_key,fired_ticktime)"
    if desired_key in normalised or legacy_key not in normalised:
        return

    conn.execute(text("PRAGMA defer_foreign_keys=ON"))
    conn.execute(
        text(
            "ALTER TABLE dashboard_notification_delivery "
            "RENAME TO dashboard_notification_delivery_legacy"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE dashboard_bgs_alert_user_state "
            "RENAME TO dashboard_bgs_alert_user_state_legacy"
        )
    )
    conn.execute(
        text("ALTER TABLE dashboard_bgs_alert RENAME TO dashboard_bgs_alert_legacy")
    )
    conn.execute(
        text(
            """
            CREATE TABLE dashboard_bgs_alert (
                id TEXT PRIMARY KEY,
                rule_id TEXT,
                rule_name TEXT NOT NULL,
                owner_scope TEXT NOT NULL,
                owner_user_id INTEGER,
                system_key TEXT NOT NULL,
                system_name TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                facts_json TEXT NOT NULL DEFAULT '{}',
                event_key TEXT NOT NULL DEFAULT 'condition',
                fired_ticktime TEXT NOT NULL,
                fired_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(rule_id) REFERENCES dashboard_bgs_rule(id) ON DELETE SET NULL,
                FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(rule_id, system_key, event_key, fired_ticktime)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE dashboard_bgs_alert_user_state (
                alert_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                read_at TEXT,
                acknowledged_at TEXT,
                PRIMARY KEY(alert_id, user_id),
                FOREIGN KEY(alert_id) REFERENCES dashboard_bgs_alert(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE dashboard_notification_delivery (
                id TEXT PRIMARY KEY,
                alert_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                destination_key TEXT NOT NULL,
                recipient_user_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                lease_until TEXT,
                last_error TEXT,
                delivered_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(alert_id) REFERENCES dashboard_bgs_alert(id) ON DELETE CASCADE,
                FOREIGN KEY(recipient_user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(alert_id, channel, destination_key)
            )
            """
        )
    )
    conn.execute(
        text(
            "INSERT INTO dashboard_bgs_alert SELECT * "
            "FROM dashboard_bgs_alert_legacy"
        )
    )
    conn.execute(
        text(
            "INSERT INTO dashboard_bgs_alert_user_state SELECT * "
            "FROM dashboard_bgs_alert_user_state_legacy"
        )
    )
    conn.execute(
        text(
            "INSERT INTO dashboard_notification_delivery SELECT * "
            "FROM dashboard_notification_delivery_legacy"
        )
    )
    conn.execute(text("DROP TABLE dashboard_notification_delivery_legacy"))
    conn.execute(text("DROP TABLE dashboard_bgs_alert_user_state_legacy"))
    conn.execute(text("DROP TABLE dashboard_bgs_alert_legacy"))


def ensure_dashboard_schema(engine) -> None:
    """Apply the idempotent dashboard schema to one tenant database."""

    def migrate():
        with engine.begin() as conn:
            _sqlite_pragmas(conn)
            tables = set(inspect(conn).get_table_names())
            if "users" not in tables:
                raise RuntimeError("Tenant database has no users table")

            existing = {column[1] for column in conn.execute(text("PRAGMA table_info(users)"))}
            user_columns = {
                "role": "TEXT NOT NULL DEFAULT 'member'",
                "must_change_password": "INTEGER NOT NULL DEFAULT 0",
                "auth_email": "TEXT",
                "auth_email_verified": "INTEGER NOT NULL DEFAULT 0",
                "auth_image": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
                "last_login_at": "TEXT",
                "discord_webhook_ciphertext": "TEXT",
                "discord_webhook_updated_at": "TEXT",
            }
            for name, definition in user_columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))

            now = utc_now()
            conn.execute(
                text(
                    "UPDATE users SET role = CASE WHEN is_admin = 1 THEN 'admin' "
                    "WHEN role IN ('member','leadership','admin') THEN role ELSE 'member' END"
                )
            )
            conn.execute(text("UPDATE users SET created_at = COALESCE(created_at, :now), updated_at = COALESCE(updated_at, :now)"), {"now": now})
            conn.execute(
                text(
                    "UPDATE users SET auth_email = 'dashboard-user-' || id || '@tenant.invalid' "
                    "WHERE auth_email IS NULL OR trim(auth_email) = ''"
                )
            )

            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_session (
                    id TEXT PRIMARY KEY,
                    expiresAt TEXT NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    createdAt TEXT NOT NULL,
                    updatedAt TEXT NOT NULL,
                    ipAddress TEXT,
                    userAgent TEXT,
                    lastVerifiedAt TEXT,
                    userId INTEGER NOT NULL,
                    FOREIGN KEY(userId) REFERENCES users(id) ON DELETE CASCADE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_account (
                    id TEXT PRIMARY KEY,
                    issuer TEXT NOT NULL,
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
                    FOREIGN KEY(userId) REFERENCES users(id) ON DELETE CASCADE
                )
            """))
            account_columns = {
                column[1]: column
                for column in conn.execute(text("PRAGMA table_info(dashboard_account)"))
            }
            if "issuer" not in account_columns:
                conn.execute(text("ALTER TABLE dashboard_account ADD COLUMN issuer TEXT"))
            provider_issuers = {
                "credential": "local:credential",
                "discord": "local:oauth:discord",
                "google": "https://accounts.google.com",
            }
            providers = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT DISTINCT providerId FROM dashboard_account "
                        "WHERE issuer IS NULL OR trim(issuer) = ''"
                    )
                )
            }
            unknown_providers = providers - provider_issuers.keys()
            if unknown_providers:
                raise RuntimeError(
                    "Cannot safely infer Better Auth issuers for providers: "
                    + ", ".join(sorted(unknown_providers))
                )
            for provider_id in providers:
                conn.execute(
                    text(
                        "UPDATE dashboard_account SET issuer = :issuer "
                        "WHERE providerId = :provider_id AND "
                        "(issuer IS NULL OR trim(issuer) = '')"
                    ),
                    {
                        "issuer": provider_issuers[provider_id],
                        "provider_id": provider_id,
                    },
                )
            collision = conn.execute(
                text(
                    "SELECT issuer, accountId FROM dashboard_account "
                    "GROUP BY issuer, accountId HAVING COUNT(*) > 1 LIMIT 1"
                )
            ).first()
            if collision:
                raise RuntimeError("Better Auth account issuer collision detected")
            account_columns = {
                column[1]: column
                for column in conn.execute(text("PRAGMA table_info(dashboard_account)"))
            }
            if not bool(account_columns["issuer"][3]):
                conn.execute(text("DROP TABLE IF EXISTS dashboard_account_v3"))
                conn.execute(text("""
                    CREATE TABLE dashboard_account_v3 (
                        id TEXT PRIMARY KEY,
                        issuer TEXT NOT NULL,
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
                        FOREIGN KEY(userId) REFERENCES users(id) ON DELETE CASCADE
                    )
                """))
                conn.execute(text("""
                    INSERT INTO dashboard_account_v3(
                        id, issuer, accountId, providerId, userId, accessToken,
                        refreshToken, idToken, accessTokenExpiresAt,
                        refreshTokenExpiresAt, scope, password, createdAt, updatedAt
                    )
                    SELECT id, issuer, accountId, providerId, userId, accessToken,
                           refreshToken, idToken, accessTokenExpiresAt,
                           refreshTokenExpiresAt, scope, password, createdAt, updatedAt
                    FROM dashboard_account
                """))
                original_count = conn.execute(
                    text("SELECT COUNT(*) FROM dashboard_account")
                ).scalar_one()
                migrated_count = conn.execute(
                    text("SELECT COUNT(*) FROM dashboard_account_v3")
                ).scalar_one()
                if original_count != migrated_count:
                    raise RuntimeError("Better Auth account migration lost rows")
                conn.execute(text("DROP TABLE dashboard_account"))
                conn.execute(
                    text("ALTER TABLE dashboard_account_v3 RENAME TO dashboard_account")
                )
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_verification (
                    id TEXT PRIMARY KEY,
                    identifier TEXT NOT NULL,
                    value TEXT NOT NULL,
                    expiresAt TEXT NOT NULL,
                    createdAt TEXT,
                    updatedAt TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_rate_limit (
                    id TEXT PRIMARY KEY,
                    key TEXT NOT NULL UNIQUE,
                    count INTEGER NOT NULL,
                    lastRequest INTEGER NOT NULL
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_view_preference (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    view_key TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    UNIQUE(user_id, view_key)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_audit_event (
                    id TEXT PRIMARY KEY,
                    actor_user_id INTEGER,
                    actor_username TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    outcome TEXT NOT NULL,
                    correlation_id TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE SET NULL
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS protected_faction (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    webhook_url TEXT,
                    description TEXT,
                    protected INTEGER NOT NULL DEFAULT 1
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_bgs_rule_template (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    definition_json TEXT NOT NULL,
                    target_kind TEXT NOT NULL DEFAULT 'watchlist',
                    default_discord INTEGER NOT NULL DEFAULT 1,
                    archived_at TEXT,
                    created_by INTEGER,
                    updated_by INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY(updated_by) REFERENCES users(id) ON DELETE SET NULL
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_bgs_rule_package (
                    id TEXT PRIMARY KEY,
                    template_id TEXT NOT NULL,
                    template_version INTEGER NOT NULL,
                    owner_scope TEXT NOT NULL,
                    owner_user_id INTEGER,
                    owner_key TEXT NOT NULL,
                    target_scope TEXT NOT NULL,
                    protected_faction_id INTEGER,
                    protected_faction_name TEXT,
                    personal_discord INTEGER NOT NULL DEFAULT 0,
                    tenant_discord INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(template_id) REFERENCES dashboard_bgs_rule_template(id),
                    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
                    UNIQUE(template_id, owner_key, target_scope)
                )
            """))
            template_columns = {
                column[1]
                for column in conn.execute(text("PRAGMA table_info(dashboard_bgs_rule_template)"))
            }
            if "target_kind" not in template_columns:
                conn.execute(
                    text(
                        "ALTER TABLE dashboard_bgs_rule_template "
                        "ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'watchlist'"
                    )
                )
            package_columns = {
                column[1]
                for column in conn.execute(text("PRAGMA table_info(dashboard_bgs_rule_package)"))
            }
            for name, definition in {
                "protected_faction_id": "INTEGER",
                "protected_faction_name": "TEXT",
            }.items():
                if name not in package_columns:
                    conn.execute(
                        text(
                            f"ALTER TABLE dashboard_bgs_rule_package ADD COLUMN {name} {definition}"
                        )
                    )
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_bgs_rule (
                    id TEXT PRIMARY KEY,
                    owner_scope TEXT NOT NULL,
                    owner_user_id INTEGER,
                    name TEXT NOT NULL,
                    target_scope TEXT NOT NULL,
                    target_system TEXT,
                    condition_type TEXT NOT NULL,
                    threshold_pp REAL NOT NULL,
                    window_days INTEGER NOT NULL DEFAULT 1,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    personal_discord INTEGER NOT NULL DEFAULT 0,
                    tenant_discord INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                )
            """))
            rule_columns = {
                column[1]
                for column in conn.execute(text("PRAGMA table_info(dashboard_bgs_rule)"))
            }
            for name, definition in {
                "package_id": "TEXT",
                "template_id": "TEXT",
                "template_version": "INTEGER",
                "template_item_key": "TEXT",
                "condition_json": "TEXT",
                "effective_from": "TEXT",
            }.items():
                if name not in rule_columns:
                    conn.execute(
                        text(f"ALTER TABLE dashboard_bgs_rule ADD COLUMN {name} {definition}")
                    )
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_bgs_rule_state (
                    rule_id TEXT NOT NULL,
                    system_key TEXT NOT NULL,
                    system_name TEXT NOT NULL,
                    last_evaluated_ticktime TEXT,
                    condition_active INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    observations_json TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(rule_id, system_key),
                    FOREIGN KEY(rule_id) REFERENCES dashboard_bgs_rule(id) ON DELETE CASCADE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_bgs_alert (
                    id TEXT PRIMARY KEY,
                    rule_id TEXT,
                    rule_name TEXT NOT NULL,
                    owner_scope TEXT NOT NULL,
                    owner_user_id INTEGER,
                    system_key TEXT NOT NULL,
                    system_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    facts_json TEXT NOT NULL DEFAULT '{}',
                    event_key TEXT NOT NULL DEFAULT 'condition',
                    fired_ticktime TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(rule_id) REFERENCES dashboard_bgs_rule(id) ON DELETE SET NULL,
                    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
                    UNIQUE(rule_id, system_key, event_key, fired_ticktime)
                )
            """))
            alert_columns = {
                column[1]
                for column in conn.execute(text("PRAGMA table_info(dashboard_bgs_alert)"))
            }
            if "event_key" not in alert_columns:
                conn.execute(
                    text(
                        "ALTER TABLE dashboard_bgs_alert ADD COLUMN "
                        "event_key TEXT NOT NULL DEFAULT 'condition'"
                    )
                )
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_bgs_alert_user_state (
                    alert_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    read_at TEXT,
                    acknowledged_at TEXT,
                    PRIMARY KEY(alert_id, user_id),
                    FOREIGN KEY(alert_id) REFERENCES dashboard_bgs_alert(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_notification_delivery (
                    id TEXT PRIMARY KEY,
                    alert_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    recipient_user_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    lease_until TEXT,
                    last_error TEXT,
                    delivered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(alert_id) REFERENCES dashboard_bgs_alert(id) ON DELETE CASCADE,
                    FOREIGN KEY(recipient_user_id) REFERENCES users(id) ON DELETE CASCADE,
                    UNIQUE(alert_id, channel, destination_key)
                )
            """))
            _ensure_bgs_alert_event_identity(conn)
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_bgs_ai_report (
                    id TEXT PRIMARY KEY,
                    report_type TEXT NOT NULL,
                    system_name TEXT NOT NULL,
                    requested_by INTEGER,
                    tenant_faction TEXT,
                    source_ticktime TEXT,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    source_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(requested_by) REFERENCES users(id) ON DELETE SET NULL
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_schema_migration (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
            """))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_auth_email ON users(auth_email) WHERE auth_email IS NOT NULL"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_session_user ON dashboard_session(userId)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_account_user ON dashboard_account(userId)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_dashboard_account_issuer_account ON dashboard_account(issuer, accountId)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_preference_user ON dashboard_view_preference(user_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_audit_created ON dashboard_audit_event(created_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_bgs_rule_owner ON dashboard_bgs_rule(owner_scope, owner_user_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_bgs_rule_package ON dashboard_bgs_rule(package_id, template_item_key)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_bgs_template_archived ON dashboard_bgs_rule_template(archived_at, lower(name))"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_bgs_alert_visible ON dashboard_bgs_alert(owner_scope, owner_user_id, fired_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_bgs_alert_active ON dashboard_bgs_alert(resolved_at, fired_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_delivery_due ON dashboard_notification_delivery(status, next_attempt_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dashboard_bgs_ai_report_created ON dashboard_bgs_ai_report(created_at)"))
            conn.execute(text("""
                INSERT OR IGNORE INTO dashboard_account(id, issuer, accountId, providerId, userId, password, createdAt, updatedAt)
                SELECT lower(hex(randomblob(16))), 'local:credential', CAST(id AS TEXT), 'credential', id, password_hash,
                       COALESCE(created_at, :now), COALESCE(updated_at, :now)
                FROM users
            """), {"now": now})
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO dashboard_bgs_rule_template("
                    "id, name, description, version, definition_json, target_kind, default_discord, "
                    "created_at, updated_at) VALUES ("
                    ":id, :name, :description, 1, :definition, 'watchlist', 1, :now, :now)"
                ),
                {
                    "id": TENANT_EARLY_WARNING_TEMPLATE_ID,
                    "name": "Tenant Faction Early Warning",
                    "description": (
                        "Warn when the tenant faction loses influence, enters a new "
                        "Election or War, drops below 5%, or another faction closes "
                        "to an absolute 2 percentage-point gap."
                    ),
                    "definition": json.dumps(
                        TENANT_EARLY_WARNING_DEFINITION,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "now": now,
                },
            )
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO dashboard_bgs_rule_template("
                    "id, name, description, version, definition_json, target_kind, default_discord, "
                    "created_at, updated_at) VALUES ("
                    ":id, :name, :description, 1, :definition, 'protected_faction', 1, :now, :now)"
                ),
                {
                    "id": PROTECTED_FACTION_EARLY_WARNING_TEMPLATE_ID,
                    "name": "Protected Faction Early Warning",
                    "description": (
                        "Warn when a selected protected faction loses influence, enters a new "
                        "Election or War, drops below 5%, or another faction closes "
                        "to an absolute 2 percentage-point gap."
                    ),
                    "definition": json.dumps(
                        PROTECTED_FACTION_EARLY_WARNING_DEFINITION,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "now": now,
                },
            )
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO dashboard_schema_migration"
                    "(version, name, applied_at) VALUES (5, 'bgs_rule_catalog', :now)"
                ),
                {"now": now},
            )
            conn.execute(
                text("INSERT OR IGNORE INTO dashboard_schema_migration(version, name, applied_at) VALUES (:version, :name, :now)"),
                {
                    "version": SCHEMA_VERSION,
                    "name": "protected_faction_early_warning",
                    "now": now,
                },
            )

            from bgs_alert_housekeeping import ensure_housekeeping_schema
            ensure_housekeeping_schema(conn)
            conn.execute(text("INSERT OR IGNORE INTO dashboard_schema_migration(version,name,applied_at) "
                              "VALUES (8,'bgs_alert_housekeeping',:now)"), {'now': now})

    _execute_retry(migrate)


def canonical_user(session, user_id):
    return session.execute(
        text(
            "SELECT id, username, is_admin, active, role, must_change_password, "
            "auth_email, auth_email_verified, auth_image, created_at, updated_at, last_login_at "
            "FROM users WHERE id = :id"
        ),
        {"id": user_id},
    ).mappings().first()


def validate_dashboard_identity(session, identity, require_session: bool = False):
    if not identity:
        raise PermissionError("Dashboard bearer authentication is required")
    user = canonical_user(session, identity.get("sub"))
    if not user or not bool(user["active"]):
        raise PermissionError("Dashboard account is inactive")
    role = user["role"] if user["role"] in ROLES else ("admin" if user["is_admin"] else "member")
    if identity.get("role") != role:
        raise PermissionError("Dashboard role changed; sign in again")
    sid = identity.get("sid")
    if require_session and not sid:
        raise PermissionError("Dashboard session is missing")
    if sid:
        current = session.execute(
            text("SELECT id FROM dashboard_session WHERE id = :id AND userId = :user_id AND expiresAt > :now"),
            {"id": sid, "user_id": user["id"], "now": utc_now()},
        ).first()
        if not current:
            raise PermissionError("Dashboard session was revoked")
    return user


def create_dashboard_session(session, user_id, ip_address=None, user_agent=None):
    now = datetime.now(timezone.utc)
    session_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(48)
    session.execute(
        text(
            "INSERT INTO dashboard_session(id, expiresAt, token, createdAt, updatedAt, ipAddress, userAgent, lastVerifiedAt, userId) "
            "VALUES (:id, :expires, :token, :now, :now, :ip, :agent, :now, :user_id)"
        ),
        {
            "id": session_id,
            "expires": (now + timedelta(hours=SESSION_HOURS)).isoformat(),
            "token": hashlib.sha256(token.encode()).hexdigest(),
            "now": now.isoformat(),
            "ip": ip_address,
            "agent": user_agent,
            "user_id": user_id,
        },
    )
    return session_id


def revoke_user_sessions(session, user_id) -> None:
    session.execute(text("DELETE FROM dashboard_session WHERE userId = :user_id"), {"user_id": user_id})


def capabilities_for(role: str):
    return sorted(ROLE_CAPABILITIES.get(role, set()))


def generate_one_time_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%*-_"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in password) and any(c.isupper() for c in password) and any(c.isdigit() for c in password) and any(c in "!@#$%*-_" for c in password):
            return password


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message, "correlation_id": request.headers.get("x-correlation-id")}}), status


def _serialize_user(user):
    return {
        "id": str(user["id"]),
        "username": user["username"],
        "role": user["role"] if user["role"] in ROLES else ("admin" if user["is_admin"] else "member"),
        "active": bool(user["active"]),
        "must_change_password": bool(user["must_change_password"]),
        "auth_email": user["auth_email"],
        "auth_email_verified": bool(user["auth_email_verified"]),
        "auth_image": user["auth_image"],
        "created_at": user["created_at"],
        "updated_at": user["updated_at"],
        "last_login_at": user["last_login_at"],
    }


def _audit(session, action: str, outcome: str, target_type=None, target_id=None, metadata=None):
    identity = getattr(g, "dashboard_identity", {})
    actor = getattr(g, "dashboard_user", None)
    session.execute(
        text(
            "INSERT INTO dashboard_audit_event(id, actor_user_id, actor_username, actor_role, action, target_type, target_id, outcome, correlation_id, metadata_json, created_at) "
            "VALUES (:id, :actor_id, :username, :role, :action, :target_type, :target_id, :outcome, :correlation, :metadata, :created_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "actor_id": actor["id"] if actor else None,
            "username": actor["username"] if actor else "system",
            "role": identity.get("role", "system"),
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id) if target_id is not None else None,
            "outcome": outcome,
            "correlation": request.headers.get("x-correlation-id"),
            "metadata": json.dumps(metadata or {}, separators=(",", ":")),
            "created_at": utc_now(),
        },
    )


def register_dashboard_user_routes(app, db, require_api_key, commit_with_retry, logger):
    def dashboard_only(capability=None, require_session=True):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                identity = getattr(g, "dashboard_identity", None)
                try:
                    user = validate_dashboard_identity(db.session, identity, require_session=require_session)
                except PermissionError as exc:
                    return _error("UNAUTHENTICATED", str(exc), 401)
                role = user["role"] if user["role"] in ROLES else ("admin" if user["is_admin"] else "member")
                if capability and capability not in ROLE_CAPABILITIES[role]:
                    return _error("FORBIDDEN", "Missing dashboard capability", 403)
                g.dashboard_user = user
                return view(*args, **kwargs)
            return wrapped
        return decorator

    def last_active_admin(user_id) -> bool:
        count = db.session.execute(
            text("SELECT COUNT(*) FROM users WHERE active = 1 AND (role = 'admin' OR is_admin = 1) AND id != :id"),
            {"id": user_id},
        ).scalar_one()
        return count == 0

    @app.route("/api/account/access", methods=["GET"])
    @require_api_key
    @dashboard_only(require_session=False)
    def dashboard_account_access():
        user = g.dashboard_user
        role = user["role"] if user["role"] in ROLES else ("admin" if user["is_admin"] else "member")
        return jsonify({"user": _serialize_user(user), "role": role, "capabilities": capabilities_for(role), "generated_at": utc_now()})

    @app.route("/api/account/change-password", methods=["POST"])
    @require_api_key
    @dashboard_only(require_session=True)
    def dashboard_change_password():
        data = request.get_json(silent=True) or {}
        current_password = str(data.get("current_password") or "")
        new_password = str(data.get("new_password") or "")
        if len(new_password) < 12 or not any(c.isalpha() for c in new_password) or not any(c.isdigit() for c in new_password):
            return _error("INVALID_PASSWORD", "New password must be at least 12 characters and contain letters and numbers", 400)
        row = db.session.execute(text("SELECT password_hash FROM users WHERE id = :id"), {"id": g.dashboard_user["id"]}).first()
        if not row or not bcrypt.checkpw(current_password.encode(), row[0].encode()):
            return _error("INVALID_CREDENTIALS", "Current password is incorrect", 400)
        password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        db.session.execute(
            text("UPDATE users SET password_hash = :password_hash, must_change_password = 0, updated_at = :now WHERE id = :id"),
            {"password_hash": password_hash, "now": utc_now(), "id": g.dashboard_user["id"]},
        )
        db.session.execute(text("UPDATE dashboard_account SET password = :password_hash, updatedAt = :now WHERE userId = :id AND providerId = 'credential'"), {"password_hash": password_hash, "now": utc_now(), "id": g.dashboard_user["id"]})
        _audit(db.session, "account.change_password", "success", "user", g.dashboard_user["id"])
        commit_with_retry(db.session)
        return jsonify({"ok": True})

    @app.route("/api/account/logout", methods=["POST"])
    @require_api_key
    @dashboard_only(require_session=True)
    def dashboard_logout():
        sid = getattr(g, "dashboard_identity", {}).get("sid")
        if sid:
            db.session.execute(text("DELETE FROM dashboard_session WHERE id = :id AND userId = :user_id"), {"id": sid, "user_id": g.dashboard_user["id"]})
        _audit(db.session, "account.logout", "success", "session", sid)
        commit_with_retry(db.session)
        return jsonify({"ok": True})

    @app.route("/api/admin/users", methods=["GET", "POST"])
    @require_api_key
    @dashboard_only("users:manage", require_session=True)
    def dashboard_admin_users():
        if request.method == "GET":
            rows = db.session.execute(
                text("SELECT id, username, is_admin, active, role, must_change_password, auth_email, auth_email_verified, auth_image, created_at, updated_at, last_login_at FROM users ORDER BY lower(username)")
            ).mappings().all()
            return jsonify({"data": [_serialize_user(row) for row in rows], "generated_at": utc_now(), "pagination": {"page": 1, "page_size": len(rows), "total": len(rows)}})

        data = request.get_json(silent=True) or {}
        username = str(data.get("username") or "").strip()
        role = str(data.get("role") or "member")
        email = str(data.get("auth_email") or "").strip() or None
        if len(username) < 3 or len(username) > 128:
            return _error("INVALID_USERNAME", "Username must be between 3 and 128 characters", 400)
        if role not in ROLES:
            return _error("INVALID_ROLE", "Unknown dashboard role", 400)
        if db.session.execute(text("SELECT 1 FROM users WHERE lower(username) = lower(:username)"), {"username": username}).first():
            return _error("USERNAME_EXISTS", "Username already exists", 409)
        password = generate_one_time_password()
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        now = utc_now()
        result = db.session.execute(
            text(
                "INSERT INTO users(username, password_hash, is_admin, active, role, must_change_password, auth_email, auth_email_verified, created_at, updated_at) "
                "VALUES (:username, :password_hash, :is_admin, 1, :role, 1, :email, 0, :now, :now)"
            ),
            {"username": username, "password_hash": password_hash, "is_admin": int(role == "admin"), "role": role, "email": email, "now": now},
        )
        user_id = result.lastrowid
        if not email:
            email = dashboard_identity_email(user_id)
            db.session.execute(
                text("UPDATE users SET auth_email = :email WHERE id = :id"),
                {"email": email, "id": user_id},
            )
        db.session.execute(
            text("INSERT INTO dashboard_account(id, issuer, accountId, providerId, userId, password, createdAt, updatedAt) VALUES (:id, 'local:credential', :account_id, 'credential', :user_id, :password, :now, :now)"),
            {"id": str(uuid.uuid4()), "account_id": str(user_id), "user_id": user_id, "password": password_hash, "now": now},
        )
        _audit(db.session, "users.create", "success", "user", user_id, {"role": role})
        commit_with_retry(db.session)
        return jsonify({"user": _serialize_user(canonical_user(db.session, user_id)), "one_time_password": password}), 201

    @app.route("/api/admin/users/<user_id>", methods=["PATCH", "DELETE"])
    @require_api_key
    @dashboard_only("users:manage", require_session=True)
    def dashboard_admin_user(user_id):
        target = canonical_user(db.session, user_id)
        if not target:
            return _error("NOT_FOUND", "User not found", 404)
        actor_id = str(g.dashboard_user["id"])
        if request.method == "DELETE":
            if str(target["id"]) == actor_id:
                return _error("SELF_DELETE", "You cannot delete your own account", 409)
            if (target["role"] == "admin" or target["is_admin"]) and bool(target["active"]) and last_active_admin(target["id"]):
                return _error("LAST_ADMIN", "The last active administrator cannot be deleted", 409)
            db.session.execute(text("UPDATE dashboard_audit_event SET actor_user_id = NULL, actor_username = '[deleted]' WHERE actor_user_id = :id"), {"id": target["id"]})
            _audit(db.session, "users.delete", "success", "user", target["id"], {"username_hash": hashlib.sha256(target["username"].encode()).hexdigest()[:16]})
            db.session.execute(text("DELETE FROM users WHERE id = :id"), {"id": target["id"]})
            commit_with_retry(db.session)
            return jsonify({"ok": True})

        data = request.get_json(silent=True) or {}
        username = str(data.get("username", target["username"])).strip()
        role = str(data.get("role", target["role"] or ("admin" if target["is_admin"] else "member")))
        active = bool(data.get("active", target["active"]))
        email = data.get("auth_email", target["auth_email"])
        email = str(email or "").strip() or dashboard_identity_email(target["id"])
        if str(target["id"]) == actor_id and not active:
            return _error("SELF_LOCK", "You cannot lock your own account", 409)
        target_is_admin = target["role"] == "admin" or bool(target["is_admin"])
        if target_is_admin and bool(target["active"]) and (role != "admin" or not active) and last_active_admin(target["id"]):
            return _error("LAST_ADMIN", "The last active administrator cannot be locked or demoted", 409)
        if role not in ROLES or len(username) < 3 or len(username) > 128:
            return _error("INVALID_USER", "Username or role is invalid", 400)
        duplicate = db.session.execute(text("SELECT 1 FROM users WHERE lower(username) = lower(:username) AND id != :id"), {"username": username, "id": target["id"]}).first()
        if duplicate:
            return _error("USERNAME_EXISTS", "Username already exists", 409)
        changed_access = role != target["role"] or active != bool(target["active"])
        db.session.execute(
            text("UPDATE users SET username = :username, role = :role, is_admin = :is_admin, active = :active, auth_email = :email, updated_at = :now WHERE id = :id"),
            {"username": username, "role": role, "is_admin": int(role == "admin"), "active": int(active), "email": email, "now": utc_now(), "id": target["id"]},
        )
        if changed_access:
            revoke_user_sessions(db.session, target["id"])
        _audit(db.session, "users.update", "success", "user", target["id"], {"role": role, "active": active})
        commit_with_retry(db.session)
        return jsonify({"user": _serialize_user(canonical_user(db.session, target["id"]))})

    @app.route("/api/admin/users/<user_id>/reset-password", methods=["POST"])
    @require_api_key
    @dashboard_only("users:manage", require_session=True)
    def dashboard_admin_reset_password(user_id):
        target = canonical_user(db.session, user_id)
        if not target:
            return _error("NOT_FOUND", "User not found", 404)
        password = generate_one_time_password()
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        db.session.execute(
            text("UPDATE users SET password_hash = :password_hash, must_change_password = 1, updated_at = :now WHERE id = :id"),
            {"password_hash": password_hash, "now": utc_now(), "id": target["id"]},
        )
        db.session.execute(text("UPDATE dashboard_account SET password = :password_hash, updatedAt = :now WHERE userId = :id AND providerId = 'credential'"), {"password_hash": password_hash, "now": utc_now(), "id": target["id"]})
        revoke_user_sessions(db.session, target["id"])
        _audit(db.session, "users.reset_password", "success", "user", target["id"])
        commit_with_retry(db.session)
        return jsonify({"ok": True, "one_time_password": password})

    @app.route("/api/dashboard/preferences/<view_key>", methods=["GET", "PUT", "DELETE"])
    @require_api_key
    @dashboard_only("dashboard:read", require_session=True)
    def dashboard_preferences(view_key):
        if not view_key or len(view_key) > 96 or not all(char.isalnum() or char in "-_." for char in view_key):
            return _error("INVALID_VIEW", "Invalid view key", 400)
        user_id = g.dashboard_user["id"]
        if request.method == "GET":
            row = db.session.execute(
                text("SELECT schema_version, payload_json, updated_at FROM dashboard_view_preference WHERE user_id = :user_id AND view_key = :view_key"),
                {"user_id": user_id, "view_key": view_key},
            ).mappings().first()
            if not row:
                return jsonify({"data": None, "generated_at": utc_now()})
            return jsonify({"data": {"schema_version": row["schema_version"], "payload": json.loads(row["payload_json"]), "updated_at": row["updated_at"]}, "generated_at": utc_now()})
        if request.method == "DELETE":
            db.session.execute(text("DELETE FROM dashboard_view_preference WHERE user_id = :user_id AND view_key = :view_key"), {"user_id": user_id, "view_key": view_key})
            _audit(db.session, "preference.delete", "success", "view", view_key)
            commit_with_retry(db.session)
            return jsonify({"ok": True})
        raw = request.get_data(cache=True)
        if len(raw) > 16 * 1024:
            return _error("PAYLOAD_TOO_LARGE", "Preference payload exceeds 16 KB", 413)
        data = request.get_json(silent=True) or {}
        version = int(data.get("schema_version") or 1)
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return _error("INVALID_PREFERENCE", "Preference payload must be an object", 400)
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode()) > 16 * 1024:
            return _error("PAYLOAD_TOO_LARGE", "Preference payload exceeds 16 KB", 413)
        now = utc_now()
        db.session.execute(
            text(
                "INSERT INTO dashboard_view_preference(id, user_id, view_key, schema_version, payload_json, created_at, updated_at) "
                "VALUES (:id, :user_id, :view_key, :version, :payload, :now, :now) "
                "ON CONFLICT(user_id, view_key) DO UPDATE SET schema_version = excluded.schema_version, payload_json = excluded.payload_json, updated_at = excluded.updated_at"
            ),
            {"id": str(uuid.uuid4()), "user_id": user_id, "view_key": view_key, "version": version, "payload": encoded, "now": now},
        )
        commit_with_retry(db.session)
        return jsonify({"ok": True, "updated_at": now})

    logger.info("Dashboard tenant user, access and preference routes registered")
