import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text

import fac_shoutout_scheduler


class DiscordInfluenceSummaryTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        timestamp = yesterday.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE event (
                    id INTEGER PRIMARY KEY,
                    cmdr TEXT,
                    timestamp TEXT
                )
            """))
            connection.execute(text("""
                CREATE TABLE mission_completed_event (
                    id INTEGER PRIMARY KEY,
                    event_id INTEGER NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE mission_completed_influence (
                    id INTEGER PRIMARY KEY,
                    mission_id INTEGER NOT NULL,
                    event_id INTEGER,
                    faction_name TEXT,
                    influence TEXT
                )
            """))
            connection.execute(
                text("INSERT INTO event(id, cmdr, timestamp) VALUES(1, 'Alpha Cmdr', :timestamp)"),
                {"timestamp": timestamp},
            )
            connection.execute(
                text("INSERT INTO event(id, cmdr, timestamp) VALUES(2, 'EIC Cmdr', :timestamp)"),
                {"timestamp": timestamp},
            )
            connection.execute(text("""
                INSERT INTO mission_completed_event(id, event_id)
                VALUES(101, 1), (102, 2)
            """))
            connection.execute(text("""
                INSERT INTO mission_completed_influence(
                    id, mission_id, event_id, faction_name, influence
                ) VALUES
                    (201, 101, 1, 'Faction Alpha', '++'),
                    (202, 102, 2, 'East India Company', '+++')
            """))

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _influence_eic_section(message):
        return message.split("**📊 Influence EIC**", 1)[1].split("**📊", 1)[0]

    def test_scheduled_summary_uses_each_tenants_faction(self):
        tenants = [
            {
                "name": "Tenant Alpha",
                "faction_name": "Faction Alpha",
                "db_uri": "sqlite://",
                "discord_webhooks": {"shoutout": "https://example.invalid/alpha"},
            },
            {
                "name": "Tenant EIC",
                "faction_name": "East India Company",
                "db_uri": "sqlite://",
                "discord_webhooks": {"shoutout": "https://example.invalid/eic"},
            },
        ]
        response = Mock(status_code=204, text="")

        with (
            patch.object(fac_shoutout_scheduler, "init_logger", return_value=Mock()),
            patch.object(fac_shoutout_scheduler, "get_tenants", return_value=tenants),
            patch.object(fac_shoutout_scheduler, "get_engine_for_tenant", return_value=self.engine),
            patch.object(fac_shoutout_scheduler.requests, "post", return_value=response) as post,
        ):
            fac_shoutout_scheduler.format_discord_summary()

        self.assertEqual(post.call_count, 2)
        alpha_section = self._influence_eic_section(post.call_args_list[0].kwargs["json"]["content"])
        eic_section = self._influence_eic_section(post.call_args_list[1].kwargs["json"]["content"])
        self.assertIn("Alpha Cmdr", alpha_section)
        self.assertNotIn("EIC Cmdr", alpha_section)
        self.assertIn("EIC Cmdr", eic_section)
        self.assertNotIn("Alpha Cmdr", eic_section)

    def test_manual_summary_only_processes_the_requested_tenant(self):
        tenant = {
            "name": "Tenant Alpha",
            "faction_name": "Faction Alpha",
            "db_uri": "sqlite://",
            "discord_webhooks": {"shoutout": "https://example.invalid/alpha"},
        }
        response = Mock(status_code=204, text="")

        with (
            patch.object(fac_shoutout_scheduler, "init_logger", return_value=Mock()),
            patch.object(fac_shoutout_scheduler, "get_tenants") as get_tenants,
            patch.object(fac_shoutout_scheduler, "get_engine_for_tenant", return_value=self.engine),
            patch.object(fac_shoutout_scheduler.requests, "post", return_value=response) as post,
        ):
            fac_shoutout_scheduler.format_discord_summary(tenant=tenant)

        get_tenants.assert_not_called()
        post.assert_called_once()
        section = self._influence_eic_section(post.call_args.kwargs["json"]["content"])
        self.assertIn("Alpha Cmdr", section)
        self.assertNotIn("EIC Cmdr", section)


if __name__ == "__main__":
    unittest.main()
