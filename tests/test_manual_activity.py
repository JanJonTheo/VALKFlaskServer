import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import app as flask_app
from models import Activity, Cmdr, Event, Faction, ManualActivitySubmission, RedeemVoucherEvent, SyntheticCZ, System, db


class _WebhookResponse:
    status_code = 200

    def json(self):
        return {"id": "discord-message-1"}


class _WebhookResponse404:
    status_code = 404

    text = "not found"


class _WebhookResponseNew:
    status_code = 200

    def json(self):
        return {"id": "discord-message-2"}


class _WebhookDeleteResponse:
    status_code = 204


class ManualActivityEndpointSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmpdir.name) / "tenant.sqlite"
        self.snapshot_db_path = Path(self.tmpdir.name) / "snapshot.sqlite"
        self.env_patcher = patch.dict("os.environ", {"SNAPSHOT_DB_URI": f"sqlite:///{self.snapshot_db_path.as_posix()}"})
        self.env_patcher.start()
        self._create_snapshot_db()
        self.db_uri = f"sqlite:///{self.db_path.as_posix()}"
        self.engine = create_engine(self.db_uri, connect_args={"check_same_thread": False})
        db.Model.metadata.create_all(bind=self.engine)
        db.session = scoped_session(sessionmaker(bind=self.engine))
        db.session.add(Event(
            event="RedeemVoucher",
            timestamp="2026-05-27T10:00:00Z",
            tickid="tick-1",
            ticktime="2026-05-27T09:00:00Z",
            cmdr="Seed",
            starsystem="Synuefe OX-Y b47-0",
            systemaddress=123456789,
            raw_json="{}",
        ))
        db.session.add(Event(
            event="MissionCompleted",
            timestamp="2026-05-27T10:01:00Z",
            tickid="tick-1",
            ticktime="2026-05-27T09:00:00Z",
            cmdr="JanJonTeo",
            starsystem="Synuefe PX-Y b47-0",
            systemaddress=123456790,
            raw_json="{}",
        ))
        activity = Activity(
            tickid="tick-1",
            ticktime="2026-05-27T09:00:00Z",
            timestamp="2026-05-27T10:02:00Z",
            cmdr="JanJonTeo",
        )
        db.session.add(activity)
        db.session.flush()
        system = System(name="Synuefe OX-Y b47-0", address=123456789, activity_id=activity.id)
        db.session.add(system)
        db.session.flush()
        db.session.add(Faction(name="Valkyries of Trade", state="None", system_id=system.id))
        db.session.add(Cmdr(name="JanJonTeo"))
        db.session.commit()
        flask_app.TENANTS[:] = [{
            "name": "Test Tenant",
            "api_key": "test-key",
            "api_version": flask_app.API_VERSION,
            "db_uri": self.db_uri,
            "discord_webhooks": {
                "manual_activity": "https://discord.invalid/webhook",
            },
            "manual_activity_webhook": {
                "enabled": True,
                "username": "BGS-Tally Manual",
                "avatar_url": "",
                "timeout_seconds": 10,
            },
        }]
        self.client = flask_app.app.test_client()

    def _create_snapshot_db(self):
        conn = sqlite3.connect(self.snapshot_db_path)
        conn.execute("CREATE TABLE eddn_system_info (system_name TEXT, system_address INTEGER)")
        conn.execute("CREATE TABLE eddn_faction (system_name TEXT, name TEXT, state TEXT)")
        conn.execute("CREATE TABLE system_tick_snapshot (system_name TEXT, system_address TEXT, payload_json TEXT, updated_at TEXT)")
        conn.execute(
            "INSERT INTO eddn_system_info (system_name, system_address) VALUES (?, ?)",
            ("Synuefe EDDN Prime", 987654321),
        )
        conn.execute(
            "INSERT INTO eddn_faction (system_name, name, state) VALUES (?, ?, ?)",
            ("Synuefe EDDN Prime", "Valkyries of Trade", "Boom"),
        )
        conn.execute(
            "INSERT INTO eddn_faction (system_name, name, state) VALUES (?, ?, ?)",
            ("Synuefe EDDN Prime", "Prime Local Services", "None"),
        )
        conn.execute(
            "INSERT INTO eddn_faction (system_name, name, state) VALUES (?, ?, ?)",
            ("Other System", "Valkyries Elsewhere", "War"),
        )
        conn.execute(
            "INSERT INTO system_tick_snapshot (system_name, system_address, payload_json, updated_at) VALUES (?, ?, ?, ?)",
            (
                "Kachian",
                "5370319489848",
                json.dumps({
                    "StarSystem": "Kachian",
                    "SystemAddress": 5370319489848,
                    "Factions": [
                        {"Name": "Union of Kachian Progressive Party", "FactionState": "None"},
                        {"Name": "Order of Kachian", "FactionState": "None"},
                        {"Name": "East India Company", "FactionState": "Expansion"},
                    ],
                }),
                "2026-05-27T10:00:00Z",
            ),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        db.session.remove()
        self.engine.dispose()
        self.env_patcher.stop()
        self.tmpdir.cleanup()

    def _payload(self):
        return {
            "submission_id": "discord:123:456:987",
            "source": "discord_modal",
            "discord": {
                "guild_id": "123",
                "channel_id": "456",
                "user_id": "789",
                "message_id": None,
                "interaction_id": "987",
            },
            "cmdr": "JanJonTeo",
            "client_timestamp": "2026-05-27T13:49:00Z",
            "system": {"name": "Synuefe EDDN Prime", "address": 987654321},
            "faction": {"name": "Valkyries of Trade", "state": "Boom"},
            "activity": {
                "type": "bounty_voucher",
                "amount": 2500000,
                "count": None,
                "influence": None,
                "cz_type": None,
                "settlement": None,
            },
            "note": "Manual entry from Discord modal",
        }

    def _headers(self):
        return {"apikey": "test-key", "apiversion": flask_app.API_VERSION}

    def _delete_payload(self, user_id="789"):
        return {
            "source": "discord_modal",
            "discord": {
                "guild_id": "123",
                "channel_id": "456",
                "user_id": user_id,
                "interaction_id": "undo-1",
            },
        }

    def _manual_submission(self, **overrides):
        values = {
            "submission_id": "manual:list:default",
            "source": "discord_modal",
            "discord_guild_id": "123",
            "discord_channel_id": "456",
            "discord_user_id": "789",
            "discord_interaction_id": "list-1",
            "cmdr": "JanJonTeo",
            "tickid": "tick-1",
            "ticktime": "2026-05-27T09:00:00Z",
            "captured_at": "2026-05-27T13:49:00Z",
            "client_timestamp": "2026-05-27T13:49:00Z",
            "system_name": "Synuefe EDDN Prime",
            "system_address": 987654321,
            "faction_name": "Valkyries of Trade",
            "faction_state": "Boom",
            "activity_type": "space_cz",
            "amount": 123,
            "count": 1,
            "influence": None,
            "cz_type": "high",
            "settlement": None,
            "activity_id": None,
            "event_ids_json": json.dumps([123]),
            "payload_hash": "hash-list-default",
            "payload_json": "{}",
            "status": "saved",
            "created_at": "2026-05-27T13:49:00Z",
            "webhook_status": "posted",
        }
        values.update(overrides)
        submission = ManualActivitySubmission(**values)
        db.session.add(submission)
        db.session.commit()
        return submission

    def test_rejects_tick_fields(self):
        payload = self._payload()
        payload["tickid"] = "client-forced"
        response = self.client.post("/api/manual/activity", json=payload, headers=self._headers())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Tick fields are not accepted. Current tick is resolved server-side.")

    def test_lookup_endpoints_are_read_only_autocomplete_lists(self):
        headers = self._headers()

        systems = self.client.get("/api/manual/lookup/systems?q=syn&limit=99", headers=headers)
        self.assertEqual(systems.status_code, 200)
        systems_json = systems.get_json()
        self.assertLessEqual(len(systems_json), 25)
        self.assertIn({"name": "Synuefe EDDN Prime", "address": 987654321}, systems_json)
        self.assertNotIn("tickid", systems_json[0])

        factions = self.client.get(
            "/api/manual/lookup/factions?system=Synuefe%20EDDN%20Prime&q=valk&limit=25",
            headers=headers,
        )
        self.assertEqual(factions.status_code, 200)
        self.assertIn({"name": "Valkyries of Trade", "state": "Boom"}, factions.get_json())
        self.assertNotIn({"name": "Valkyries Elsewhere", "state": "War"}, factions.get_json())
        self.assertNotIn("ticktime", factions.get_json()[0])

        all_system_factions = self.client.get(
            "/api/manual/lookup/factions?system=Synuefe%20EDDN%20Prime&limit=25",
            headers=headers,
        )
        self.assertEqual(all_system_factions.status_code, 200)
        self.assertIn({"name": "Prime Local Services", "state": "None"}, all_system_factions.get_json())
        self.assertIn({"name": "Valkyries of Trade", "state": "Boom"}, all_system_factions.get_json())
        self.assertNotIn({"name": "Valkyries Elsewhere", "state": "War"}, all_system_factions.get_json())

        missing_system = self.client.get("/api/manual/lookup/factions?q=valk&limit=25", headers=headers)
        self.assertEqual(missing_system.status_code, 200)
        self.assertEqual(missing_system.get_json(), [])

        snapshot_factions = self.client.get(
            "/api/manual/lookup/factions?system=Kachian&limit=25",
            headers=headers,
        )
        self.assertEqual(snapshot_factions.status_code, 200)
        self.assertIn({"name": "East India Company", "state": "Expansion"}, snapshot_factions.get_json())
        self.assertIn({"name": "Order of Kachian", "state": "None"}, snapshot_factions.get_json())

        snapshot_filtered = self.client.get(
            "/api/manual/lookup/factions?system=Kachian&q=east&limit=25",
            headers=headers,
        )
        self.assertEqual(snapshot_filtered.status_code, 200)
        self.assertEqual(snapshot_filtered.get_json(), [{"name": "East India Company", "state": "Expansion"}])

        cmdrs = self.client.get("/api/manual/lookup/cmdrs?q=jan&limit=25", headers=headers)
        self.assertEqual(cmdrs.status_code, 200)
        self.assertEqual(cmdrs.get_json()[0], {"name": "JanJonTeo"})
        self.assertEqual(db.session.query(ManualActivitySubmission).count(), 0)

    def test_lookup_short_query_returns_empty_list(self):
        response = self.client.get("/api/manual/lookup/systems?q=s&limit=25", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])

    def test_manual_activity_rejects_faction_not_present_in_system(self):
        payload = self._payload()
        payload["submission_id"] = "discord:123:456:invalid-faction"
        payload["faction"]["name"] = "Not Present Faction"
        response = self.client.post("/api/manual/activity", json=payload, headers=self._headers())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Faction is not present in the selected system.")
        self.assertEqual(db.session.query(ManualActivitySubmission).count(), 0)
        self.assertEqual(db.session.query(Faction).filter_by(name="Not Present Faction").count(), 0)

    def test_undo_requires_valid_auth(self):
        response = self.client.post("/api/manual/activity/undo", json=self._delete_payload())
        self.assertEqual(response.status_code, 401)

    def test_undo_requires_current_tick(self):
        db.session.query(Event).delete()
        db.session.query(Activity).delete()
        db.session.commit()

        response = self.client.post("/api/manual/activity/undo", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "No current tick available. Submit at least one current BGS-Tally event first.")

    @patch("requests.delete", return_value=_WebhookDeleteResponse())
    @patch("requests.post", return_value=_WebhookResponse())
    def test_undo_single_activity_marks_deleted_removes_events_and_reverses_aggregate(self, post_mock, delete_mock):
        saved = self.client.post("/api/manual/activity", json=self._payload(), headers=self._headers())
        self.assertEqual(saved.status_code, 200)
        event_ids = saved.get_json()["event_ids"]
        self.assertEqual(db.session.query(Event).filter(Event.id.in_(event_ids)).count(), 1)
        self.assertEqual(db.session.query(RedeemVoucherEvent).count(), 1)

        response = self.client.post("/api/manual/activity/undo", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "deleted")
        self.assertEqual(data["operation"], "undo")
        self.assertEqual(data["deleted_count"], 1)
        self.assertEqual(data["deleted_activities"][0]["submission_id"], "discord:123:456:987")
        self.assertEqual(data["webhook_status"], "deleted")
        self.assertEqual(delete_mock.call_count, 1)

        submission = db.session.query(ManualActivitySubmission).filter_by(submission_id="discord:123:456:987").one()
        self.assertEqual(submission.status, "deleted")
        audit = json.loads(submission.error_message)
        self.assertEqual(audit["operation"], "undo")
        self.assertEqual(audit["actor_discord_user_id"], "789")
        self.assertEqual(db.session.query(Event).filter(Event.id.in_(event_ids)).count(), 0)
        self.assertEqual(db.session.query(RedeemVoucherEvent).count(), 0)
        self.assertEqual(sum((faction.bvs or 0) for faction in db.session.query(Faction).filter_by(name="Valkyries of Trade").all()), 0)
        self.assertEqual(post_mock.call_count, 1)

    @patch("requests.delete", return_value=_WebhookDeleteResponse())
    @patch("requests.patch", return_value=_WebhookResponse())
    @patch("requests.post", return_value=_WebhookResponse())
    def test_undo_cz_activity_also_removes_matching_combat_bond_submission(self, post_mock, patch_mock, delete_mock):
        cz_payload = self._payload()
        cz_payload["submission_id"] = "discord:123:456:cz"
        cz_payload["discord"]["interaction_id"] = "cz"
        cz_payload["activity"]["type"] = "space_cz"
        cz_payload["activity"]["amount"] = None
        cz_payload["activity"]["count"] = 1
        cz_payload["activity"]["cz_type"] = "high"
        cz_saved = self.client.post("/api/manual/activity", json=cz_payload, headers=self._headers())
        self.assertEqual(cz_saved.status_code, 200)

        bond_payload = self._payload()
        bond_payload["submission_id"] = "discord:123:456:cz:combat_bond"
        bond_payload["discord"]["interaction_id"] = "cz-bond"
        bond_payload["activity"]["type"] = "combat_bond"
        bond_payload["activity"]["amount"] = 750000
        bond_saved = self.client.post("/api/manual/activity", json=bond_payload, headers=self._headers())
        self.assertEqual(bond_saved.status_code, 200)

        response = self.client.post("/api/manual/activity/undo", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["deleted_count"], 2)
        self.assertEqual(
            {item["submission_id"] for item in data["deleted_activities"]},
            {"discord:123:456:cz", "discord:123:456:cz:combat_bond"},
        )
        self.assertEqual(db.session.query(ManualActivitySubmission).filter_by(status="saved").count(), 0)
        self.assertEqual(db.session.query(SyntheticCZ).count(), 0)
        self.assertEqual(db.session.query(RedeemVoucherEvent).count(), 0)
        factions = db.session.query(Faction).filter_by(name="Valkyries of Trade").all()
        self.assertEqual(sum((faction.cbs or 0) for faction in factions), 0)
        self.assertIn({"low": 0, "medium": 0, "high": 0}, [json.loads(faction.czspace) for faction in factions if faction.czspace])
        self.assertEqual(delete_mock.call_count, 1)

    @patch("requests.patch", return_value=_WebhookResponse())
    @patch("requests.post", return_value=_WebhookResponse())
    def test_clear_ct_only_deletes_current_tick_activities_for_same_discord_user(self, post_mock, patch_mock):
        first = self.client.post("/api/manual/activity", json=self._payload(), headers=self._headers())
        self.assertEqual(first.status_code, 200)

        other_user_payload = self._payload()
        other_user_payload["submission_id"] = "discord:123:456:other-user"
        other_user_payload["discord"]["user_id"] = "999"
        other_user_payload["discord"]["interaction_id"] = "other-user"
        other_user_payload["activity"]["amount"] = 500000
        second = self.client.post("/api/manual/activity", json=other_user_payload, headers=self._headers())
        self.assertEqual(second.status_code, 200)

        response = self.client.post("/api/manual/activity/clear-ct", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "deleted")
        self.assertEqual(data["operation"], "clear_ct")
        self.assertEqual(data["deleted_count"], 1)
        self.assertEqual(data["webhook_status"], "posted")

        deleted = db.session.query(ManualActivitySubmission).filter_by(submission_id="discord:123:456:987").one()
        kept = db.session.query(ManualActivitySubmission).filter_by(submission_id="discord:123:456:other-user").one()
        self.assertEqual(deleted.status, "deleted")
        self.assertEqual(kept.status, "saved")
        self.assertEqual(sum((faction.bvs or 0) for faction in db.session.query(Faction).filter_by(name="Valkyries of Trade").all()), 500000)
        self.assertGreaterEqual(patch_mock.call_count, 2)

    def test_undo_no_op_returns_200_with_empty_deleted_list(self):
        response = self.client.post("/api/manual/activity/undo", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "no_op")
        self.assertEqual(data["operation"], "undo")
        self.assertEqual(data["deleted_count"], 0)
        self.assertEqual(data["deleted_activities"], [])
        self.assertEqual(data["webhook_status"], "skipped")

    def test_list_ct_requires_valid_auth(self):
        missing = self.client.post("/api/manual/activity/list-ct", json=self._delete_payload())
        self.assertEqual(missing.status_code, 401)

        invalid = self.client.post(
            "/api/manual/activity/list-ct",
            json=self._delete_payload(),
            headers={"apikey": "wrong-key", "apiversion": flask_app.API_VERSION},
        )
        self.assertEqual(invalid.status_code, 401)

    def test_list_ct_requires_current_tick(self):
        db.session.query(Event).delete()
        db.session.query(Activity).delete()
        db.session.commit()

        response = self.client.post("/api/manual/activity/list-ct", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "No current tick available. Submit at least one current BGS-Tally event first.")

    @patch("requests.post", return_value=_WebhookResponse())
    def test_list_ct_returns_current_tick_activities_for_same_discord_user(self, post_mock):
        first = self.client.post("/api/manual/activity", json=self._payload(), headers=self._headers())
        self.assertEqual(first.status_code, 200)

        other_user_payload = self._payload()
        other_user_payload["submission_id"] = "discord:123:456:other-list-user"
        other_user_payload["discord"]["user_id"] = "999"
        other_user_payload["discord"]["interaction_id"] = "other-list-user"
        other_user_payload["activity"]["amount"] = 500000
        second = self.client.post("/api/manual/activity", json=other_user_payload, headers=self._headers())
        self.assertEqual(second.status_code, 200)

        response = self.client.post("/api/manual/activity/list-ct", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["operation"], "list_ct")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["activities"][0]["submission_id"], "discord:123:456:987")
        self.assertEqual(data["activities"][0]["system"], "Synuefe EDDN Prime")
        self.assertEqual(data["activities"][0]["faction"], "Valkyries of Trade")

    def test_list_ct_filters_to_same_discord_user_current_tick_and_saved_status(self):
        self._manual_submission(submission_id="manual:list:current-user", discord_user_id="789")
        self._manual_submission(submission_id="manual:list:other-user", discord_user_id="999", payload_hash="hash-list-other-user")
        self._manual_submission(submission_id="manual:list:old-tick", tickid="tick-old", payload_hash="hash-list-old-tick")
        self._manual_submission(submission_id="manual:list:deleted", status="deleted", payload_hash="hash-list-deleted")

        response = self.client.post("/api/manual/activity/list-ct", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["operation"], "list_ct")
        self.assertEqual(data["tickid"], "tick-1")
        self.assertEqual(data["ticktime"], "2026-05-27T09:00:00Z")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["activities"], [{
            "submission_id": "manual:list:current-user",
            "cmdr": "JanJonTeo",
            "system": "Synuefe EDDN Prime",
            "faction": "Valkyries of Trade",
            "activity_type": "space_cz",
            "amount": 123,
            "count": 1,
            "influence": None,
            "cz_type": "high",
            "settlement": None,
            "event_ids": [123],
            "captured_at": "2026-05-27T13:49:00Z",
            "webhook_status": "posted",
        }])

    @patch("requests.post", return_value=_WebhookResponse())
    @patch("requests.patch", return_value=_WebhookResponse())
    @patch("requests.delete", return_value=_WebhookDeleteResponse())
    def test_list_ct_is_read_only(self, delete_mock, patch_mock, post_mock):
        saved = self.client.post("/api/manual/activity", json=self._payload(), headers=self._headers())
        self.assertEqual(saved.status_code, 200)
        event_ids = saved.get_json()["event_ids"]
        before_submission_count = db.session.query(ManualActivitySubmission).count()
        before_event_count = db.session.query(Event).count()
        before_bvs = sum((faction.bvs or 0) for faction in db.session.query(Faction).filter_by(name="Valkyries of Trade").all())
        post_calls_after_save = post_mock.call_count

        response = self.client.post("/api/manual/activity/list-ct", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 1)
        self.assertEqual(db.session.query(ManualActivitySubmission).count(), before_submission_count)
        self.assertEqual(db.session.query(Event).count(), before_event_count)
        self.assertEqual(db.session.query(Event).filter(Event.id.in_(event_ids)).count(), len(event_ids))
        self.assertEqual(sum((faction.bvs or 0) for faction in db.session.query(Faction).filter_by(name="Valkyries of Trade").all()), before_bvs)
        self.assertEqual(db.session.query(ManualActivitySubmission).filter_by(submission_id="discord:123:456:987").one().status, "saved")
        self.assertEqual(post_mock.call_count, post_calls_after_save)
        self.assertEqual(patch_mock.call_count, 0)
        self.assertEqual(delete_mock.call_count, 0)

    def test_list_ct_no_op_returns_200_with_empty_activities(self):
        response = self.client.post("/api/manual/activity/list-ct", json=self._delete_payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "no_op")
        self.assertEqual(data["operation"], "list_ct")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["activities"], [])

    @patch("requests.post", return_value=_WebhookResponse())
    def test_bounty_voucher_saved_and_duplicate_is_idempotent(self, post_mock):
        response = self.client.post("/api/manual/activity", json=self._payload(), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "saved")
        self.assertEqual(data["webhook_status"], "posted")
        self.assertEqual(data["webhook_message_id"], "discord-message-1")
        self.assertEqual(data["cmdr"], "JanJonTeo")
        self.assertEqual(post_mock.call_args.kwargs["json"]["username"], "JanJonTeo")
        self.assertEqual(post_mock.call_args.kwargs["json"]["embeds"][0]["author"]["name"], "Manual Discord Input")

        submission = db.session.query(ManualActivitySubmission).filter_by(submission_id="discord:123:456:987").one()
        self.assertEqual(submission.activity_type, "bounty_voucher")
        self.assertEqual(json.loads(submission.event_ids_json), data["event_ids"])

        faction = db.session.query(Faction).filter_by(name="Valkyries of Trade", bvs=2500000).one()
        self.assertEqual(faction.bvs, 2500000)
        redeem = db.session.query(RedeemVoucherEvent).one()
        self.assertEqual(redeem.type, "bounty")

        duplicate = self.client.post("/api/manual/activity", json=self._payload(), headers=self._headers())
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.get_json()["status"], "duplicate")
        self.assertEqual(duplicate.get_json()["webhook_status"], "skipped_duplicate")
        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(db.session.query(Faction).filter_by(name="Valkyries of Trade", bvs=2500000).one().bvs, 2500000)

    @patch("requests.patch", return_value=_WebhookResponse())
    @patch("requests.post", return_value=_WebhookResponse())
    def test_subsequent_manual_entries_edit_previous_discord_message(self, post_mock, patch_mock):
        first = self.client.post("/api/manual/activity", json=self._payload(), headers=self._headers())
        self.assertEqual(first.status_code, 200)
        first_json = first.get_json()
        self.assertEqual(first_json["webhook_status"], "posted")
        self.assertEqual(first_json["webhook_message_id"], "discord-message-1")

        second_payload = self._payload()
        second_payload["submission_id"] = "discord:123:456:988"
        second_payload["discord"]["interaction_id"] = "988"
        second_payload["activity"]["type"] = "combat_bond"
        second_payload["activity"]["amount"] = 1500000
        second = self.client.post("/api/manual/activity", json=second_payload, headers=self._headers())
        self.assertEqual(second.status_code, 200)
        second_json = second.get_json()
        self.assertEqual(second_json["webhook_status"], "posted")
        self.assertEqual(second_json["webhook_message_id"], "discord-message-1")
        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(patch_mock.call_count, 1)

        patch_payload = patch_mock.call_args.kwargs["json"]
        fields = patch_payload["embeds"][0]["fields"]
        self.assertEqual(patch_payload["username"], "JanJonTeo")
        self.assertEqual(patch_payload["embeds"][0]["author"]["name"], "Manual Discord Input")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["name"], "Synuefe EDDN Prime")
        field_values = "\n".join(field["value"] for field in fields)
        self.assertNotIn("CMDR JanJonTeo", field_values)
        self.assertIn("BVs", field_values)
        self.assertIn("CBs", field_values)

    @patch("requests.patch", return_value=_WebhookResponse404())
    @patch("requests.post", side_effect=[_WebhookResponse(), _WebhookResponseNew()])
    def test_missing_previous_discord_message_posts_new_grouped_message(self, post_mock, patch_mock):
        first = self.client.post("/api/manual/activity", json=self._payload(), headers=self._headers())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["webhook_message_id"], "discord-message-1")

        second_payload = self._payload()
        second_payload["submission_id"] = "discord:123:456:989"
        second_payload["discord"]["interaction_id"] = "989"
        second_payload["activity"]["type"] = "combat_bond"
        second_payload["activity"]["amount"] = 1500000
        second = self.client.post("/api/manual/activity", json=second_payload, headers=self._headers())
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["webhook_status"], "posted")
        self.assertEqual(second.get_json()["webhook_message_id"], "discord-message-2")
        self.assertEqual(patch_mock.call_count, 1)
        self.assertEqual(post_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
