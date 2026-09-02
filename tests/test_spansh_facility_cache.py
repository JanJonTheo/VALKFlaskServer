from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import spansh_facility_cache as cache


class SpanshFacilityCacheTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_cache = os.environ.get("SPANSH_CACHE_DB")
        os.environ["SPANSH_CACHE_DB"] = os.path.join(
            self.tempdir.name, "spansh-facilities.db"
        )

    def tearDown(self):
        if self.previous_cache is None:
            os.environ.pop("SPANSH_CACHE_DB", None)
        else:
            os.environ["SPANSH_CACHE_DB"] = self.previous_cache
        self.tempdir.cleanup()

    @staticmethod
    def source(path):
        if path.startswith("search/systems"):
            return {
                "results": [
                    {"id64": 123456789, "name": "Test System"},
                    {"id64": 987654321, "name": "Test System B"},
                ]
            }
        if path == "dump/123456789":
            return {
                "system": {
                    "id64": 123456789,
                    "name": "Test System",
                    "x": 10.5,
                    "y": -2.0,
                    "z": 44.25,
                    "factions": [
                        {
                            "name": "Test Faction",
                            "influence": 0.42,
                            "government": "Corporate",
                        }
                    ],
                    "date": "2026-08-29T01:00:00Z",
                    "stations": [
                        {
                            "id": 1001,
                            "name": "Test Gateway",
                            "type": "Coriolis Starport",
                            "distanceToArrival": 42.5,
                            "controllingFaction": "Test Faction",
                            "primaryEconomy": "Industrial",
                            "services": ["Market", "Shipyard"],
                            "market": {"commodities": []},
                            "shipyard": {"ships": []},
                            "updateTime": "2026-08-29T00:50:00Z",
                        },
                        {
                            "id": 1003,
                            "name": "Test Orbis",
                            "type": "Orbis Starport",
                        },
                        {
                            "id": 1004,
                            "name": "Test Ocellus",
                            "type": "Ocellus Starport",
                        },
                        {
                            "id": 1005,
                            "name": "Test Dodec",
                            "type": "Dodecagonal Agricultural Station",
                        },
                        {
                            "id": 1006,
                            "name": "G4K-N8L",
                            "carrierName": "[VALK] Graf Zeppelin",
                            "type": "Drake-Class Carrier",
                        },
                    ],
                    "bodies": [
                        {
                            "id64": 123456790,
                            "name": "Test System 1",
                            "stations": [
                                {
                                    "id": 1002,
                                    "name": "Test Settlement",
                                    "type": "Settlement",
                                    "latitude": 1.5,
                                    "longitude": 2.5,
                                    "services": ["Vista Genomics"],
                                }
                            ],
                        }
                    ],
                }
            }
        raise AssertionError(f"Unexpected Spansh path {path}")

    def test_persists_flattened_system_and_body_facilities(self):
        with patch.object(cache, "_request_json", side_effect=self.source) as request:
            first = cache.get_system_facilities("Test System")
            second = cache.get_system_facilities("Test System")

        self.assertEqual(first["cache_status"], "MISS")
        self.assertEqual(second["cache_status"], "HIT")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(first["stations"]), 6)
        self.assertEqual(first["coordinates"], {"x": 10.5, "y": -2.0, "z": 44.25})
        self.assertEqual(first["faction_count"], 1)
        self.assertEqual(first["factions"][0]["name"], "Test Faction")
        self.assertEqual(
            first["facility_type_counts"],
            {
                "Coriolis Starport": 1,
                "Dodecagonal Agricultural Station": 1,
                "Drake-Class Carrier": 1,
                "Ocellus Starport": 1,
                "Orbis Starport": 1,
                "Settlement": 1,
            },
        )
        settlement = next(item for item in first["stations"] if item["is_settlement"])
        self.assertEqual(settlement["body"], "Test System 1")
        self.assertEqual(settlement["latitude"], 1.5)
        carrier = next(
            item for item in first["stations"] if item["type"] == "Drake-Class Carrier"
        )
        self.assertEqual(carrier["name"], "G4K-N8L")
        self.assertEqual(carrier["carrier_name"], "[VALK] Graf Zeppelin")
        self.assertEqual(carrier["carrier_owner"], "")
        self.assertEqual(
            cache.get_facility_type_statistics(
                ["Test System", "test system", "Missing System"]
            ),
            {
                "types": {
                    "dodec": 1,
                    "orbis": 1,
                    "ocellus": 1,
                    "coriolis": 1,
                },
                "cached_systems": 1,
                "requested_systems": 2,
            },
        )

    def test_serves_stale_cache_when_spansh_is_unavailable(self):
        with patch.object(cache, "_request_json", side_effect=self.source):
            cache.get_system_facilities("Test System")
        with patch.object(cache, "_request_json", side_effect=TimeoutError("offline")):
            result = cache.get_system_facilities("Test System", force=True)

        self.assertEqual(result["cache_status"], "STALE")
        self.assertTrue(result["stale"])
        self.assertIn("offline", result["warning"])
        self.assertEqual(len(result["stations"]), 6)

    def test_watchlist_collection_finds_runtime_parent_database(self):
        runtime_root = Path(self.tempdir.name)
        module_dir = runtime_root / "valk"
        shadow_database = module_dir / "db" / "tenant.db"
        tenant_database = runtime_root / "db" / "tenant.db"
        shadow_database.parent.mkdir(parents=True)
        tenant_database.parent.mkdir(parents=True)

        with closing(sqlite3.connect(shadow_database)) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
            connection.commit()
        with closing(sqlite3.connect(tenant_database)) as connection:
            connection.execute(
                "CREATE TABLE dashboard_view_preference "
                "(view_key TEXT NOT NULL, payload_json TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO dashboard_view_preference(view_key, payload_json) "
                "VALUES (?, ?)",
                (
                    cache.WATCHLIST_VIEW_KEY,
                    json.dumps(
                        {
                            "systems": [
                                {"system": "Test System"},
                                {"system": "test system"},
                                {"system": "Second System"},
                            ]
                        }
                    ),
                ),
            )
            connection.commit()

        with patch.object(cache, "BASE_DIR", module_dir):
            systems = cache.collect_tenant_watchlist_systems(
                [{"db_uri": "sqlite:///db/tenant.db"}]
            )

        self.assertEqual(systems, ["Second System", "Test System"])


if __name__ == "__main__":
    unittest.main()
