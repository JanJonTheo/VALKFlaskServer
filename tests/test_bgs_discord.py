import json
import os
import tempfile
import unittest
from unittest.mock import patch

from bgs_discord import context, notification, current_system
from sqlalchemy import create_engine, text


class DiscordEmbedTests(unittest.TestCase):
    def test_current_data_uses_database_and_raw_faction_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            uri = "sqlite:///" + directory + "/current.db"
            engine = create_engine(uri)
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE eddn_system_info(system_name TEXT, eddn_message_id TEXT, updated_at TEXT)"))
                conn.execute(text("CREATE TABLE eddn_faction(system_name TEXT, name TEXT, influence REAL, updated_at TEXT)"))
                conn.execute(text("CREATE TABLE eddn_message(id TEXT, message_json TEXT)"))
                conn.execute(text("INSERT INTO eddn_system_info VALUES ('Sol','m','2026-09-08')"))
                conn.execute(text("INSERT INTO eddn_faction VALUES ('Sol','A',0.4,'2026-09-08')"))
                conn.execute(text("INSERT INTO eddn_message VALUES ('m',:payload)"), {"payload": repr({"message": {"Factions": [{"Name": "A", "Government_Localised": "Democracy", "Allegiance": "Federation"}]}})})
            engine.dispose()
            with patch.dict(os.environ, {"EDDN_DATABASE": uri}):
                info, factions = current_system("Sol")
            self.assertEqual(info["updated_at"], "2026-09-08")
            self.assertEqual(factions[0]["government"], "Democracy")
            self.assertEqual(factions[0]["influence"], .4)

    def alert(self, facts=None, **extra):
        return {"system_name": "39 Tauri", "title": "Faction closes to a 2 pp gap", "message": "Historical gap warning", "severity": "warning", "fired_ticktime": "2026-09-08T01:07:08Z", "fired_at": "2026-09-08T18:37:55+00:00", "facts_json": json.dumps(facts or {}), **extra}

    def test_gap_selects_event_and_separates_current_values(self):
        alert = self.alert({"tenant_faction": "EIC", "threshold_pp": 2, "entered_factions": [{"faction": "Rival", "gap_pp": 1.87}, {"faction": "Other", "gap_pp": .7}]}, event_key="gap:rival")
        body = notification(alert, {}, [{"name": "EIC", "influence": .40}, {"name": "Rival", "influence": .35}, {"name": "Other", "influence": .25}])
        embed = body["embeds"][0]
        self.assertIn("Gap 1.87 pp", embed["description"])
        self.assertIn("Threshold 2.00 pp · at trigger", embed["description"])
        self.assertEqual([f["name"] for f in embed["fields"] if f.get("inline")], ["⚠ EIC", "⚠ Rival"])
        self.assertIn("40.00%", embed["fields"][0]["value"])
        self.assertIn("35.00%", embed["fields"][1]["value"])
        self.assertEqual(embed["color"], 0xE3BD59)
        self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_single_faction_missing_values_and_real_zero(self):
        alert = self.alert({"tenant_faction": "EIC", "loss_pp": 4, "threshold_pp": 3})
        missing = notification(alert)["embeds"][0]["fields"][0]
        self.assertEqual(missing["value"], "Current faction data unavailable")
        for value, expected in [(None, "—"), (0, "0.00%")]:
            field = notification(alert, {}, [{"name": "EIC", "influence": value}])["embeds"][0]["fields"][0]
            self.assertIn(expected, field["value"])

    def test_context_variants_and_ambiguous_legacy(self):
        cases = [({"controlling_faction": "A", "competitor": "B", "gap_pp": 1}, ["A", "B"], "Gap 1.00 pp"), ({"new_conflicts": [{"faction1": "A", "faction2": "B", "type": "War"}]}, ["A", "B"], "New conflict: War"), ({"strongest_change": {"faction": "A", "delta_pp": -4}}, ["A"], "Loss 4.00 pp"), ({"tenant_faction": "A", "tenant_influence_pp": 4, "threshold_pp": 5}, ["A"], "Influence 4.00%"), ({"tenant_faction": "A", "entered_factions": [{"faction": "B", "gap_pp": 1}, {"faction": "C", "gap_pp": 2}]}, [], "Alarm details")]
        for facts, names, reason in cases:
            with self.subTest(facts=facts):
                actual = context(self.alert(facts))
                self.assertEqual(actual[:2], (names, reason))

    def test_resolved_critical_escape_links_and_embed_limits(self):
        alert = self.alert({"tenant_faction": "@everyone **x**" * 100, "competitor": "B" * 1000, "gap_pp": 1}, resolved_at="now", severity="critical")
        alert.update(system_name="A name & (test)", message="@everyone *warning* " * 1000)
        embed = notification(alert)["embeds"][0]
        self.assertIn("RESOLVED", embed["description"])
        self.assertEqual(embed["color"], 0xE65C72)
        self.assertNotIn("@everyone", embed["description"])
        self.assertIn("A%20name%20%26%20%28test%29", str(embed["fields"]))
        total = len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
        for field in embed["fields"]:
            self.assertLessEqual(len(field["name"]), 256)
            self.assertLessEqual(len(field["value"]), 1024)
            total += len(field["name"]) + len(field["value"])
        self.assertLessEqual(total, 6000)


if __name__ == "__main__":
    unittest.main()
