import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import app as flask_app
from models import db


class BGSTallyDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.original_tenants = list(flask_app.TENANTS)
        self.original_tickids = dict(flask_app.last_known_tickid)
        self.client = flask_app.app.test_client()

    def tearDown(self):
        flask_app.TENANTS[:] = self.original_tenants
        flask_app.last_known_tickid.clear()
        flask_app.last_known_tickid.update(self.original_tickids)
        try:
            db.session.remove()
        except RuntimeError:
            pass

    def test_discovery_requests_colonisation_events(self):
        response = self.client.get("/discovery")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        events = payload["events"]

        self.assertEqual(payload["version"], "1.8.0")
        self.assertEqual(payload["headers"]["apiversion"]["current"], "1.8.0")
        self.assertIn("MarketBuy", events)
        self.assertIn("SyntheticScenario", events)
        self.assertIn("ColonisationSystemClaim", events)
        self.assertIn("ColonisationBeaconDeployed", events)
        self.assertIn("ColonisationConstructionDepot", events)
        self.assertIn("ColonisationContribution", events)
        self.assertEqual(payload["endpoints"]["events"]["min_period"], "10")
        self.assertEqual(payload["endpoints"]["events"]["max_batch"], "100")

    def test_events_endpoint_stores_colonisation_event_raw(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "tenant.sqlite"
            db_uri = f"sqlite:///{db_path.as_posix()}"
            engine = create_engine(db_uri, connect_args={"check_same_thread": False})
            db.Model.metadata.create_all(bind=engine)
            db.session = scoped_session(sessionmaker(bind=engine))

            tickid = "123456789012345678901234"
            flask_app.TENANTS[:] = [{
                "name": "Temp Tenant",
                "api_key": "temp-key",
                "api_version": flask_app.API_VERSION,
                "db_uri": db_uri,
                "discord_webhooks": {},
            }]
            flask_app.last_known_tickid["Temp Tenant"] = tickid

            response = self.client.post(
                "/events",
                json=[{
                    "timestamp": "2026-08-27T21:00:00Z",
                    "event": "ColonisationContribution",
                    "cmdr": "Probe",
                    "tickid": tickid,
                    "ticktime": "2026-08-27T20:00:00Z",
                    "StarSystem": "Probe System",
                    "SystemAddress": 1234567890,
                    "MarketID": 3962369026,
                    "Contributions": [{"Name": "$steel_name;", "Amount": 42}],
                }],
                headers={"apikey": "temp-key", "apiversion": flask_app.API_VERSION},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"status": "success"})

            connection = sqlite3.connect(db_path)
            try:
                row = connection.execute(
                    "SELECT event, starsystem, systemaddress, raw_json FROM event"
                ).fetchone()
                self.assertEqual(row[0], "ColonisationContribution")
                self.assertEqual(row[1], "Probe System")
                self.assertEqual(row[2], 1234567890)
                self.assertIn("Contributions", row[3])

                colon_tables = {
                    row[0]
                    for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%colon%'"
                    ).fetchall()
                }
                self.assertIn("colonisation_delivery", colon_tables)
                self.assertIn("colonisation_assist_status", colon_tables)
                self.assertNotIn("colonisation_construction_depot_event", colon_tables)
            finally:
                connection.close()
                db.session.remove()
                engine.dispose()

    def test_events_endpoint_preserves_redeem_voucher_faction_allocations(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "tenant.sqlite"
            db_uri = f"sqlite:///{db_path.as_posix()}"
            engine = create_engine(db_uri, connect_args={"check_same_thread": False})
            db.Model.metadata.create_all(bind=engine)
            db.session = scoped_session(sessionmaker(bind=engine))

            tickid = "123456789012345678901234"
            flask_app.TENANTS[:] = [{
                "name": "Temp Tenant",
                "api_key": "temp-key",
                "api_version": flask_app.API_VERSION,
                "db_uri": db_uri,
                "discord_webhooks": {},
            }]

            factions = [{"Faction": "East India Company", "Amount": 13274495}]
            response = self.client.post(
                "/events",
                json=[{
                    "timestamp": "2026-07-27T16:11:08Z",
                    "event": "RedeemVoucher",
                    "Type": "bounty",
                    "Amount": 13274495,
                    "Factions": factions,
                    "cmdr": "JanJonTheo",
                    "tickid": tickid,
                    "ticktime": "2026-07-27T09:54:27Z",
                    "StationFaction": {"Name": "Blackiron"},
                    "StarSystem": "HIP 52503",
                    "SystemAddress": "285455960435",
                }],
                headers={"apikey": "temp-key", "apiversion": flask_app.API_VERSION},
            )

            self.assertEqual(response.status_code, 200)
            connection = sqlite3.connect(db_path)
            try:
                row = connection.execute(
                    """
                    SELECT e.raw_json, rv.faction, rv.factions
                    FROM event e
                    JOIN redeem_voucher_event rv ON rv.event_id = e.id
                    """
                ).fetchone()
                self.assertEqual(json.loads(row[0])["Factions"], factions)
                self.assertEqual(row[1], "East India Company")
                self.assertEqual(json.loads(row[2]), factions)
            finally:
                connection.close()
                db.session.remove()
                engine.dispose()
