import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker

import app as flask_app
import databases as database_manager
from manual_activity import _create_events
from models import db


class MissionEventApiTest(unittest.TestCase):
    def setUp(self):
        self.original_tenants = list(flask_app.TENANTS)
        self.original_tickids = dict(flask_app.last_known_tickid)
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmpdir.name) / "tenant.sqlite"
        self.db_uri = f"sqlite:///{self.db_path.as_posix()}"
        self.engine = create_engine(
            self.db_uri,
            connect_args={"check_same_thread": False},
        )
        db.Model.metadata.create_all(bind=self.engine)
        db.session = scoped_session(sessionmaker(bind=self.engine))
        flask_app.TENANTS[:] = [{
            "name": "Temp Tenant",
            "api_key": "temp-key",
            "api_version": flask_app.API_VERSION,
            "db_uri": self.db_uri,
            "faction_name": "East India Company",
            "discord_webhooks": {},
        }]
        flask_app.last_known_tickid["Temp Tenant"] = "123456789012345678901234"
        self.client = flask_app.app.test_client()

    def tearDown(self):
        flask_app.TENANTS[:] = self.original_tenants
        flask_app.last_known_tickid.clear()
        flask_app.last_known_tickid.update(self.original_tickids)
        try:
            db.session.remove()
        except RuntimeError:
            pass
        self.engine.dispose()
        self.tmpdir.cleanup()

    @staticmethod
    def headers():
        return {"apikey": "temp-key", "apiversion": flask_app.API_VERSION}

    @staticmethod
    def common(timestamp, event):
        return {
            "timestamp": timestamp,
            "event": event,
            "cmdr": "Probe",
            "tickid": "123456789012345678901234",
            "ticktime": "2026-08-28T20:00:00Z",
            "StarSystem": "Probe System",
            "SystemAddress": 1234567890,
        }

    def test_discovery_advertises_all_supported_mission_journal_events(self):
        response = self.client.get("/discovery")

        self.assertEqual(response.status_code, 200)
        advertised = response.get_json()["events"]
        for event_name in (
            "CargoDepot",
            "MissionAbandoned",
            "MissionAccepted",
            "MissionCompleted",
            "MissionFailed",
            "MissionRedirected",
            "Missions",
        ):
            self.assertIn(event_name, advertised)

    def test_mission_event_batch_persists_details_and_valid_foreign_keys(self):
        payload = [
            self.common("2026-08-28T20:00:01Z", "Docked"),
            {
                **self.common("2026-08-28T20:00:02Z", "MissionAccepted"),
                "MissionID": 1065000001,
                "Name": "Mission_AltruismCredits",
                "Faction": "East India Company",
                "Expiry": "2026-08-29T20:00:00Z",
            },
            {
                **self.common("2026-08-28T20:00:03Z", "MissionCompleted"),
                "MissionID": 1065000001,
                "Name": "Mission_AltruismCredits_name",
                "Faction": "East India Company",
                "Reward": 1000,
                "FactionEffects": [{
                    "Faction": "East India Company",
                    "Effects": [{
                        "Effect": "$MISSIONUTIL_Interaction_Summary_EP_up;",
                        "Trend": "UpGood",
                    }],
                    "Influence": [
                        {"SystemAddress": 1234567890, "Trend": "UpGood", "Influence": "++"},
                        {"SystemAddress": 9876543210, "Trend": "UpGood", "Influence": "+"},
                    ],
                    "ReputationTrend": "UpGood",
                    "Reputation": "++",
                }],
            },
            {
                **self.common("2026-08-28T20:00:04Z", "MissionFailed"),
                "MissionID": 1065000002,
                "Name": "Mission_Collect_name",
                "Faction": "East India Company",
                "Fine": 25000,
            },
            {
                **self.common("2026-08-28T20:00:05Z", "MissionAbandoned"),
                "MissionID": 1065000003,
                "Name": "Mission_Delivery_name",
            },
            {
                **self.common("2026-08-28T20:00:06Z", "MissionRedirected"),
                "MissionID": 1065000004,
                "NewDestinationSystem": "New System",
                "OldDestinationSystem": "Old System",
            },
            {
                **self.common("2026-08-28T20:00:07Z", "CargoDepot"),
                "MissionID": 1065000005,
                "UpdateType": "Deliver",
                "CargoType": "$Gold_Name;",
                "Count": 5,
            },
            {
                **self.common("2026-08-28T20:00:08Z", "Missions"),
                "Active": [{"MissionID": 1065000006, "Name": "Mission_Courier_name"}],
                "Failed": [],
                "Complete": [],
            },
        ]

        response = self.client.post("/events", json=payload, headers=self.headers())

        self.assertEqual(response.status_code, 200, response.get_json())
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            mission_events = connection.execute(
                "SELECT id, event, raw_json FROM event WHERE event IN "
                "('CargoDepot','MissionAbandoned','MissionAccepted','MissionCompleted',"
                "'MissionFailed','MissionRedirected','Missions') ORDER BY id"
            ).fetchall()
            self.assertEqual(len(mission_events), 7)
            self.assertEqual(
                {row["event"] for row in mission_events},
                {
                    "CargoDepot",
                    "MissionAbandoned",
                    "MissionAccepted",
                    "MissionCompleted",
                    "MissionFailed",
                    "MissionRedirected",
                    "Missions",
                },
            )
            self.assertTrue(all(row["raw_json"] for row in mission_events))

            completed = connection.execute(
                "SELECT id, event_id, mission_id, name, faction, reward "
                "FROM mission_completed_event"
            ).fetchone()
            completed_event = connection.execute(
                "SELECT id FROM event WHERE event='MissionCompleted'"
            ).fetchone()
            self.assertEqual(completed["event_id"], completed_event["id"])
            self.assertEqual(completed["mission_id"], 1065000001)
            self.assertEqual(completed["faction"], "East India Company")

            influences = connection.execute(
                "SELECT mission_id, event_id, influence, faction_name "
                "FROM mission_completed_influence ORDER BY id"
            ).fetchall()
            self.assertEqual(len(influences), 2)
            self.assertTrue(all(row["mission_id"] == completed["id"] for row in influences))
            self.assertTrue(all(row["event_id"] == completed_event["id"] for row in influences))

            failed = connection.execute(
                "SELECT mission_id, name, mission_name, faction, awarding_faction, fine "
                "FROM mission_failed_event"
            ).fetchone()
            self.assertEqual(failed["mission_id"], 1065000002)
            self.assertEqual(failed["name"], "Mission_Collect_name")
            self.assertEqual(failed["faction"], "East India Company")
            self.assertEqual(failed["fine"], 25000)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()

        influence_response = self.client.get(
            "/api/summary/influence-by-faction?period=all",
            headers=self.headers(),
        )
        self.assertEqual(influence_response.status_code, 200, influence_response.get_json())
        self.assertEqual(influence_response.get_json(), [{
            "cmdr": "Probe",
            "faction_name": "East India Company",
            "influence": 3,
        }])

        for path in (
            "/api/summary/influence-eic?period=all",
            "/api/summary/top5/influence-by-faction?period=all",
            "/api/summary/top5/influence-eic?period=all",
            "/api/summary/leaderboard?period=all",
        ):
            summary_response = self.client.get(path, headers=self.headers())
            self.assertEqual(
                summary_response.status_code,
                200,
                {"path": path, "response": summary_response.get_json()},
            )

        failed_response = self.client.get(
            "/api/summary/missions-failed?period=all",
            headers=self.headers(),
        )
        self.assertEqual(failed_response.status_code, 200, failed_response.get_json())
        self.assertEqual(failed_response.get_json(), [{"cmdr": "Probe", "missions_failed": 1}])

    def test_mission_completed_without_faction_effects_is_still_persisted(self):
        payload = [
            self.common("2026-08-28T21:00:01Z", "Docked"),
            {
                **self.common("2026-08-28T21:00:02Z", "MissionCompleted"),
                "MissionID": 1065000010,
                "Name": "Mission_Courier_name",
                "Faction": "East India Company",
                "FactionEffects": None,
            },
        ]

        response = self.client.post("/events", json=payload, headers=self.headers())

        self.assertEqual(response.status_code, 200, response.get_json())
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM event")).scalar(), 2)
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM mission_completed_event")).scalar(),
                1,
            )
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM mission_completed_influence")).scalar(),
                0,
            )

    def test_influence_summary_keeps_legacy_event_id_links_readable(self):
        with self.engine.begin() as connection:
            for second in range(1, 4):
                connection.execute(text(
                    "INSERT INTO event(event,timestamp,tickid,ticktime,cmdr,raw_json) "
                    "VALUES('Docked',:timestamp,'123456789012345678901234',"
                    "'2026-08-28T20:00:00Z','Legacy','{}')"
                ), {"timestamp": f"2026-08-28T22:00:0{second}Z"})
            legacy_event_id = connection.execute(text(
                "INSERT INTO event(event,timestamp,tickid,ticktime,cmdr,raw_json) "
                "VALUES('MissionCompleted','2026-08-28T22:00:04Z',"
                "'123456789012345678901234','2026-08-28T20:00:00Z','Legacy','{}')"
            )).lastrowid
            connection.execute(text(
                "INSERT INTO mission_completed_event(event_id,mission_id,name,faction) "
                "VALUES(:event_id,1065000020,'Legacy Mission','East India Company')"
            ), {"event_id": legacy_event_id})
            connection.execute(text(
                "INSERT INTO mission_completed_influence"
                "(mission_id,event_id,system,influence,faction_name) "
                "VALUES(:legacy_event_id,NULL,'1234567890','++','East India Company')"
            ), {"legacy_event_id": legacy_event_id})

        response = self.client.get(
            "/api/summary/influence-by-faction?period=all",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json(), [{
            "cmdr": "Legacy",
            "faction_name": "East India Company",
            "influence": 2,
        }])

    def test_manual_mission_events_use_the_same_normalized_links_and_details(self):
        completed_data = {
            "submission_id": "manual-mission-completed",
            "source": "test",
            "discord": {},
            "activity_type": "mission_completed",
            "amount": 5000,
            "count": 1,
            "influence": 2,
            "faction_name": "East India Company",
            "faction_state": "Boom",
            "cmdr": "Manual Probe",
            "system_name": "Probe System",
            "system_address": 1234567890,
        }
        failed_data = {
            **completed_data,
            "submission_id": "manual-mission-failed",
            "activity_type": "mission_failed",
            "amount": 25000,
            "count": 1,
        }

        with flask_app.app.app_context():
            completed_ids = _create_events(
                completed_data,
                "2026-08-28T23:00:01Z",
                "123456789012345678901234",
                "2026-08-28T20:00:00Z",
            )
            failed_ids = _create_events(
                failed_data,
                "2026-08-28T23:00:02Z",
                "123456789012345678901234",
                "2026-08-28T20:00:00Z",
            )
            db.session.commit()

        with self.engine.connect() as connection:
            completed = connection.execute(text(
                "SELECT id,event_id,mission_id FROM mission_completed_event"
            )).mappings().one()
            influence = connection.execute(text(
                "SELECT mission_id,event_id,influence FROM mission_completed_influence"
            )).mappings().one()
            failed = connection.execute(text(
                "SELECT event_id,mission_id,name,faction,fine FROM mission_failed_event"
            )).mappings().one()

        self.assertEqual(completed_ids, [completed["event_id"]])
        self.assertEqual(influence["mission_id"], completed["id"])
        self.assertEqual(influence["event_id"], completed["event_id"])
        self.assertEqual(influence["influence"], "++")
        self.assertEqual(failed_ids, [failed["event_id"]])
        self.assertEqual(failed["mission_id"], failed["event_id"])
        self.assertEqual(failed["name"], "Manual Discord Activity")
        self.assertEqual(failed["faction"], "East India Company")
        self.assertEqual(failed["fine"], 25000)


class MissionSchemaMigrationTest(unittest.TestCase):
    def test_existing_mission_tables_gain_new_columns_without_rewriting_rows(self):
        original_tenants = list(database_manager.TENANTS)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "legacy.sqlite"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript("""
                    CREATE TABLE event (
                        id INTEGER PRIMARY KEY,
                        event VARCHAR(64) NOT NULL,
                        timestamp VARCHAR(64) NOT NULL,
                        tickid VARCHAR(24) NOT NULL,
                        ticktime VARCHAR(64) NOT NULL,
                        cmdr VARCHAR(64),
                        starsystem VARCHAR(128),
                        systemaddress BIGINT,
                        raw_json TEXT
                    );
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY,
                        username VARCHAR(64) NOT NULL UNIQUE,
                        password_hash VARCHAR(128) NOT NULL,
                        is_admin INTEGER NOT NULL DEFAULT 0,
                        active INTEGER NOT NULL DEFAULT 1
                    );
                    CREATE TABLE mission_completed_event (
                        id INTEGER PRIMARY KEY,
                        event_id INTEGER NOT NULL,
                        awarding_faction VARCHAR(128),
                        mission_name VARCHAR(128),
                        reward INTEGER,
                        FOREIGN KEY(event_id) REFERENCES event(id)
                    );
                    CREATE TABLE mission_completed_influence (
                        id INTEGER PRIMARY KEY,
                        mission_id INTEGER NOT NULL,
                        system VARCHAR(128),
                        influence VARCHAR(8),
                        trend VARCHAR(32),
                        faction_name VARCHAR(128),
                        reputation VARCHAR(8),
                        reputation_trend VARCHAR(32),
                        effect VARCHAR(128),
                        effect_trend VARCHAR(32),
                        FOREIGN KEY(mission_id) REFERENCES mission_completed_event(id)
                    );
                    CREATE TABLE mission_failed_event (
                        id INTEGER PRIMARY KEY,
                        event_id INTEGER NOT NULL,
                        mission_name VARCHAR(128),
                        awarding_faction VARCHAR(128),
                        fine INTEGER,
                        FOREIGN KEY(event_id) REFERENCES event(id)
                    );
                    INSERT INTO mission_completed_influence(
                        id,mission_id,system,influence,faction_name
                    ) VALUES(1,99,'1234567890','++','Legacy Faction');
                """)
                connection.commit()
            finally:
                connection.close()

            database_manager.TENANTS[:] = [{
                "name": "Legacy Tenant",
                "db_uri": f"sqlite:///{db_path.as_posix()}",
            }]
            try:
                database_manager.update_all_tenant_databases()
            finally:
                database_manager.TENANTS[:] = original_tenants

            connection = sqlite3.connect(db_path)
            try:
                influence_columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(mission_completed_influence)"
                    )
                }
                failed_columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(mission_failed_event)"
                    )
                }
                legacy_row = connection.execute(
                    "SELECT mission_id,event_id,influence FROM mission_completed_influence WHERE id=1"
                ).fetchone()
            finally:
                connection.close()

        self.assertIn("event_id", influence_columns)
        self.assertTrue({"mission_id", "name", "faction"}.issubset(failed_columns))
        self.assertEqual(legacy_row, (99, None, "++"))


if __name__ == "__main__":
    unittest.main()
