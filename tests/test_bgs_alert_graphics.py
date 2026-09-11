import json
import unittest
from unittest.mock import patch
from bgs_alert_graphics import comparison, render_graphic, message_payload, delivery_request, system_metadata


def row(tick, a, b):
    return {'ticktime': tick, 'payload_json': json.dumps({'SystemFaction': {'Name': 'A'}, 'Population': 1234,
            'Factions': [{'Name': 'A', 'Influence': a}, {'Name': 'B', 'Influence': b}]})}


class GraphicsTests(unittest.TestCase):
    def test_conflict_update_includes_the_matching_pair_score_and_stakes(self):
        before = '2026-09-10T12:00:00Z'
        after = '2026-09-11T12:00:00Z'
        conflicts = [
            {'Faction1': {'Name':'X','Stake':'Wrong port','WonDays':4}, 'Faction2': {'Name':'Y','Stake':'Other wrong port','WonDays':0}, 'WarType':'War','Status':'active'},
            {'Faction1': {'Name':'A','Stake':'Alpha Port','WonDays':2}, 'Faction2': {'Name':'B','Stake':'Beta Port','WonDays':1}, 'WarType':'War','Status':'active'},
        ]
        rows = [row(before,.2,.2),row(after,.2,.2)]
        for item in rows:
            payload=json.loads(item['payload_json']); payload['Conflicts']=conflicts; item['payload_json']=json.dumps(payload)
        alert = {'system_name':'Test','fired_ticktime':after,'fired_at':after,'message':'Conflict update','event_key':'conflict:war:a:b','facts': {
            'ticktime':after,'baseline_ticktime':before,'new_conflicts':[{'faction1':'A','faction2':'B','type':'war','is_update':True}]}}
        data=comparison(alert,rows,'tenant_faction_new_conflict')
        self.assertEqual(data['kind'],'conflict')
        self.assertTrue(data['reason'].startswith('Conflict update'))
        payload=message_payload(alert,data,has_image=False)
        fields=payload['embeds'][-1]['fields']
        detail=next(f['value'] for f in fields if f['name']=='Alert Snapshot')
        self.assertIn('2 won days',detail)
        self.assertIn('1 won days',detail)
        self.assertIn('Alpha Port',detail)
        self.assertIn('Beta Port',detail)
        self.assertNotIn('Wrong port',detail)

    def test_compact_single_faction_and_combined_links(self):
        import struct
        data = comparison(self.alert, self.rows)
        png = render_graphic(data)
        self.assertEqual(struct.unpack('>II', png[16:24]), (1000, 240))
        payload = message_payload(self.alert, data)
        fields = payload['embeds'][1]['fields']
        links = [f for f in fields if f['name'] == 'System/Map Links']
        self.assertEqual(len(links), 1)
        self.assertNotIn('\n', links[0]['value'])
        for name in ['RC', 'Inara', 'Spansh', 'EDGIS']:
            self.assertIn('[' + name + ']', links[0]['value'])
        self.assertIn('System Data', [f['name'] for f in fields])
        self.assertFalse(any('snapshot' in f['name'] for f in fields))

    def test_missing_metadata_uses_labelled_current_source_without_changing_comparison(self):
        data = comparison(self.alert, self.rows)
        original = json.dumps(data, sort_keys=True)
        info = {'controlling_faction': 'Current controller', 'allegiance': 'Empire', 'government': 'Corporate',
                'economy': 'Industrial', 'population': 150000000, 'updated_at': '2026-09-09T08:00:00Z'}
        with patch('bgs_discord.current_system', return_value=(info, [])):
            metadata = system_metadata(self.alert, data)
        self.assertEqual(json.dumps(data, sort_keys=True), original)
        payload = message_payload(self.alert, data, metadata=metadata)
        field = next(f for f in payload['embeds'][1]['fields'] if f['name'] == 'System Data')
        for value in ['Current controller', 'Empire', 'Corporate', 'Industrial', '150,000,000', 'Latest available System Data']:
            self.assertIn(value, field['value'])
        self.assertEqual(data['current']['factions'][0]['influence_pp'], 35)

    def test_complete_historical_metadata_and_failed_current_lookup(self):
        data = comparison(self.alert, self.rows)
        data['current']['system'].update(controller='A', allegiance='Empire', government='Corporate', economy='Industrial', population=0)
        with patch('bgs_discord.current_system') as lookup:
            metadata = system_metadata(self.alert, data)
            lookup.assert_not_called()
        self.assertEqual(metadata['population'], 0)
        self.assertEqual(metadata['source'], 'Alert Snapshot')
        data['current']['system']['economy'] = None
        with patch('bgs_discord.current_system', side_effect=RuntimeError('unavailable')):
            metadata = system_metadata(self.alert, data)
        self.assertEqual(metadata['controller'], 'A')

    def setUp(self):
        self.before = '2026-09-01T12:00:00Z'
        self.after = '2026-09-08T12:00:00Z'
        self.alert = {'system_name': 'Test system', 'fired_at': self.after, 'fired_ticktime': self.after, 'message': 'Warning',
                      'facts': {'tenant_faction': 'A', 'ticktime': self.after, 'baseline_ticktime': self.before,
                                'baseline_influence_pp': 40, 'tenant_influence_pp': 35, 'loss_pp': 5, 'threshold_pp': 3}}
        self.rows = [row(self.after, .35, .33), row(self.before, .40, .30)]

    def test_sparse_snapshots_and_frozen_context(self):
        data = comparison(self.alert, self.rows, 'tenant_faction_loss')
        self.assertEqual(data['previous']['factions'][0]['influence_pp'], 40)
        self.assertEqual(data['current']['factions'][0]['influence_pp'], 35)
        self.assertEqual(data['previous']['ticktime'], self.before)
        self.alert['facts']['presentation'] = data
        self.assertEqual(comparison(self.alert, [row('2026-09-10T12:00:00Z', .99, .01)]), data)

    def test_render_all_kinds_and_multipart(self):
        facts = self.alert['facts']
        variants = [facts, {**facts, 'loss_pp': None, 'entered_factions': [{'faction': 'B', 'gap_pp': 2, 'previous_gap_pp': 10}]},
                    {'tenant_faction': 'A', 'tenant_influence_pp': 0, 'threshold_pp': 5},
                    {'strongest_change': {'faction': 'A', 'delta_pp': 5, 'baseline_influence_pp': 30, 'current_influence_pp': 35}},
                    {'new_conflicts': [{'faction1': 'A', 'faction2': 'B', 'type': 'War'}]}, {}]
        for f in variants:
            with self.subTest(facts=f):
                alert = {**self.alert, 'facts': f}
                data = comparison(alert, self.rows)
                self.assertTrue(render_graphic(data).startswith(b'\x89PNG'))
                payload = message_payload(alert, data)
                self.assertTrue(payload['content'].startswith('## Test system\nAlert time: <t:'))
                self.assertEqual(payload['embeds'][0]['image']['url'], 'attachment://bgs-alert.png')
                self.assertEqual(payload['embeds'][1]['fields'][0]['name'], 'Previous Snapshot')
                self.assertEqual(payload['embeds'][1]['fields'][1]['name'], 'Alert Snapshot')
                self.assertEqual(payload['allowed_mentions'], {'parse': []})
        with patch('bgs_alert_graphics.historical_comparison', return_value=comparison(self.alert, self.rows)):
            request = delivery_request(self.alert)
            self.assertEqual(request['files']['files[0]'][2], 'image/png')
            self.assertIn('attachments', json.loads(request['data']['payload_json']))

    def test_renderer_failure_keeps_text_and_missing_is_not_zero(self):
        data = comparison(self.alert)
        self.assertEqual(data['previous']['factions'][0]['influence_pp'], 40)
        with patch('bgs_alert_graphics.historical_comparison', return_value=data), patch('bgs_alert_graphics.render_graphic', side_effect=RuntimeError('test')):
            payload = delivery_request(self.alert)['json']
        self.assertNotIn('attachments', payload)
        self.assertEqual(len(payload['embeds']), 1)
        self.assertIn('40.00%', payload['embeds'][0]['fields'][0]['value'])
        missing = comparison({**self.alert, 'facts': {'tenant_faction': 'A', 'loss_pp': 5}})
        self.assertIsNone(missing['current']['factions'][0]['influence_pp'])

    def test_fact_values_win_over_corrected_snapshots(self):
        data = comparison(self.alert, [row(self.after, .9, .1), row(self.before, .8, .2)])
        self.assertEqual(data['current']['factions'][0]['influence_pp'], 35)
        self.assertEqual(data['previous']['factions'][0]['influence_pp'], 40)

    def test_gap_keeps_recorded_difference_and_does_not_invent_rival(self):
        alert = {**self.alert, 'facts': {**self.alert['facts'], 'loss_pp': None,
                 'entered_factions': [{'faction': 'B', 'gap_pp': 1.87, 'previous_gap_pp': 10}]}}
        data = comparison(alert, self.rows)
        self.assertEqual(data['current_gap_pp'], 1.87)
        self.assertIsNone(data['current']['factions'][1]['influence_pp'])
        self.assertEqual(data['previous']['factions'][1]['influence_pp'], 30)

    def test_nullable_journal_lists_keep_historical_system_data(self):
        rows = [dict(r) for r in self.rows]
        for r in rows:
            payload = json.loads(r['payload_json'])
            payload['Conflicts'] = None
            r['payload_json'] = json.dumps(payload)
        data = comparison(self.alert, rows)
        self.assertEqual(data['current']['system']['population'], 1234)
        self.assertEqual(data['current']['conflicts'], [])
        payload['Factions'] = None
        rows[0]['payload_json'] = json.dumps(payload)
        self.assertEqual(comparison(self.alert, rows)['current']['factions'][0]['influence_pp'], 35)

    def test_long_names_and_payload_limits(self):
        alert = {**self.alert, 'system_name': 'System ' * 40, 'facts': {'tenant_faction': 'Long faction ' * 20, 'loss_pp': 4}}
        data = comparison(alert)
        self.assertTrue(render_graphic(data).startswith(b'\x89PNG'))
        payload = message_payload(alert, data)
        self.assertLessEqual(len(payload['content']), 2000)
        total = 0
        for embed in payload['embeds']:
            total += len(embed.get('title', '')) + len(embed.get('description', '')) + len(embed.get('footer', {}).get('text', ''))
            for field in embed.get('fields', []):
                self.assertLessEqual(len(field['value']), 1024)
                total += len(field['name']) + len(field['value'])
        self.assertLessEqual(total, 6000)


if __name__ == '__main__':
    unittest.main()
