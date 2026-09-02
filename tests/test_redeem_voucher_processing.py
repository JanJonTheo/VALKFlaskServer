import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker

from bgs_v3_bucket_eval import evaluate_bounty_bucket
from databases import backfill_redeem_voucher_details
from models import Event, RedeemVoucherEvent, db


class RedeemVoucherProcessingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmpdir.name) / "tenant.sqlite"
        self.engine = create_engine(
            f"sqlite:///{self.db_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        db.Model.metadata.create_all(bind=self.engine)
        self.session = scoped_session(sessionmaker(bind=self.engine))
        self.facade = SimpleNamespace(session=self.session)

    def tearDown(self):
        self.session.remove()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _add_bounty(self, timestamp, tickid, faction, amount, *, raw_json=None):
        event = Event(
            event="RedeemVoucher",
            timestamp=timestamp,
            tickid=tickid,
            ticktime=timestamp,
            cmdr="Test Cmdr",
            starsystem="Test System",
            systemaddress=42,
            raw_json=raw_json or "{}",
        )
        self.session.add(event)
        self.session.flush()
        self.session.add(RedeemVoucherEvent(
            event_id=event.id,
            amount=amount,
            type="bounty",
            factions=json.dumps([{"Faction": faction, "Amount": amount}]),
            starsystem="Test System",
            systemaddress=42,
        ))
        return event

    def test_current_and_last_tick_are_not_evaluated_as_all_time(self):
        self._add_bounty("2026-08-30T10:00:00Z", "old-tick", "Old Faction", 1_000_000)
        self._add_bounty("2026-09-01T10:00:00Z", "current-tick", "Current Faction", 2_000_000)
        self.session.commit()

        current = evaluate_bounty_bucket(self.facade, period="ct", enrich_population=False)
        previous = evaluate_bounty_bucket(self.facade, period="lt", enrich_population=False)

        self.assertEqual([(row["faction"], row["bounty_credits"]) for row in current], [("Current Faction", 2_000_000)])
        self.assertEqual([(row["faction"], row["bounty_credits"]) for row in previous], [("Old Faction", 1_000_000)])

    def test_backfill_repairs_only_losslessly_recoverable_details(self):
        from_list = self._add_bounty(
            "2026-09-01T10:00:00Z",
            "tick-a",
            "From List",
            100,
            raw_json=json.dumps({
                "event": "RedeemVoucher",
                "Type": "bounty",
                "Amount": 100,
                "Factions": [{"Faction": "From List", "Amount": 100}],
            }),
        )
        list_detail = self.session.query(RedeemVoucherEvent).filter_by(event_id=from_list.id).one()
        list_detail.factions = None

        from_singular = self._add_bounty(
            "2026-09-01T11:00:00Z",
            "tick-a",
            "From Singular",
            200,
            raw_json=str({
                "event": "RedeemVoucher",
                "Type": "bounty",
                "Amount": 200,
                "Faction": "From Singular",
            }),
        )
        singular_detail = self.session.query(RedeemVoucherEvent).filter_by(event_id=from_singular.id).one()
        singular_detail.faction = "From Singular"
        singular_detail.factions = None

        from_blank_raw = self._add_bounty(
            "2026-09-01T11:30:00Z",
            "tick-a",
            "Recovered Detail",
            250,
            raw_json=json.dumps({
                "event": "RedeemVoucher",
                "Type": "bounty",
                "Amount": 250,
                "Factions": [{"Faction": "", "Amount": 250}],
            }),
        )

        unrecoverable = self._add_bounty(
            "2026-09-01T12:00:00Z",
            "tick-a",
            "Unknown",
            300,
            raw_json=json.dumps({"event": "RedeemVoucher", "Type": "bounty", "Amount": 300}),
        )
        missing_detail = self.session.query(RedeemVoucherEvent).filter_by(event_id=unrecoverable.id).one()
        missing_detail.faction = None
        missing_detail.factions = None
        self.session.commit()

        with self.engine.begin() as connection:
            stats = backfill_redeem_voucher_details(connection, "test")

        rows = {
            row["event_id"]: row
            for row in self.session.execute(text(
                "SELECT event_id, faction, factions FROM redeem_voucher_event"
            )).mappings()
        }
        repaired_raw = self.session.execute(
            text("SELECT raw_json FROM event WHERE id = :id"),
            {"id": from_singular.id},
        ).scalar_one()
        repaired_blank_raw = self.session.execute(
            text("SELECT raw_json FROM event WHERE id = :id"),
            {"id": from_blank_raw.id},
        ).scalar_one()

        self.assertEqual(stats, {"scanned": 4, "factions": 2, "faction": 2, "raw_json": 2})
        self.assertEqual(json.loads(rows[from_list.id]["factions"])[0]["Faction"], "From List")
        self.assertEqual(rows[from_list.id]["faction"], "From List")
        self.assertEqual(json.loads(rows[from_singular.id]["factions"])[0]["Faction"], "From Singular")
        self.assertEqual(json.loads(repaired_raw)["Factions"][0]["Faction"], "From Singular")
        self.assertEqual(rows[from_blank_raw.id]["faction"], "Recovered Detail")
        self.assertEqual(json.loads(repaired_blank_raw)["Factions"][0]["Faction"], "Recovered Detail")
        self.assertIsNone(rows[unrecoverable.id]["faction"])
        self.assertIsNone(rows[unrecoverable.id]["factions"])

    def test_new_databases_include_dashboard_query_indexes(self):
        index_names = {
            index["name"]
            for table_name in (
                "event",
                "redeem_voucher_event",
                "sell_exploration_data_event",
                "multi_sell_exploration_data_event",
            )
            for index in inspect(self.engine).get_indexes(table_name)
        }
        self.assertTrue({
            "idx_event_timestamp",
            "idx_event_tickid_timestamp",
            "idx_redeem_voucher_type_event_id",
            "idx_sell_exploration_event_id",
            "idx_multi_sell_exploration_event_id",
        }.issubset(index_names))


if __name__ == "__main__":
    unittest.main()
