import tempfile
import unittest
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import jwt
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import scoped_session, sessionmaker

import app as flask_app
from dashboard_users import ensure_dashboard_schema
from models import Event, db


class ColonisationApiTest(unittest.TestCase):
    def setUp(self):
        self.original_tenants = list(flask_app.TENANTS)
        self.original_tickids = dict(flask_app.last_known_tickid)
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmpdir.name) / "tenant.sqlite"
        self.engine = create_engine(
            f"sqlite:///{self.db_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        db.Model.metadata.create_all(bind=self.engine)
        db.session = scoped_session(sessionmaker(bind=self.engine))
        flask_app.TENANTS[:] = [{
            "name": "VALK Development",
            "api_key": "temp-key",
            "api_version": flask_app.API_VERSION,
            "db_uri": f"sqlite:///{self.db_path.as_posix()}",
            "discord_webhooks": {},
        }]
        flask_app.last_known_tickid["VALK Development"] = "123456789012345678901234"
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

    def headers(self):
        return {"apikey": "temp-key", "apiversion": flask_app.API_VERSION}

    def seed_colonisation_events(self):
        payload = [
            {
                "timestamp": "2026-08-27T22:31:27Z",
                "event": "Docked",
                "cmdr": "JanJonTheo",
                "tickid": "123456789012345678901234",
                "ticktime": "2026-08-27T20:00:00Z",
                "StarSystem": "Synookoi AJ-A c14-30",
                "SystemAddress": 8336162561146,
                "MarketID": 3965326082,
                "StationName": "Orbital Construction Site: Cook Vision",
                "StationType": "SpaceConstructionDepot",
            },
            {
                "timestamp": "2026-08-27T22:31:59Z",
                "event": "ColonisationContribution",
                "cmdr": "JanJonTheo",
                "tickid": "123456789012345678901234",
                "ticktime": "2026-08-27T20:00:00Z",
                "StarSystem": "Synookoi AJ-A c14-30",
                "SystemAddress": 8336162561146,
                "MarketID": 3965326082,
                "Contributions": [{"Name": "$Titanium_name;", "Amount": 40}],
            },
            {
                "timestamp": "2026-08-27T22:32:22Z",
                "event": "ColonisationConstructionDepot",
                "cmdr": "JanJonTheo",
                "tickid": "123456789012345678901234",
                "ticktime": "2026-08-27T20:00:00Z",
                "StarSystem": "Synookoi AJ-A c14-30",
                "SystemAddress": 8336162561146,
                "MarketID": 3965326082,
                "ConstructionProgress": 0.5,
                "ConstructionComplete": False,
                "ConstructionFailed": False,
                "ResourcesRequired": [
                    {
                        "Name": "$aluminium_name;",
                        "RequiredAmount": 100,
                        "ProvidedAmount": 100,
                        "Payment": 3000,
                    },
                    {
                        "Name": "$titanium_name;",
                        "RequiredAmount": 50,
                        "ProvidedAmount": 10,
                        "Payment": 5000,
                    },
                ],
            },
        ]
        response = self.client.post("/events", json=payload, headers=self.headers())
        self.assertEqual(response.status_code, 200)

    def test_dashboard_bearer_can_read_colonisation_without_legacy_api_key(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1
                )
            """))
            conn.execute(text(
                "INSERT INTO users(id, username, password_hash, is_admin, active) "
                "VALUES (1, 'member', 'unused', 0, 1)"
            ))
        ensure_dashboard_schema(self.engine)
        secret = "colonisation-dashboard-test-secret"
        token = jwt.encode(
            {
                "sub": "1",
                "tenant_id": "valk-development",
                "role": "member",
                "capabilities": ["dashboard:read"],
                "aud": "valk-api",
                "jti": "colonisation-read-test",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            secret,
            algorithm="HS256",
        )
        with patch.dict(os.environ, {"DASHBOARD_JWT_SECRET": secret}):
            response = self.client.get(
                "/api/colonisation/contributions?period=all",
                headers={"authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["tenant"], "VALK Development")

    def test_summary_uses_received_journal_events(self):
        self.seed_colonisation_events()

        response = self.client.get(
            "/api/colonisation/summary?cmdr=JanJonTheo&market_id=3965326082",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"], "VALK Development")
        self.assertEqual(payload["cmdr"], "JanJonTheo")
        self.assertEqual(payload["target_system"], "Synookoi AJ-A c14-30")
        self.assertEqual(payload["target_station"], "Orbital Construction Site: Cook Vision")
        self.assertEqual(payload["target_name"], "Cook Vision")
        self.assertEqual(payload["market_id"], 3965326082)
        self.assertEqual(payload["total_need"], 150)
        self.assertEqual(payload["total_provided"], 110)
        self.assertEqual(payload["total_remaining"], 40)
        self.assertEqual(payload["session_delivered"], 40)
        self.assertEqual(payload["total_delivered"], 110)
        self.assertIn("Colonisation Summary", payload["text"])
        self.assertIn("Target : Cook Vision", payload["text"])
        self.assertNotIn("Target : Orbital Construction Site: Cook Vision", payload["text"])
        self.assertIn("Titanium                   50     10     40 open", payload["text"])

    def test_contributions_can_be_grouped_and_filtered(self):
        self.seed_colonisation_events()
        db.session.add(Event(
            event="ColonisationContribution",
            timestamp="2026-08-26T10:00:00Z",
            tickid="000000000000000000000001",
            ticktime="2026-08-26T08:00:00Z",
            cmdr="OtherCmdr",
            starsystem="Other System",
            systemaddress=42,
            raw_json=str({
                "timestamp": "2026-08-26T10:00:00Z",
                "event": "ColonisationContribution",
                "cmdr": "OtherCmdr",
                "tickid": "000000000000000000000001",
                "ticktime": "2026-08-26T08:00:00Z",
                "StarSystem": "Other System",
                "SystemAddress": 42,
                "MarketID": 12345,
                "Contributions": [{"Name": "$aluminium_name;", "Amount": 99}],
            }),
        ))
        db.session.commit()

        tick_response = self.client.get(
            "/api/colonisation/contributions?group_by=construction&period=ct&market_ids=3965326082",
            headers=self.headers(),
        )
        self.assertEqual(tick_response.status_code, 200)
        tick_payload = tick_response.get_json()
        self.assertEqual(tick_payload["tenant"], "VALK Development")
        self.assertEqual(tick_payload["filters"]["group_by"], "construction")
        self.assertEqual(tick_payload["filters"]["tickid"], "123456789012345678901234")
        self.assertEqual(tick_payload["filters"]["market_ids"], [3965326082])
        self.assertEqual(tick_payload["totals"]["quantity"], 40)
        self.assertEqual(tick_payload["totals"]["event_count"], 1)
        self.assertEqual(tick_payload["groups"][0]["label"], "Cook Vision")
        self.assertEqual(tick_payload["groups"][0]["quantity"], 40)
        self.assertEqual(tick_payload["groups"][0]["subgroups"][0]["label"], "JanJonTheo")
        self.assertEqual(tick_payload["groups"][0]["subgroups"][0]["commodities"][0]["commodity"], "Titanium")
        self.assertEqual(tick_payload["groups"][0]["subgroups"][0]["commodities"][0]["quantity"], 40)
        self.assertEqual(tick_payload["records"][0]["commodity"], "Titanium")
        self.assertIn("Colonisation Contributions", tick_payload["text"])
        self.assertIn("- Titanium", tick_payload["text"])

        cmdr_response = self.client.get(
            "/api/colonisation/contributions"
            "?group_by=cmdr&period=custom&from=2026-08-27&to=2026-08-27&cmdr=JanJonTheo",
            headers=self.headers(),
        )
        self.assertEqual(cmdr_response.status_code, 200)
        cmdr_payload = cmdr_response.get_json()
        self.assertEqual(cmdr_payload["filters"]["group_by"], "cmdr")
        self.assertEqual(cmdr_payload["filters"]["cmdrs"], ["JanJonTheo"])
        self.assertEqual(cmdr_payload["totals"]["quantity"], 40)
        self.assertEqual(cmdr_payload["groups"][0]["label"], "JanJonTheo")
        self.assertEqual(cmdr_payload["groups"][0]["subgroups"][0]["label"], "Cook Vision")

        construction_response = self.client.get(
            "/api/colonisation/contributions?group_by=construction&period=ct&construction=Cook%20Vision",
            headers=self.headers(),
        )
        self.assertEqual(construction_response.status_code, 200)
        construction_payload = construction_response.get_json()
        self.assertEqual(construction_payload["totals"]["quantity"], 40)
        self.assertEqual(construction_payload["filters"]["constructions"], ["Cook Vision"])

    def test_construction_status_lists_open_and_finished_details(self):
        self.seed_colonisation_events()
        db.session.add(Event(
            event="Docked",
            timestamp="2026-08-25T20:00:00Z",
            tickid="000000000000000000000002",
            ticktime="2026-08-25T18:00:00Z",
            cmdr="OtherCmdr",
            starsystem="Finished System",
            systemaddress=777,
            raw_json=str({
                "timestamp": "2026-08-25T20:00:00Z",
                "event": "Docked",
                "cmdr": "OtherCmdr",
                "tickid": "000000000000000000000002",
                "ticktime": "2026-08-25T18:00:00Z",
                "StarSystem": "Finished System",
                "SystemAddress": 777,
                "MarketID": 777001,
                "StationName": "Orbital Construction Site: Finished Base",
            }),
        ))
        db.session.add(Event(
            event="ColonisationContribution",
            timestamp="2026-08-25T20:15:00Z",
            tickid="000000000000000000000002",
            ticktime="2026-08-25T18:00:00Z",
            cmdr="OtherCmdr",
            starsystem="Finished System",
            systemaddress=777,
            raw_json=str({
                "timestamp": "2026-08-25T20:15:00Z",
                "event": "ColonisationContribution",
                "cmdr": "OtherCmdr",
                "tickid": "000000000000000000000002",
                "ticktime": "2026-08-25T18:00:00Z",
                "StarSystem": "Finished System",
                "SystemAddress": 777,
                "MarketID": 777001,
                "Contributions": [{"Name": "$water_name;", "Amount": 10}],
            }),
        ))
        db.session.add(Event(
            event="ColonisationConstructionDepot",
            timestamp="2026-08-25T20:30:00Z",
            tickid="000000000000000000000002",
            ticktime="2026-08-25T18:00:00Z",
            cmdr="OtherCmdr",
            starsystem="Finished System",
            systemaddress=777,
            raw_json=str({
                "timestamp": "2026-08-25T20:30:00Z",
                "event": "ColonisationConstructionDepot",
                "cmdr": "OtherCmdr",
                "tickid": "000000000000000000000002",
                "ticktime": "2026-08-25T18:00:00Z",
                "StarSystem": "Finished System",
                "SystemAddress": 777,
                "MarketID": 777001,
                "ConstructionProgress": 1.0,
                "ConstructionComplete": True,
                "ConstructionFailed": False,
                "ResourcesRequired": [
                    {"Name": "$water_name;", "RequiredAmount": 10, "ProvidedAmount": 10}
                ],
            }),
        ))
        db.session.commit()

        open_response = self.client.get(
            "/api/colonisation/constructions?status=open&period=ct&cmdr=JanJonTheo&market_ids=3965326082",
            headers=self.headers(),
        )
        self.assertEqual(open_response.status_code, 200)
        open_payload = open_response.get_json()
        self.assertEqual(open_payload["filters"]["market_ids"], [3965326082])
        self.assertEqual(open_payload["totals"]["construction_count"], 1)
        self.assertEqual(open_payload["constructions"][0]["status"], "open")
        self.assertEqual(open_payload["constructions"][0]["total_remaining"], 40)
        titanium = next(
            item for item in open_payload["constructions"][0]["commodities"]
            if item["commodity"] == "Titanium"
        )
        self.assertEqual(titanium["contributors"][0]["cmdr"], "JanJonTheo")
        self.assertEqual(titanium["contributors"][0]["quantity"], 40)

        finished_response = self.client.get(
            "/api/colonisation/constructions?status=finished&period=all",
            headers=self.headers(),
        )
        self.assertEqual(finished_response.status_code, 200)
        finished_payload = finished_response.get_json()
        self.assertEqual(finished_payload["totals"]["construction_count"], 1)
        construction = finished_payload["constructions"][0]
        self.assertEqual(construction["label"], "Finished Base")
        self.assertEqual(construction["status"], "finished")
        self.assertEqual(construction["commodities"][0]["remaining"], 0)
        self.assertEqual(construction["commodities"][0]["contributors"][0]["cmdr"], "OtherCmdr")
        self.assertEqual(construction["commodities"][0]["contributors"][0]["last_delivery_at"], "2026-08-25T20:15:00Z")

    def test_delivery_and_status_logs_override_session_totals(self):
        self.seed_colonisation_events()

        delivery_response = self.client.post(
            "/api/colonisation/deliveries",
            json={
                "deliveries": [
                    {
                        "DeliveryId": "delivery-1",
                        "SessionId": "session-a",
                        "ClientId": "client-1",
                        "CmdrName": "JanJonTheo",
                        "TargetName": "Cook Vision",
                        "TargetSystem": "Synookoi AJ-A c14-30",
                        "TargetStation": "Orbital Construction Site: Cook Vision",
                        "ConstructionMarketID": 3965326082,
                        "CommodityKey": "titanium",
                        "Name_Localised": "Titanium",
                        "Quantity": 25,
                        "VerificationSource": "ConstructionDepotDetails",
                    },
                    {
                        "DeliveryId": "delivery-2",
                        "SessionId": "session-b",
                        "ClientId": "client-2",
                        "CmdrName": "JanJonTheo",
                        "TargetName": "Cook Vision",
                        "TargetSystem": "Synookoi AJ-A c14-30",
                        "TargetStation": "Orbital Construction Site: Cook Vision",
                        "ConstructionMarketID": 3965326082,
                        "CommodityKey": "aluminium",
                        "Name_Localised": "Aluminium",
                        "Quantity": 15,
                    },
                ]
            },
            headers=self.headers(),
        )
        self.assertEqual(delivery_response.status_code, 200)
        self.assertEqual(delivery_response.get_json()["saved_count"], 2)

        status_response = self.client.post(
            "/api/colonisation/status",
            json={
                "StatusId": "status-1",
                "SessionId": "session-a",
                "ClientId": "client-1",
                "CmdrName": "JanJonTheo",
                "TargetName": "Cook Vision",
                "TargetSystem": "Synookoi AJ-A c14-30",
                "TargetStation": "Orbital Construction Site: Cook Vision",
                "ConstructionMarketID": 3965326082,
                "Phase": "aborted",
                "Reason": "Colonisation Assist stopped by client or hotkey.",
                "CargoCount": 7,
            },
            headers=self.headers(),
        )
        self.assertEqual(status_response.status_code, 200)

        response = self.client.get(
            "/api/colonisation/summary?cmdr=JanJonTheo&market_id=3965326082",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["session_id"], "session-a")
        self.assertEqual(payload["session_delivered"], 25)
        self.assertEqual(payload["total_delivered"], 40)
        self.assertEqual(payload["cargo_count"], 7)
        self.assertEqual(payload["reason"], "Colonisation Assist stopped by client or hotkey.")
        self.assertIn("Reason : Colonisation Assist stopped by client or hotkey.", payload["text"])
        self.assertIn("Delivered session 25t; local total 40t; cargo 7t.", payload["text"])


if __name__ == "__main__":
    unittest.main()
