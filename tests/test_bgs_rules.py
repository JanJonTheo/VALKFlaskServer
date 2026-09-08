import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import uuid
from functools import wraps
from types import SimpleNamespace

import requests
from flask import Flask, g, request
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker

from bgs_rule_scheduler import _delivery_webhook, _dispatch_tenant, _evaluate_tenant, evaluate_rule
from bgs_rules import _call_openai, decrypt_webhook, encrypt_webhook, expansion_range, register_bgs_rule_routes, validate_discord_webhook
from dashboard_users import _ensure_bgs_alert_event_identity, ensure_dashboard_schema, utc_now


def snapshot(ticktime, controller="Controller", controller_influence=0.40, rival=0.30):
    return {
        "ticktime": ticktime,
        "payload_json": {
            "SystemFaction": {"Name": controller},
            "Factions": [
                {"Name": controller, "Influence": controller_influence},
                {"Name": "Rival", "Influence": rival},
            ],
        },
    }


class RuleEvaluationTest(unittest.TestCase):
    def rule(self, condition, threshold=5, days=1):
        return {
            "condition_type": condition,
            "threshold_pp": threshold,
            "window_days": days,
        }

    def test_all_rule_types_use_percentage_points(self):
        current = snapshot("2026-08-29T12:00:00Z", controller_influence=0.20, rival=0.18)
        baseline = snapshot("2026-08-28T12:00:00Z", controller_influence=0.28, rival=0.25)
        self.assertTrue(evaluate_rule(self.rule("controller_below", 25), [current])["active"])
        self.assertTrue(evaluate_rule(self.rule("controller_gap", 3), [current])["active"])
        gain_current = snapshot("2026-08-29T12:00:00Z", rival=0.32)
        self.assertTrue(evaluate_rule(self.rule("competitor_gain", 5), [gain_current, baseline])["active"])
        self.assertTrue(evaluate_rule(self.rule("competitor_loss", 5), [current, baseline])["active"])
        self.assertTrue(evaluate_rule(self.rule("controller_loss", 5), [current, baseline])["active"])

    def test_missing_baseline_and_controller_change_are_insufficient(self):
        current = snapshot("2026-08-29T12:00:00Z")
        self.assertEqual(
            evaluate_rule(self.rule("controller_loss"), [current])["status"],
            "insufficient_data",
        )
        old = snapshot("2026-08-28T12:00:00Z", controller="Old Controller")
        self.assertEqual(
            evaluate_rule(self.rule("controller_loss"), [current, old])["status"],
            "insufficient_data",
        )

    @staticmethod
    def tenant_snapshot(ticktime, influence, rival, conflicts=None):
        return {
            "ticktime": ticktime,
            "payload_json": {
                "SystemFaction": {"Name": "Controller"},
                "Factions": [
                    {"Name": "Controller", "Influence": 0.50},
                    {"Name": "Test Faction", "Influence": influence},
                    {"Name": "Rival", "Influence": rival},
                ],
                "Conflicts": conflicts or [],
            },
        }

    @staticmethod
    def tenant_rule(condition):
        return {
            "condition_type": condition["type"],
            "condition_json": json.dumps(condition),
            "threshold_pp": condition.get("threshold_pp", 1),
            "window_days": 1,
        }

    def test_tenant_faction_transition_rules_allow_days_between_snapshots(self):
        previous = self.tenant_snapshot("2026-08-20T12:00:00Z", 0.08, 0.04)
        current = self.tenant_snapshot("2026-08-29T12:00:00Z", 0.04, 0.02)

        loss = evaluate_rule(
            self.tenant_rule({"type": "tenant_faction_loss", "threshold_pp": 3}),
            [current, previous],
            "Test Faction",
        )
        below = evaluate_rule(
            self.tenant_rule({"type": "tenant_faction_below", "threshold_pp": 5}),
            [current, previous],
            "Test Faction",
        )
        gap = evaluate_rule(
            self.tenant_rule({"type": "tenant_faction_gap", "threshold_pp": 2}),
            [current, previous],
            "Test Faction",
        )

        self.assertEqual(loss["events"], ["loss:2026-08-29T12:00:00Z"])
        self.assertEqual(below["events"], ["below"])
        self.assertEqual(gap["events"], ["gap:rival"])
        missing = evaluate_rule(
            self.tenant_rule({"type": "tenant_faction_loss", "threshold_pp": 3}),
            [current],
            "Test Faction",
        )
        self.assertEqual(missing["status"], "insufficient_data")

    def test_windowed_rule_accepts_older_available_baseline(self):
        current = snapshot("2026-09-08T12:00:00Z", controller_influence=0.20)
        previous = snapshot("2026-09-01T12:00:00Z", controller_influence=0.30)
        result = evaluate_rule(self.rule("controller_loss", 5), [current, previous])
        self.assertTrue(result["active"])
        self.assertEqual(result["facts"]["baseline_ticktime"], previous["ticktime"])
        self.assertEqual(result["facts"]["comparison_days"], 7)

    def test_new_conflict_ignores_status_changes_and_civil_war(self):
        election = {
            "Faction1": {"Name": "Test Faction"},
            "Faction2": {"Name": "Rival"},
            "WarType": "$Election;",
            "Status": "$FactionWarPending;",
        }
        active_election = {**election, "Status": "$FactionWarActive;"}
        civil_war = {**election, "WarType": "$CivilWar;"}
        empty = self.tenant_snapshot("2026-08-27T12:00:00Z", 0.10, 0.08)
        pending = self.tenant_snapshot("2026-08-28T12:00:00Z", 0.10, 0.08, [election, civil_war])
        active = self.tenant_snapshot("2026-08-29T12:00:00Z", 0.10, 0.08, [active_election])
        rule = self.tenant_rule(
            {
                "type": "tenant_faction_new_conflict",
                "conflict_types": ["election", "war"],
            }
        )
        entered = evaluate_rule(rule, [pending, empty], "Test Faction")
        status_change = evaluate_rule(rule, [active, pending], "Test Faction")
        self.assertEqual(len(entered["events"]), 1)
        self.assertEqual(entered["facts"]["new_conflicts"][0]["type"], "election")
        self.assertEqual(status_change["events"], [])


class WebhookSecurityTest(unittest.TestCase):
    def test_webhook_validation_and_encryption(self):
        webhook = "https://discord.com/api/webhooks/123456/token_value"
        with patch.dict(os.environ, {"VALK_WEBHOOK_ENCRYPTION_KEY": "test-secret-that-is-long-and-deployment-specific"}):
            ciphertext = encrypt_webhook(webhook)
            self.assertNotIn("token_value", ciphertext)
            self.assertEqual(decrypt_webhook(ciphertext), webhook)
        with self.assertRaises(ValueError):
            validate_discord_webhook("https://discord.com.evil.invalid/api/webhooks/123/token")
        with self.assertRaises(ValueError):
            validate_discord_webhook("http://discord.com/api/webhooks/123/token")

    def test_protected_delivery_resolves_the_current_webhook_without_persisting_it(self):
        engine = create_engine("sqlite://")
        webhook = "https://discord.com/api/webhooks/123456/current_token"
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE protected_faction("
                    "id INTEGER PRIMARY KEY, name TEXT, webhook_url TEXT, protected INTEGER)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO protected_faction VALUES "
                    "(9, 'Aegis Shield', :webhook, 1)"
                ),
                {"webhook": webhook},
            )
            delivery = {
                "channel": "protected_faction_discord",
                "destination_key": "protected-faction:9",
                "recipient_user_id": None,
            }
            self.assertEqual(_delivery_webhook(conn, {}, delivery), webhook)
            self.assertNotIn(webhook, json.dumps(delivery))
            conn.execute(
                text("UPDATE protected_faction SET protected = 0 WHERE id = 9")
            )
            self.assertIsNone(_delivery_webhook(conn, {}, delivery))
        engine.dispose()


class OpenAiReportTest(unittest.TestCase):
    def test_uses_stateless_structured_responses(self):
        report = {
            "summary": "Stable but contested.",
            "risks": [],
            "opportunities": [],
            "asset_exposure": [],
            "recommended_actions": [],
            "uncertainties": [],
            "data_quality": "Settled snapshot and Spansh cache available.",
        }
        client = MagicMock()
        client.responses.create.return_value = SimpleNamespace(output_text=json.dumps(report))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model"}), patch("openai.OpenAI", return_value=client):
            result, model = _call_openai("risk", {"system_name": "Test System"}, 1, "tenant")
        self.assertEqual(result, report)
        self.assertEqual(model, "test-model")
        arguments = client.responses.create.call_args.kwargs
        self.assertFalse(arguments["store"])
        self.assertEqual(arguments["text"]["format"]["type"], "json_schema")
        self.assertTrue(arguments["text"]["format"]["strict"])

    def test_expansion_candidates_use_per_axis_normal_and_extended_cubes(self):
        target = {"x": 0, "y": 0, "z": 0}
        self.assertEqual(expansion_range(target, {"x": 20, "y": 5, "z": 1})["range"], "normal")
        self.assertEqual(expansion_range(target, {"x": 21, "y": 5, "z": 1})["range"], "extended")
        self.assertIsNone(expansion_range(target, {"x": 31, "y": 0, "z": 0}))


class AlertIdentityMigrationTest(unittest.TestCase):
    def test_legacy_alert_identity_migration_preserves_related_rows(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("CREATE TABLE users(id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE dashboard_bgs_rule(id TEXT PRIMARY KEY)"))
            conn.execute(
                text(
                    "CREATE TABLE dashboard_bgs_alert("
                    "id TEXT PRIMARY KEY, rule_id TEXT, rule_name TEXT NOT NULL, owner_scope TEXT NOT NULL, "
                    "owner_user_id INTEGER, system_key TEXT NOT NULL, system_name TEXT NOT NULL, "
                    "severity TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL, facts_json TEXT NOT NULL, "
                    "event_key TEXT NOT NULL DEFAULT 'condition', fired_ticktime TEXT NOT NULL, fired_at TEXT NOT NULL, "
                    "resolved_at TEXT, UNIQUE(rule_id, system_key, fired_ticktime))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE dashboard_bgs_alert_user_state("
                    "alert_id TEXT NOT NULL, user_id INTEGER NOT NULL, read_at TEXT, acknowledged_at TEXT, "
                    "PRIMARY KEY(alert_id, user_id), FOREIGN KEY(alert_id) REFERENCES dashboard_bgs_alert(id) ON DELETE CASCADE, "
                    "FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE dashboard_notification_delivery("
                    "id TEXT PRIMARY KEY, alert_id TEXT NOT NULL, channel TEXT NOT NULL, destination_key TEXT NOT NULL, "
                    "recipient_user_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
                    "next_attempt_at TEXT NOT NULL, lease_until TEXT, last_error TEXT, delivered_at TEXT, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                    "FOREIGN KEY(alert_id) REFERENCES dashboard_bgs_alert(id) ON DELETE CASCADE, "
                    "FOREIGN KEY(recipient_user_id) REFERENCES users(id) ON DELETE CASCADE, "
                    "UNIQUE(alert_id, channel, destination_key))"
                )
            )
            conn.execute(text("INSERT INTO users(id) VALUES (1)"))
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_alert VALUES ("
                    "'alert', NULL, 'Rule', 'personal', 1, 'system', 'System', 'warning', "
                    "'Title', 'Message', '{}', 'gap:rival', '2026-08-29T12:00:00Z', "
                    "'2026-08-29T12:01:00Z', NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_alert_user_state "
                    "VALUES ('alert', 1, '2026-08-29T12:02:00Z', NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_notification_delivery("
                    "id, alert_id, channel, destination_key, recipient_user_id, next_attempt_at, created_at, updated_at) "
                    "VALUES ('delivery', 'alert', 'discord', 'personal:1', 1, "
                    "'2026-08-29T12:01:00Z', '2026-08-29T12:01:00Z', '2026-08-29T12:01:00Z')"
                )
            )
            _ensure_bgs_alert_event_identity(conn)
            table_sql = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='dashboard_bgs_alert'"
                )
            ).scalar_one()
            self.assertIn(
                "UNIQUE(rule_id, system_key, event_key, fired_ticktime)", table_sql
            )
            self.assertEqual(
                conn.execute(text("SELECT COUNT(*) FROM dashboard_bgs_alert")).scalar_one(),
                1,
            )
            self.assertEqual(
                conn.execute(
                    text("SELECT COUNT(*) FROM dashboard_bgs_alert_user_state")
                ).scalar_one(),
                1,
            )
            self.assertEqual(
                conn.execute(
                    text("SELECT COUNT(*) FROM dashboard_notification_delivery")
                ).scalar_one(),
                1,
            )
        engine.dispose()


class RuleSchedulerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.tenant_path = root / "tenant.db"
        self.snapshot_path = root / "snapshots.db"
        self.eddn_path = root / "eddn.db"
        self.tenant_uri = f"sqlite:///{self.tenant_path.as_posix()}"
        self.snapshot_uri = f"sqlite:///{self.snapshot_path.as_posix()}"
        self.eddn_uri = f"sqlite:///{self.eddn_path.as_posix()}"
        tenant_engine = create_engine(self.tenant_uri)
        with tenant_engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1
                )
            """))
            conn.execute(text("INSERT INTO users(id, username, password_hash, is_admin, active) VALUES (1, 'test', 'hash', 0, 1)"))
        ensure_dashboard_schema(tenant_engine)
        now = utc_now()
        self.rule_id = str(uuid.uuid4())
        with tenant_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO dashboard_view_preference(id, user_id, view_key, schema_version, payload_json, created_at, updated_at) "
                    "VALUES ('pref', 1, 'bgs-system-watchlist', 2, :payload, :now, :now)"
                ),
                {"payload": json.dumps({"systems": [{"system": "Test System"}]}), "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_rule(id, owner_scope, owner_user_id, name, target_scope, target_system, "
                    "condition_type, threshold_pp, window_days, severity, personal_discord, tenant_discord, enabled, "
                    "created_by, created_at, updated_at) VALUES (:id, 'personal', 1, 'Controller guard', 'system', "
                    "'Test System', 'controller_below', 25, 1, 'critical', 0, 0, 1, 1, :now, :now)"
                ),
                {"id": self.rule_id, "now": now},
            )
        tenant_engine.dispose()
        self.snapshot_engine = create_engine(self.snapshot_uri)
        with self.snapshot_engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE system_tick_snapshot (
                    ticktime TEXT NOT NULL,
                    system_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    is_settled INTEGER NOT NULL
                )
            """))
        self.eddn_engine = create_engine(self.eddn_uri)
        with self.eddn_engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE eddn_faction (system_name TEXT NOT NULL, name TEXT NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO eddn_faction(system_name, name) VALUES ('Test System', 'Test Faction')"
                )
            )

    def tearDown(self):
        self.eddn_engine.dispose()
        self.snapshot_engine.dispose()
        self.tempdir.cleanup()

    def test_manual_delivery_dispatch_and_retry_use_only_the_tenant_webhook(self):
        engine = create_engine(self.tenant_uri)
        now = utc_now()
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO dashboard_bgs_alert(id,rule_name,owner_scope,owner_user_id,system_key,system_name,"
                "severity,title,message,fired_ticktime,fired_at) VALUES ('manual','Guard','personal',1,'sol','Sol',"
                "'warning','Guard · Sol','Example alarm',:now,:now)"
            ), {"now": now})
            conn.execute(text(
                "INSERT INTO dashboard_notification_delivery(id,alert_id,channel,destination_key,status,attempts,"
                "next_attempt_at,created_at,updated_at) VALUES ('send','manual','tenant_discord','tenant:bgs:manual:request',"
                "'pending',0,:now,:now,:now)"
            ), {"now": now})
        tenant = {"db_uri": self.tenant_uri, "discord_webhooks": {"bgs": "https://discord.com/api/webhooks/123/test_token"}}
        with patch("bgs_rule_scheduler.requests.post", return_value=SimpleNamespace(status_code=503)) as post:
            _dispatch_tenant(tenant)
            self.assertEqual(post.call_args.args[0], tenant["discord_webhooks"]["bgs"])
        with engine.begin() as conn:
            row = conn.execute(text("SELECT status,delivered_at FROM dashboard_notification_delivery")).mappings().one()
            self.assertEqual(row["status"], "retry")
            self.assertIsNone(row["delivered_at"])
            conn.execute(text("UPDATE dashboard_notification_delivery SET next_attempt_at='2000-01-01'"))
        with patch("bgs_rule_scheduler.requests.post", return_value=SimpleNamespace(status_code=204)) as post:
            _dispatch_tenant(tenant)
            body = post.call_args.kwargs["json"]
            self.assertEqual(body["embeds"][0]["title"], "Sol")
            self.assertIn("ACTIVE", body["embeds"][0]["description"])
            self.assertEqual(body["allowed_mentions"], {"parse": []})
        with engine.connect() as conn:
            row = conn.execute(text("SELECT status,delivered_at FROM dashboard_notification_delivery")).mappings().one()
            self.assertEqual(row["status"], "delivered")
            self.assertIsNotNone(row["delivered_at"])
        engine.dispose()

    def add_snapshot(self, ticktime, controller_influence):
        with self.snapshot_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO system_tick_snapshot(ticktime, system_name, payload_json, is_settled) VALUES (:tick, 'Test System', :payload, 1)"),
                {"tick": ticktime, "payload": json.dumps(snapshot(ticktime, controller_influence=controller_influence)["payload_json"])},
            )

    def add_tenant_snapshot(self, ticktime, tenant_influence):
        payload = {
            "SystemFaction": {"Name": "Controller"},
            "Factions": [
                {"Name": "Controller", "Influence": 0.50},
                {"Name": "Test Faction", "Influence": tenant_influence},
                {"Name": "Rival", "Influence": 0.10},
            ],
            "Conflicts": [],
        }
        with self.snapshot_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO system_tick_snapshot(ticktime, system_name, payload_json, is_settled) "
                    "VALUES (:tick, 'Test System', :payload, 1)"
                ),
                {"tick": ticktime, "payload": json.dumps(payload)},
            )

    def test_edge_trigger_is_idempotent_resolves_and_retriggers(self):
        tenant = {"name": "Test", "db_uri": self.tenant_uri, "discord_webhooks": {}}
        self.add_snapshot("2026-08-27T12:00:00Z", 0.20)
        first = _evaluate_tenant(tenant, self.snapshot_engine)
        second = _evaluate_tenant(tenant, self.snapshot_engine)
        self.assertEqual(first["alerts"], 1)
        self.assertEqual(second["alerts"], 0)
        self.add_snapshot("2026-08-28T12:00:00Z", 0.30)
        resolved = _evaluate_tenant(tenant, self.snapshot_engine)
        self.assertEqual(resolved["resolved"], 1)
        self.add_snapshot("2026-08-29T12:00:00Z", 0.20)
        retriggered = _evaluate_tenant(tenant, self.snapshot_engine)
        self.assertEqual(retriggered["alerts"], 1)
        engine = create_engine(self.tenant_uri)
        with engine.connect() as conn:
            alerts = conn.execute(
                text("SELECT resolved_at FROM dashboard_bgs_alert ORDER BY fired_at")
            ).all()
        engine.dispose()
        self.assertEqual(len(alerts), 2)
        self.assertIsNotNone(alerts[0][0])
        self.assertIsNone(alerts[1][0])

    def test_global_catalog_package_forms_a_baseline_then_uses_dynamic_targets(self):
        engine = create_engine(self.tenant_uri)
        now = utc_now()
        package_id = str(uuid.uuid4())
        rule_id = str(uuid.uuid4())
        condition = {"type": "tenant_faction_loss", "threshold_pp": 3}
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM dashboard_bgs_rule"))
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_rule_package(id, template_id, template_version, owner_scope, "
                    "owner_user_id, owner_key, target_scope, personal_discord, tenant_discord, created_by, "
                    "created_at, updated_at) VALUES (:id, 'bgs-tenant-faction-early-warning', 1, 'tenant', "
                    "NULL, 'tenant', 'watchlist_all', 0, 0, 1, :now, :now)"
                ),
                {"id": package_id, "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_rule(id, owner_scope, owner_user_id, name, target_scope, "
                    "target_system, condition_type, threshold_pp, window_days, severity, personal_discord, "
                    "tenant_discord, enabled, created_by, created_at, updated_at, package_id, template_id, "
                    "template_version, template_item_key, condition_json, effective_from) VALUES ("
                    ":id, 'tenant', NULL, 'Tenant loss', 'watchlist_all', NULL, 'tenant_faction_loss', "
                    "3, 1, 'warning', 0, 0, 1, 1, :now, :now, :package_id, "
                    "'bgs-tenant-faction-early-warning', 1, 'tenant-influence-loss', :condition, :now)"
                ),
                {
                    "id": rule_id,
                    "package_id": package_id,
                    "condition": json.dumps(condition),
                    "now": now,
                },
            )
        engine.dispose()
        self.add_tenant_snapshot("2026-08-28T12:00:00Z", 0.40)
        self.add_tenant_snapshot("2026-08-29T12:00:00Z", 0.36)
        tenant = {
            "name": "Test",
            "db_uri": self.tenant_uri,
            "faction_name": "Test Faction",
            "discord_webhooks": {},
        }
        baseline = _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)
        self.assertEqual(baseline["alerts"], 0)
        self.assertEqual(_evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)["alerts"], 0)
        self.add_tenant_snapshot("2026-08-30T12:00:00Z", 0.30)
        fired = _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)
        repeated = _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)
        self.assertEqual(fired["alerts"], 1)
        self.assertEqual(repeated["alerts"], 0)

    def test_late_data_and_multi_day_gaps_fire_once_independently_of_global_ticks(self):
        engine = create_engine(self.tenant_uri)
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE dashboard_bgs_rule SET condition_type='tenant_faction_loss', threshold_pp=3"
            ))
        tenant = {"name": "Test", "db_uri": self.tenant_uri, "faction_name": "Test Faction"}
        self.add_tenant_snapshot("2026-09-08T12:00:00Z", 0.30)
        insufficient = _evaluate_tenant(tenant, self.snapshot_engine)
        self.assertEqual(insufficient["insufficient"], 1)
        with engine.connect() as conn:
            self.assertIsNone(conn.execute(text(
                "SELECT last_evaluated_ticktime FROM dashboard_bgs_rule_state"
            )).scalar())
        # A later global tick for another system must not block this system.
        with self.snapshot_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO system_tick_snapshot(ticktime, system_name, payload_json, is_settled) "
                "VALUES ('2026-09-09T12:00:00Z', 'Other System', '{}', 1)"
            ))
        self.add_tenant_snapshot("2026-09-01T12:00:00Z", 0.40)
        fired = _evaluate_tenant(tenant, self.snapshot_engine)
        repeated = _evaluate_tenant(tenant, self.snapshot_engine)
        self.assertEqual(fired["alerts"], 1)
        self.assertEqual(repeated["alerts"], 0)
        with engine.connect() as conn:
            facts = json.loads(conn.execute(text("SELECT facts_json FROM dashboard_bgs_alert")).scalar())
        self.assertEqual(facts["baseline_ticktime"], "2026-09-01T12:00:00Z")
        self.assertEqual(facts["loss_pp"], 10)
        engine.dispose()

    def test_corrected_data_in_same_tick_can_trigger_after_successful_evaluation(self):
        engine = create_engine(self.tenant_uri)
        with engine.begin() as conn:
            conn.execute(text("UPDATE dashboard_bgs_rule SET condition_type='tenant_faction_loss', threshold_pp=3"))
        engine.dispose()
        tenant = {"name": "Test", "db_uri": self.tenant_uri, "faction_name": "Test Faction"}
        self.add_tenant_snapshot("2026-09-01T12:00:00Z", 0.40)
        self.add_tenant_snapshot("2026-09-08T12:00:00Z", 0.39)
        self.assertEqual(_evaluate_tenant(tenant, self.snapshot_engine)["alerts"], 0)
        corrected = RuleEvaluationTest.tenant_snapshot("2026-09-08T12:00:00Z", 0.30, 0.10)
        with self.snapshot_engine.begin() as conn:
            conn.execute(text("UPDATE system_tick_snapshot SET payload_json=:payload WHERE ticktime=:tick"),
                         {"payload": json.dumps(corrected["payload_json"]), "tick": corrected["ticktime"]})
        self.assertEqual(_evaluate_tenant(tenant, self.snapshot_engine)["alerts"], 1)
        self.assertEqual(_evaluate_tenant(tenant, self.snapshot_engine)["alerts"], 0)

    def test_protected_package_uses_selected_faction_and_pauses_when_disabled(self):
        engine = create_engine(self.tenant_uri)
        now = utc_now()
        package_id = str(uuid.uuid4())
        rule_id = str(uuid.uuid4())
        condition = {"type": "tenant_faction_loss", "threshold_pp": 3}
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM dashboard_bgs_rule"))
            conn.execute(
                text(
                    "INSERT INTO protected_faction(id, name, webhook_url, description, protected) "
                    "VALUES (9, 'Aegis Shield', NULL, 'Protected ally', 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_rule_package(id, template_id, template_version, owner_scope, "
                    "owner_user_id, owner_key, target_scope, protected_faction_id, protected_faction_name, "
                    "personal_discord, tenant_discord, created_by, created_at, updated_at) VALUES ("
                    ":id, 'bgs-protected-faction-early-warning', 1, 'tenant', NULL, "
                    "'tenant:protected-faction:9', 'watchlist_all', 9, 'Aegis Shield', 0, 0, 1, :now, :now)"
                ),
                {"id": package_id, "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_rule(id, owner_scope, owner_user_id, name, target_scope, "
                    "target_system, condition_type, threshold_pp, window_days, severity, personal_discord, "
                    "tenant_discord, enabled, created_by, created_at, updated_at, package_id, template_id, "
                    "template_version, template_item_key, condition_json, effective_from) VALUES ("
                    ":id, 'tenant', NULL, 'Protected loss', 'watchlist_all', NULL, 'tenant_faction_loss', "
                    "3, 1, 'warning', 0, 0, 1, 1, :now, :now, :package_id, "
                    "'bgs-protected-faction-early-warning', 1, 'protected-influence-loss', :condition, :now)"
                ),
                {
                    "id": rule_id,
                    "package_id": package_id,
                    "condition": json.dumps(condition),
                    "now": now,
                },
            )
        engine.dispose()
        with self.eddn_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO eddn_faction(system_name, name) "
                    "VALUES ('Test System', 'Aegis Shield')"
                )
            )

        def add_protected_snapshot(ticktime, influence):
            payload = {
                "SystemFaction": {"Name": "Controller"},
                "Factions": [
                    {"Name": "Controller", "Influence": 0.50},
                    {"Name": "Test Faction", "Influence": 0.20},
                    {"Name": "Aegis Shield", "Influence": influence},
                    {"Name": "Rival", "Influence": 0.10},
                ],
                "Conflicts": [],
            }
            with self.snapshot_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO system_tick_snapshot(ticktime, system_name, payload_json, is_settled) "
                        "VALUES (:tick, 'Test System', :payload, 1)"
                    ),
                    {"tick": ticktime, "payload": json.dumps(payload)},
                )

        tenant = {
            "name": "Test",
            "db_uri": self.tenant_uri,
            "faction_name": "Test Faction",
            "discord_webhooks": {},
        }
        add_protected_snapshot("2026-08-28T12:00:00Z", 0.40)
        add_protected_snapshot("2026-08-29T12:00:00Z", 0.36)
        self.assertEqual(
            _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)[
                "alerts"
            ],
            0,
        )
        add_protected_snapshot("2026-08-30T12:00:00Z", 0.30)
        self.assertEqual(
            _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)[
                "alerts"
            ],
            1,
        )
        engine = create_engine(self.tenant_uri)
        with engine.begin() as conn:
            facts = json.loads(
                conn.execute(
                    text("SELECT facts_json FROM dashboard_bgs_alert")
                ).scalar_one()
            )
            self.assertEqual(facts["monitored_faction"], "Aegis Shield")
            conn.execute(
                text("UPDATE protected_faction SET protected = 0 WHERE id = 9")
            )
        paused = _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)
        self.assertEqual(paused["resolved"], 1)
        engine.dispose()

    def test_multiple_gap_events_create_and_resolve_independent_alerts(self):
        engine = create_engine(self.tenant_uri)
        now = utc_now()
        package_id = str(uuid.uuid4())
        rule_id = str(uuid.uuid4())
        condition = {"type": "tenant_faction_gap", "threshold_pp": 2}
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM dashboard_bgs_rule"))
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_rule_package(id, template_id, template_version, owner_scope, "
                    "owner_user_id, owner_key, target_scope, personal_discord, tenant_discord, created_by, "
                    "created_at, updated_at) VALUES (:id, 'bgs-tenant-faction-early-warning', 1, 'personal', "
                    "1, 'user:1', 'watchlist_all', 0, 0, 1, :now, :now)"
                ),
                {"id": package_id, "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_rule(id, owner_scope, owner_user_id, name, target_scope, "
                    "target_system, condition_type, threshold_pp, window_days, severity, personal_discord, "
                    "tenant_discord, enabled, created_by, created_at, updated_at, package_id, template_id, "
                    "template_version, template_item_key, condition_json, effective_from) VALUES ("
                    ":id, 'personal', 1, 'Tenant gap', 'watchlist_all', NULL, 'tenant_faction_gap', "
                    "2, 1, 'warning', 0, 0, 1, 1, :now, :now, :package_id, "
                    "'bgs-tenant-faction-early-warning', 1, 'tenant-influence-gap', :condition, :now)"
                ),
                {
                    "id": rule_id,
                    "package_id": package_id,
                    "condition": json.dumps(condition),
                    "now": now,
                },
            )
        engine.dispose()

        def add_gap_snapshot(ticktime, alpha, beta):
            payload = {
                "SystemFaction": {"Name": "Controller"},
                "Factions": [
                    {"Name": "Controller", "Influence": 0.50},
                    {"Name": "Test Faction", "Influence": 0.10},
                    {"Name": "Alpha", "Influence": alpha},
                    {"Name": "Beta", "Influence": beta},
                ],
                "Conflicts": [],
            }
            with self.snapshot_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO system_tick_snapshot(ticktime, system_name, payload_json, is_settled) "
                        "VALUES (:tick, 'Test System', :payload, 1)"
                    ),
                    {"tick": ticktime, "payload": json.dumps(payload)},
                )

        tenant = {
            "name": "Test",
            "db_uri": self.tenant_uri,
            "faction_name": "Test Faction",
            "discord_webhooks": {},
        }
        add_gap_snapshot("2026-08-27T12:00:00Z", 0.20, 0.30)
        add_gap_snapshot("2026-08-28T12:00:00Z", 0.20, 0.30)
        baseline = _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)
        self.assertEqual(baseline["alerts"], 0)
        add_gap_snapshot("2026-08-29T12:00:00Z", 0.11, 0.12)
        fired = _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)
        self.assertEqual(fired["alerts"], 2)
        add_gap_snapshot("2026-08-30T12:00:00Z", 0.11, 0.20)
        resolved = _evaluate_tenant(tenant, self.snapshot_engine, self.eddn_engine)
        self.assertEqual(resolved["resolved"], 1)

        engine = create_engine(self.tenant_uri)
        with engine.connect() as conn:
            alerts = conn.execute(
                text(
                    "SELECT event_key, resolved_at FROM dashboard_bgs_alert "
                    "ORDER BY event_key"
                )
            ).all()
        engine.dispose()
        self.assertEqual([row[0] for row in alerts], ["gap:alpha", "gap:beta"])
        self.assertIsNone(alerts[0][1])
        self.assertIsNotNone(alerts[1][1])


class RuleApiPermissionTest(unittest.TestCase):
    def setUp(self):
        self.webhooks = {}
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1
                )
            """))
            conn.execute(text("INSERT INTO users(id, username, password_hash, is_admin, active) VALUES (1, 'member', 'hash', 0, 1)"))
            conn.execute(text("INSERT INTO users(id, username, password_hash, is_admin, active) VALUES (2, 'lead', 'hash', 0, 1)"))
            conn.execute(text("INSERT INTO users(id, username, password_hash, is_admin, active) VALUES (3, 'admin', 'hash', 1, 1)"))
        ensure_dashboard_schema(self.engine)
        now = utc_now()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE users SET role='leadership' WHERE id=2"))
            conn.execute(text("UPDATE users SET role='admin' WHERE id=3"))
            conn.execute(
                text(
                    "INSERT INTO protected_faction(id, name, webhook_url, description, protected) "
                    "VALUES (9, 'Aegis Shield', 'https://discord.com/api/webhooks/123/token_value', "
                    "'Protected ally', 1)"
                )
            )
            for user_id, session_id in ((1, "member-session"), (2, "lead-session"), (3, "admin-session")):
                conn.execute(
                    text("INSERT INTO dashboard_session(id, expiresAt, token, createdAt, updatedAt, userId) VALUES (:id, '2999-01-01T00:00:00+00:00', :token, :now, :now, :user_id)"),
                    {"id": session_id, "token": f"token-{user_id}", "now": now, "user_id": user_id},
                )
                conn.execute(
                    text("INSERT INTO dashboard_view_preference(id, user_id, view_key, schema_version, payload_json, created_at, updated_at) VALUES (:id, :user_id, 'bgs-system-watchlist', 2, :payload, :now, :now)"),
                    {"id": f"pref-{user_id}", "user_id": user_id, "payload": json.dumps({"systems": [{"system": "Test System"}]}), "now": now},
                )
        self.session = scoped_session(sessionmaker(bind=self.engine))
        db = SimpleNamespace(session=self.session)
        app = Flask(__name__)

        def require_api_key(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                identities = {
                    "Bearer member": {"sub": "1", "role": "member", "capabilities": ["dashboard:read", "rules:write"], "sid": "member-session"},
                    "Bearer lead": {"sub": "2", "role": "leadership", "capabilities": ["dashboard:read", "rules:write", "tenant-rules:write", "bgs-ai:run"], "sid": "lead-session"},
                    "Bearer admin": {"sub": "3", "role": "admin", "capabilities": ["dashboard:read", "protected-factions:manage"], "sid": "admin-session"},
                }
                g.dashboard_identity = identities.get(request.headers.get("authorization"))
                g.tenant = {"id": "test", "name": "Test", "faction_name": "Test Faction", "discord_webhooks": self.webhooks}
                return view(*args, **kwargs)
            return wrapped

        register_bgs_rule_routes(app, db, require_api_key, lambda current: current.commit(), app.logger)
        self.client = app.test_client()

    def tearDown(self):
        self.session.remove()
        self.engine.dispose()

    def add_manual_alert(self, scope="tenant", owner=None):
        now = utc_now()
        self.session.execute(text(
            "INSERT INTO dashboard_bgs_alert(id,rule_name,owner_scope,owner_user_id,system_key,system_name,"
            "severity,title,message,fired_ticktime,fired_at) VALUES ('manual','Guard',:scope,:owner,'sol','Sol',"
            "'warning','Guard · Sol','Example alarm',:now,:now)"
        ), {"scope": scope, "owner": owner, "now": now})
        self.session.commit()

    def test_manual_discord_permissions_missing_webhook_and_visibility(self):
        self.add_manual_alert("personal", 2)
        url = "/api/dashboard/bgs/alerts/manual/discord"
        body = {"request_id": str(uuid.uuid4())}
        self.assertEqual(self.client.post(url, headers={"authorization": "Bearer member"}, json=body).status_code, 403)
        self.assertEqual(self.client.post(url, headers={"authorization": "Bearer admin"}, json=body).status_code, 404)
        self.assertEqual(self.client.post(url, headers={"authorization": "Bearer lead"}, json=body).status_code, 409)
        self.webhooks["bgs"] = "https://discord.com/api/webhooks/123/test_token"
        self.assertEqual(self.client.post(url, headers={"authorization": "Bearer lead"}, json={}).status_code, 400)
        self.assertEqual(self.client.post(url, headers={"authorization": "Bearer lead"}, json=body).status_code, 202)
        self.assertEqual(self.session.execute(text("SELECT channel FROM dashboard_notification_delivery")).scalar_one(), "tenant_discord")

    def test_manual_discord_is_idempotent_and_allows_explicit_resend(self):
        self.add_manual_alert()
        self.webhooks["bgs"] = "https://discord.com/api/webhooks/123/test_token"
        url = "/api/dashboard/bgs/alerts/manual/discord"
        headers = {"authorization": "Bearer lead"}
        body = {"request_id": str(uuid.uuid4())}
        first = self.client.post(url, headers=headers, json=body)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.client.post(url, headers=headers, json=body).get_json(), first.get_json())
        self.client.post(url, headers=headers, json={"request_id": str(uuid.uuid4())})
        self.assertEqual(self.session.execute(text("SELECT COUNT(*) FROM dashboard_notification_delivery")).scalar_one(), 1)
        sent = utc_now()
        self.session.execute(text("UPDATE dashboard_notification_delivery SET status='delivered',delivered_at=:now"), {"now": sent})
        self.session.commit()
        self.client.post(url, headers=headers, json=body)
        self.assertEqual(self.session.execute(text("SELECT COUNT(*) FROM dashboard_notification_delivery")).scalar_one(), 1)
        self.client.post(url, headers=headers, json={"request_id": str(uuid.uuid4())})
        self.assertEqual(self.session.execute(text("SELECT COUNT(*) FROM dashboard_notification_delivery")).scalar_one(), 2)
        listed = self.client.get("/api/dashboard/bgs/alerts", headers=headers).get_json()["data"][0]
        self.assertEqual(listed["discord"]["status"], "pending")
        self.assertEqual(listed["discord"]["last_sent_at"], sent)
        self.assertIsNone(listed["read_at"])
        self.assertIsNone(listed["acknowledged_at"])

    @staticmethod
    def payload(scope):
        return {
            "name": f"{scope.title()} controller guard",
            "owner_scope": scope,
            "target_scope": "watchlist_all",
            "target_system": None,
            "condition_type": "controller_below",
            "threshold_pp": 20,
            "window_days": 1,
            "severity": "warning",
            "personal_discord": False,
            "tenant_discord": False,
            "enabled": True,
        }

    def test_members_manage_personal_rules_but_not_tenant_rules(self):
        denied = self.client.post("/api/dashboard/bgs/rules", headers={"authorization": "Bearer member"}, json=self.payload("tenant"))
        self.assertEqual(denied.status_code, 403)
        tenant_created = self.client.post("/api/dashboard/bgs/rules", headers={"authorization": "Bearer lead"}, json=self.payload("tenant"))
        self.assertEqual(tenant_created.status_code, 201)
        personal_created = self.client.post("/api/dashboard/bgs/rules", headers={"authorization": "Bearer member"}, json=self.payload("personal"))
        self.assertEqual(personal_created.status_code, 201)
        visible = self.client.get("/api/dashboard/bgs/rules", headers={"authorization": "Bearer member"})
        self.assertEqual(visible.status_code, 200)
        self.assertEqual({rule["owner_scope"] for rule in visible.get_json()["data"]}, {"personal", "tenant"})

    def test_empty_global_package_can_be_restored_without_duplicate_rules(self):
        url = "/api/dashboard/bgs/rule-templates/bgs-tenant-faction-early-warning/apply"
        headers = {"authorization": "Bearer lead"}
        original = self.client.post(url, headers=headers, json={"watchlist_scope": "global"}).get_json()["data"]
        self.session.execute(text("DELETE FROM dashboard_bgs_rule WHERE package_id=:id"), {"id": original["id"]})
        self.session.commit()
        denied = self.client.post(url, headers={"authorization": "Bearer member"}, json={"watchlist_scope": "global"})
        self.assertEqual(denied.status_code, 403)
        restored = self.client.post(url, headers=headers, json={"watchlist_scope": "global"}).get_json()
        self.assertTrue(restored["restored"])
        self.assertEqual(restored["data"]["id"], original["id"])
        self.assertEqual(len(restored["data"]["rules"]), 4)
        self.assertTrue(all(rule["owner_scope"] == "tenant" for rule in restored["data"]["rules"]))
        repeated = self.client.post(url, headers=headers, json={"watchlist_scope": "global"}).get_json()
        self.assertFalse(repeated["restored"])
        self.assertEqual({r["id"] for r in restored["data"]["rules"]}, {r["id"] for r in repeated["data"]["rules"]})

    def test_deleting_last_package_rule_removes_catalog_assignment(self):
        headers = {"authorization": "Bearer lead"}
        package = self.client.post(
            "/api/dashboard/bgs/rule-templates/bgs-tenant-faction-early-warning/apply",
            headers=headers, json={"watchlist_scope": "global"},
        ).get_json()["data"]
        for rule in package["rules"]:
            response = self.client.delete(f"/api/dashboard/bgs/rules/{rule['id']}", headers=headers)
            self.assertEqual(response.status_code, 200)
        catalog = self.client.get("/api/dashboard/bgs/rule-templates", headers=headers).get_json()["data"]
        self.assertFalse(any(p["id"] == package["id"] for t in catalog for p in t["packages"]))

    def test_bgs_ai_accepts_systems_outside_the_tenant_watchlist(self):
        source = {
            "source_ticktime": "2026-09-02T12:00:00Z",
            "spansh": {"cached_at": "2026-09-02T12:01:00Z"},
        }
        with patch("bgs_rules._build_ai_source", return_value=source) as build_source, patch(
            "bgs_rules._call_openai",
            return_value=({"summary": "Global system analyzed."}, "test-model"),
        ):
            response = self.client.post(
                "/api/dashboard/bgs/ai-reports/analyze",
                headers={"authorization": "Bearer lead"},
                json={"system_name": "Global System", "report_type": "risk"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["data"]["system_name"], "Global System")
        build_source.assert_called_once_with(
            "Global System",
            "risk",
            "Test Faction",
        )

    def test_catalog_application_is_atomic_scoped_and_idempotent(self):
        catalog = self.client.get(
            "/api/dashboard/bgs/rule-templates",
            headers={"authorization": "Bearer member"},
        )
        self.assertEqual(catalog.status_code, 200)
        template = next(
            item
            for item in catalog.get_json()["data"]
            if item["target_kind"] == "watchlist"
        )
        self.assertEqual(template["name"], "Tenant Faction Early Warning")
        self.assertEqual(len(template["items"]), 4)

        applied = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer member"},
            json={"watchlist_scope": "personal", "discord": True},
        )
        self.assertEqual(applied.status_code, 201)
        self.assertEqual(len(applied.get_json()["data"]["rules"]), 4)
        self.assertFalse(applied.get_json()["discord_enabled"])

        duplicate = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer member"},
            json={"watchlist_scope": "personal"},
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.get_json()["already_applied"])

        denied = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer member"},
            json={"watchlist_scope": "global"},
        )
        self.assertEqual(denied.status_code, 403)

        global_applied = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer lead"},
            json={"watchlist_scope": "global"},
        )
        self.assertEqual(global_applied.status_code, 201)
        self.assertEqual(global_applied.get_json()["data"]["owner_scope"], "tenant")

    def test_personal_webhook_can_be_saved_displayed_tested_and_removed(self):
        webhook = "https://discord.com/api/webhooks/123456/personal_token"
        headers = {"authorization": "Bearer member"}
        with patch.dict(
            os.environ,
            {"VALK_WEBHOOK_ENCRYPTION_KEY": "test-personal-webhook-key"},
        ):
            initial = self.client.get(
                "/api/account/discord-webhook",
                headers=headers,
            )
            self.assertEqual(initial.status_code, 200)
            self.assertIsNone(initial.get_json()["webhook_url"])

            saved = self.client.put(
                "/api/account/discord-webhook",
                headers=headers,
                json={"webhook_url": webhook},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.get_json()["webhook_url"], webhook)

            ciphertext = self.session.execute(
                text(
                    "SELECT discord_webhook_ciphertext FROM users WHERE id = 1"
                )
            ).scalar_one()
            self.assertNotIn("personal_token", ciphertext)

            displayed = self.client.get(
                "/api/account/discord-webhook",
                headers=headers,
            )
            self.assertTrue(displayed.get_json()["configured"])
            self.assertEqual(displayed.get_json()["webhook_url"], webhook)

            with patch("bgs_rules.requests.post") as post:
                post.return_value = SimpleNamespace(status_code=204)
                tested = self.client.post(
                    "/api/account/discord-webhook/test",
                    headers=headers,
                )
            self.assertEqual(tested.status_code, 200)
            self.assertEqual(post.call_args.args[0], webhook)
            self.assertEqual(
                post.call_args.kwargs["json"]["allowed_mentions"],
                {"parse": []},
            )
            self.assertFalse(post.call_args.kwargs["allow_redirects"])

            removed = self.client.delete(
                "/api/account/discord-webhook",
                headers=headers,
            )
            self.assertEqual(removed.status_code, 200)
            self.assertIsNone(removed.get_json()["webhook_url"])

    def test_personal_webhook_save_reports_missing_encryption_key(self):
        with patch.dict(os.environ, {"VALK_WEBHOOK_ENCRYPTION_KEY": ""}):
            response = self.client.put(
                "/api/account/discord-webhook",
                headers={"authorization": "Bearer member"},
                json={
                    "webhook_url": "https://discord.com/api/webhooks/123456/token"
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "WEBHOOK_ENCRYPTION_UNAVAILABLE",
        )

    def test_template_update_is_versioned_and_package_sync_is_explicit(self):
        template = next(item for item in self.client.get(
            "/api/dashboard/bgs/rule-templates",
            headers={"authorization": "Bearer lead"},
        ).get_json()["data"] if item["target_kind"] == "watchlist")
        applied = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer member"},
            json={"watchlist_scope": "personal", "discord": False},
        ).get_json()["data"]
        items = template["items"]
        items[0]["condition"]["threshold_pp"] = 4
        updated_response = self.client.patch(
            f"/api/dashboard/bgs/rule-templates/{template['id']}",
            headers={"authorization": "Bearer lead"},
            json={
                "name": template["name"],
                "description": template["description"],
                "default_discord": template["default_discord"],
                "items": items,
            },
        )
        self.assertEqual(updated_response.status_code, 200)
        self.assertEqual(updated_response.get_json()["data"]["version"], 2)

        catalog = next(item for item in self.client.get(
            "/api/dashboard/bgs/rule-templates",
            headers={"authorization": "Bearer member"},
        ).get_json()["data"] if item["target_kind"] == "watchlist")
        self.assertEqual(catalog["version"], 2)
        self.assertEqual(catalog["packages"][0]["template_version"], 1)
        synced = self.client.post(
            f"/api/dashboard/bgs/rule-packages/{applied['id']}/sync",
            headers={"authorization": "Bearer member"},
        )
        self.assertEqual(synced.status_code, 200)
        self.assertEqual(synced.get_json()["data"]["template_version"], 2)
        loss_rule = next(
            rule
            for rule in synced.get_json()["data"]["rules"]
            if rule["template_item_key"] == "tenant-influence-loss"
        )
        self.assertEqual(loss_rule["condition"]["threshold_pp"], 4)

        archived = self.client.patch(
            f"/api/dashboard/bgs/rule-templates/{template['id']}",
            headers={"authorization": "Bearer lead"},
            json={"archived": True},
        )
        self.assertEqual(archived.status_code, 200)
        hidden = self.client.get(
            "/api/dashboard/bgs/rule-templates",
            headers={"authorization": "Bearer member"},
        )
        self.assertEqual(
            [item["target_kind"] for item in hidden.get_json()["data"]],
            ["protected_faction"],
        )

    def test_protected_catalog_requires_leadership_and_targets_one_faction(self):
        catalog = self.client.get(
            "/api/dashboard/bgs/rule-templates",
            headers={"authorization": "Bearer lead"},
        ).get_json()
        template = next(
            item
            for item in catalog["data"]
            if item["target_kind"] == "protected_faction"
        )
        self.assertTrue(catalog["can_apply_protected"])
        self.assertNotIn("webhook_url", catalog["protected_factions"][0])
        self.assertTrue(catalog["protected_factions"][0]["webhook_configured"])

        denied = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer member"},
            json={"watchlist_scope": "protected", "protected_faction_id": 9},
        )
        self.assertEqual(denied.status_code, 403)
        applied = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer lead"},
            json={
                "watchlist_scope": "protected",
                "protected_faction_id": 9,
                "discord": True,
            },
        )
        self.assertEqual(applied.status_code, 201)
        package = applied.get_json()["data"]
        self.assertEqual(package["watchlist_scope"], "protected")
        self.assertEqual(package["protected_faction_id"], 9)
        self.assertEqual(package["protected_faction"]["name"], "Aegis Shield")
        self.assertTrue(package["tenant_discord"])

        duplicate = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer lead"},
            json={"watchlist_scope": "protected", "protected_faction_id": 9},
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.get_json()["already_applied"])

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO protected_faction(id, name, webhook_url, description, protected) "
                    "VALUES (10, 'Beacon Guard', NULL, 'Second protected ally', 1)"
                )
            )
        second = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer lead"},
            json={"watchlist_scope": "protected", "protected_faction_id": 10},
        )
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(second.get_json()["data"]["id"], package["id"])
        self.assertEqual(second.get_json()["data"]["protected_faction_id"], 10)
        self.assertFalse(second.get_json()["discord_enabled"])

    def test_admin_protected_faction_crud_masks_webhooks_and_checks_permissions(self):
        api_key_only = self.client.get("/api/admin/protected-factions")
        self.assertEqual(api_key_only.status_code, 401)
        leadership = self.client.get(
            "/api/admin/protected-factions",
            headers={"authorization": "Bearer lead"},
        )
        self.assertEqual(leadership.status_code, 403)

        listed = self.client.get(
            "/api/admin/protected-factions",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn("webhook_url", listed.get_json()["data"][0])
        self.assertTrue(listed.get_json()["data"][0]["webhook_configured"])

        invalid = self.client.post(
            "/api/admin/protected-factions",
            headers={"authorization": "Bearer admin"},
            json={
                "name": "Unsafe Ally",
                "webhook_url": "https://discord.com.evil.invalid/api/webhooks/1/token",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        duplicate = self.client.post(
            "/api/admin/protected-factions",
            headers={"authorization": "Bearer admin"},
            json={"name": "aegis shield"},
        )
        self.assertEqual(duplicate.status_code, 409)

        secret = "https://discord.com/api/webhooks/987654/private_token"
        created = self.client.post(
            "/api/admin/protected-factions",
            headers={"authorization": "Bearer admin"},
            json={
                "name": "Beacon Guard",
                "description": "Protected ally",
                "protected": True,
                "webhook_url": secret,
            },
        )
        self.assertEqual(created.status_code, 201)
        payload = created.get_json()["data"]
        self.assertNotIn("webhook_url", payload)
        self.assertTrue(payload["webhook_configured"])

        with patch("bgs_rules.requests.post") as post:
            post.return_value = SimpleNamespace(status_code=204)
            tested = self.client.post(
                f"/api/admin/protected-factions/{payload['id']}/webhook-test",
                headers={"authorization": "Bearer admin"},
            )
        self.assertEqual(tested.status_code, 200)
        self.assertEqual(post.call_args.args[0], secret)
        self.assertEqual(post.call_args.kwargs["json"]["allowed_mentions"], {"parse": []})
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        with patch(
            "bgs_rules.requests.post",
            side_effect=requests.RequestException(f"delivery failed for {secret}"),
        ):
            failed_test = self.client.post(
                f"/api/admin/protected-factions/{payload['id']}/webhook-test",
                headers={"authorization": "Bearer admin"},
            )
        self.assertEqual(failed_test.status_code, 502)
        self.assertNotIn("private_token", failed_test.get_data(as_text=True))

        preserved = self.client.patch(
            f"/api/admin/protected-factions/{payload['id']}",
            headers={"authorization": "Bearer admin"},
            json={"description": "Updated"},
        )
        self.assertEqual(preserved.status_code, 200)
        self.assertTrue(preserved.get_json()["data"]["webhook_configured"])

        removed = self.client.patch(
            f"/api/admin/protected-factions/{payload['id']}",
            headers={"authorization": "Bearer admin"},
            json={"webhook_url": None},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(removed.get_json()["data"]["webhook_configured"])
        deleted = self.client.delete(
            f"/api/admin/protected-factions/{payload['id']}",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(deleted.status_code, 200)

        audit_payload = " ".join(
            str(row[0] or "")
            for row in self.session.execute(
                text("SELECT metadata_json FROM dashboard_audit_event")
            ).all()
        )
        self.assertNotIn("private_token", audit_payload)

    def test_deactivation_pauses_protected_rules_and_resolves_alerts(self):
        catalog = self.client.get(
            "/api/dashboard/bgs/rule-templates",
            headers={"authorization": "Bearer lead"},
        ).get_json()
        template = next(
            item
            for item in catalog["data"]
            if item["target_kind"] == "protected_faction"
        )
        package = self.client.post(
            f"/api/dashboard/bgs/rule-templates/{template['id']}/apply",
            headers={"authorization": "Bearer lead"},
            json={"watchlist_scope": "protected", "protected_faction_id": 9},
        ).get_json()["data"]
        rule_id = package["rules"][0]["id"]
        now = utc_now()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_rule_state(rule_id, system_key, system_name, "
                    "condition_active, status, observations_json, updated_at) "
                    "VALUES (:rule_id, 'test system', 'Test System', 1, 'active', '{}', :now)"
                ),
                {"rule_id": rule_id, "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_alert(id, rule_id, rule_name, owner_scope, "
                    "system_key, system_name, severity, title, message, facts_json, event_key, "
                    "fired_ticktime, fired_at) VALUES ('protected-alert', :rule_id, 'Guard', "
                    "'tenant', 'test system', 'Test System', 'warning', 'Alert', 'Message', "
                    "'{}', 'condition', '2026-08-30T00:00:00Z', :now)"
                ),
                {"rule_id": rule_id, "now": now},
            )

        deactivated = self.client.patch(
            "/api/admin/protected-factions/9",
            headers={"authorization": "Bearer admin"},
            json={"protected": False},
        )
        self.assertEqual(deactivated.status_code, 200)
        self.assertEqual(deactivated.get_json()["states_paused"], 1)
        self.assertEqual(deactivated.get_json()["alerts_resolved"], 1)
        state = self.session.execute(
            text(
                "SELECT status, condition_active FROM dashboard_bgs_rule_state "
                "WHERE rule_id = :rule_id"
            ),
            {"rule_id": rule_id},
        ).one()
        self.assertEqual(state[0], "paused_target_missing")
        self.assertIsNone(state[1])
        self.assertIsNotNone(
            self.session.execute(
                text("SELECT resolved_at FROM dashboard_bgs_alert WHERE id='protected-alert'")
            ).scalar_one()
        )

        reactivated = self.client.patch(
            "/api/admin/protected-factions/9",
            headers={"authorization": "Bearer admin"},
            json={"protected": True},
        )
        self.assertEqual(reactivated.status_code, 200)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE dashboard_bgs_rule_state SET status='active', condition_active=1 "
                    "WHERE rule_id=:rule_id"
                ),
                {"rule_id": rule_id},
            )
            conn.execute(
                text(
                    "INSERT INTO dashboard_bgs_alert(id, rule_id, rule_name, owner_scope, "
                    "system_key, system_name, severity, title, message, facts_json, event_key, "
                    "fired_ticktime, fired_at) VALUES ('renamed-alert', :rule_id, 'Guard', "
                    "'tenant', 'test system', 'Test System', 'warning', 'Alert', 'Message', "
                    "'{}', 'condition', '2026-08-31T00:00:00Z', :now)"
                ),
                {"rule_id": rule_id, "now": now},
            )
        renamed = self.client.patch(
            "/api/admin/protected-factions/9",
            headers={"authorization": "Bearer admin"},
            json={"name": "Aegis Shield Renamed"},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.get_json()["states_paused"], 1)
        self.assertEqual(renamed.get_json()["alerts_resolved"], 1)

    def test_admin_protected_faction_candidates_use_eddn_partial_search(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            database_path = Path(temp_directory) / "eddn.sqlite"
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text("CREATE TABLE eddn_faction(name TEXT, system_name TEXT)")
                )
                connection.execute(
                    text(
                        "INSERT INTO eddn_faction(name, system_name) VALUES "
                        "('Aegis Shield', 'One'), ('Aegis Vanguard', 'Two'), "
                        "('Beacon Guard', 'Three'), "
                        "('East India Company', 'Four')"
                    )
                )
            engine.dispose()
            with patch.dict(
                os.environ,
                {"EDDN_DATABASE": f"sqlite:///{database_path.as_posix()}"},
            ):
                response = self.client.get(
                    "/api/admin/protected-factions/candidates?q=Ae",
                    headers={"authorization": "Bearer admin"},
                )
                infix_response = self.client.get(
                    "/api/admin/protected-factions/candidates?q=Shield",
                    headers={"authorization": "Bearer admin"},
                )
                east_response = self.client.get(
                    "/api/admin/protected-factions/candidates?q=Eas",
                    headers={"authorization": "Bearer admin"},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["name"] for item in response.get_json()["data"]],
            ["Aegis Shield", "Aegis Vanguard"],
        )
        self.assertEqual(infix_response.status_code, 200)
        self.assertEqual(
            [item["name"] for item in infix_response.get_json()["data"]],
            ["Aegis Shield"],
        )
        self.assertEqual(east_response.status_code, 200)
        self.assertEqual(
            [item["name"] for item in east_response.get_json()["data"]],
            ["East India Company"],
        )


if __name__ == "__main__":
    unittest.main()
