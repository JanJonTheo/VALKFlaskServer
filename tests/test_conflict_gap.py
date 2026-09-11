import unittest
import json
from sqlalchemy import create_engine, text
from bgs_rule_scheduler import evaluate_rule, _gap_conflict_opponents, _resolve_inactive_alerts
from repair_conflict_gaps import redundant


class Fixtures:
    @staticmethod
    def tenant_rule(condition):
        return {'condition_type': condition['type'], 'condition_json': json.dumps(condition), 'threshold_pp': condition.get('threshold_pp', 1)}

    @staticmethod
    def tenant_snapshot(tick, influence, rival):
        return {'ticktime': tick, 'payload_json': {'SystemFaction': {'Name': 'Controller'}, 'Factions': [
            {'Name': 'Controller', 'Influence': .5}, {'Name': 'Test Faction', 'Influence': influence},
            {'Name': 'Rival', 'Influence': rival}], 'Conflicts': []}}


class ConflictGapTest(unittest.TestCase):
    def setUp(self):
        self.rule = Fixtures.tenant_rule({'type': 'tenant_faction_gap', 'threshold_pp': 2})
        self.previous = Fixtures.tenant_snapshot('2026-09-10T12:00:00Z', .10, .20)
        self.current = Fixtures.tenant_snapshot('2026-09-11T12:00:00Z', .10, .10)

    def conflict(self, kind='War', status='pending', opponent='Rival'):
        return {'Faction1': {'Name': 'Test Faction'}, 'Faction2': {'Name': opponent}, 'WarType': kind, 'Status': status}

    def evaluate(self):
        return evaluate_rule(self.rule, [self.current, self.previous], 'Test Faction')

    def test_conflict_types_and_statuses_suppress_only_pair(self):
        for kind in ('War', '$Election;', '$CivilWar;', 'civil_war'):
            for status in ('pending', 'active', '$FactionWarPending;', '$FactionWarActive;', ''):
                with self.subTest(kind=kind, status=status):
                    self.current['payload_json']['Conflicts'] = [self.conflict(kind, status)]
                    result = self.evaluate()
                    self.assertFalse(result['active'])
                    self.assertEqual(result['events'], [])
                    self.assertEqual(result['active_event_keys'], [])

    def test_third_party_warning_survives(self):
        self.current['payload_json']['Conflicts'] = [self.conflict()]
        self.current['payload_json']['Factions'].append({'Name': 'Other', 'Influence': .11})
        self.previous['payload_json']['Factions'].append({'Name': 'Other', 'Influence': .30})
        self.assertEqual(self.evaluate()['events'], ['gap:other'])

    def test_unrelated_conflict_does_not_suppress(self):
        self.current['payload_json']['Conflicts'] = [self.conflict(opponent='Other')]
        self.assertEqual(self.evaluate()['events'], ['gap:rival'])

    def test_ended_conflict_does_not_suppress(self):
        for status in ('won', 'lost', '$FactionWarWon;', 'finished'):
            self.current['payload_json']['Conflicts'] = [self.conflict(status=status)]
            self.assertEqual(self.evaluate()['events'], ['gap:rival'])

    def test_gap_becomes_eligible_after_conflict_ends(self):
        self.previous['payload_json']['Factions'][2]['Influence'] = .10
        self.previous['payload_json']['Conflicts'] = [self.conflict()]
        self.assertEqual(self.evaluate()['events'], ['gap:rival'])
        self.previous['payload_json']['Conflicts'] = []
        self.assertEqual(self.evaluate()['events'], [])

    def test_case_and_reversed_pair(self):
        p = {'Conflicts': [{'faction1': ' rival ', 'faction2': ' TEST FACTION ', 'war_type': 'war', 'status': 'active'}]}
        self.assertEqual(_gap_conflict_opponents(p, 'Test Faction'), {'rival'})

    def test_controller_gap_suppressed(self):
        self.current['payload_json']['SystemFaction'] = {'Name': 'Test Faction'}
        self.current['payload_json']['Factions'] = self.current['payload_json']['Factions'][1:]
        rule = {'condition_type': 'controller_gap', 'threshold_pp': 2}
        self.assertTrue(evaluate_rule(rule, [self.current])['active'])
        self.current['payload_json']['Conflicts'] = [self.conflict()]
        self.assertFalse(evaluate_rule(rule, [self.current])['active'])

    def test_conflict_alert_is_retained(self):
        self.current['payload_json']['Conflicts'] = [self.conflict()]
        rule = Fixtures.tenant_rule({'type': 'tenant_faction_new_conflict', 'conflict_types': ['war', 'election']})
        result = evaluate_rule(rule, [self.current, self.previous], 'Test Faction')
        self.assertEqual(len(result['events']), 1)
        self.assertFalse(self.evaluate()['active'])

    def test_cleanup_uses_exact_alert_pair(self):
        alert = {'condition_type': 'tenant_faction_gap', 'event_key': 'gap:rival', 'facts_json': json.dumps({
            'tenant_faction': 'Test Faction', 'entered_factions': [{'faction': 'Rival'}]})}
        self.current['payload_json']['Conflicts'] = [self.conflict(opponent='Other')]
        self.assertFalse(redundant(alert, self.current['payload_json']))
        self.current['payload_json']['Conflicts'] = [self.conflict()]
        self.assertTrue(redundant(alert, self.current['payload_json']))

    def test_suppressed_alert_resolves_and_cancels_pending_delivery(self):
        engine = create_engine('sqlite://')
        with engine.begin() as c:
            c.execute(text('CREATE TABLE dashboard_bgs_alert (id TEXT,rule_id TEXT,system_key TEXT,event_key TEXT,fired_ticktime TEXT,facts_json TEXT,resolved_at TEXT)'))
            c.execute(text('CREATE TABLE dashboard_notification_delivery (alert_id TEXT,status TEXT,updated_at TEXT,lease_until TEXT)'))
            c.execute(text("INSERT INTO dashboard_bgs_alert VALUES ('a','r','system','gap:rival','tick','{}',NULL)"))
            for status in ('pending', 'retry', 'sent'):
                c.execute(text("INSERT INTO dashboard_notification_delivery VALUES ('a',:status,NULL,NULL)"), {'status': status})
            self.current['payload_json']['Conflicts'] = [self.conflict()]
            result = self.evaluate()
            self.assertEqual(_resolve_inactive_alerts(c, {**self.rule, 'id': 'r'}, 'system', result, 'now'), 1)
            self.assertEqual(c.execute(text('SELECT resolved_at FROM dashboard_bgs_alert')).scalar_one(), 'now')
            self.assertEqual(c.execute(text('SELECT status FROM dashboard_notification_delivery')).scalars().all(), ['cancelled', 'cancelled', 'sent'])
        engine.dispose()


if __name__ == '__main__': unittest.main()
