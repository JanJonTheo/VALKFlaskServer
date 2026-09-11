import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from sqlalchemy import text, create_engine
import test_bgs_rules as fixtures
from bgs_alert_housekeeping import cleanup_batch, resolve_alert, ensure_housekeeping_schema
from bgs_rule_scheduler import _evaluate_tenant, cleanup_all_tenants


class LifecycleApiTests(fixtures.RuleApiPermissionTest):
    def test_global_requires_admin_and_resolution_is_idempotent(self):
        self.add_manual_alert()
        for role in ('member', 'lead'):
            headers={'authorization': 'Bearer '+role}
            listed=self.client.get('/api/dashboard/bgs/alerts', headers=headers).get_json()['data'][0]
            self.assertFalse(listed['can_manage'])
            self.assertEqual(self.client.post('/api/dashboard/bgs/alerts/manual/resolve',headers=headers).status_code,403)
            self.assertEqual(self.client.delete('/api/dashboard/bgs/alerts/manual',headers=headers).status_code,403)
        headers={'authorization':'Bearer admin'}
        self.assertEqual(self.client.post('/api/dashboard/bgs/alerts/manual/resolve',headers=headers).status_code,200)
        first=self.session.execute(text("SELECT resolved_at FROM dashboard_bgs_alert")).scalar_one()
        self.assertIsNotNone(first)
        self.client.post('/api/dashboard/bgs/alerts/manual/resolve',headers=headers)
        self.assertEqual(first,self.session.execute(text('SELECT resolved_at FROM dashboard_bgs_alert')).scalar_one())
        self.assertEqual(self.client.delete('/api/dashboard/bgs/alerts/manual',headers=headers).status_code,200)
        self.assertEqual(self.client.delete('/api/dashboard/bgs/alerts/manual',headers=headers).status_code,404)

    def test_personal_owner_only_and_missing_identity(self):
        self.add_manual_alert('personal',1)
        for role in ('lead','admin'):
            headers={'authorization':'Bearer '+role}
            self.assertEqual(self.client.post('/api/dashboard/bgs/alerts/manual/resolve',headers=headers).status_code,404)
            self.assertEqual(self.client.delete('/api/dashboard/bgs/alerts/manual',headers=headers).status_code,404)
        self.assertEqual(self.client.delete('/api/dashboard/bgs/alerts/manual').status_code,401)
        headers={'authorization':'Bearer member'}
        listed=self.client.get('/api/dashboard/bgs/alerts',headers=headers).get_json()['data'][0]
        self.assertTrue(listed['can_manage'])
        self.assertEqual(self.client.delete('/api/dashboard/bgs/alerts/manual',headers=headers).status_code,200)

    def test_retention_boundary_and_cancelled_deliveries(self):
        self.add_manual_alert()
        now=datetime(2026,9,11,12,tzinfo=timezone.utc)
        self.session.execute(text("INSERT INTO dashboard_bgs_alert_user_state(alert_id,user_id,read_at) VALUES ('manual',1,'2026-01-01')"))
        for status in ('pending','retry','processing','delivered'):
            self.session.execute(text("INSERT INTO dashboard_notification_delivery(id,alert_id,channel,destination_key,status,next_attempt_at,created_at,updated_at) VALUES (:s,'manual','tenant_discord',:s,:s,'2000','2000','2000')"),{'s':status})
        resolve_alert(self.session,'manual',(now-timedelta(days=10)).isoformat())
        states=dict(self.session.execute(text('SELECT id,status FROM dashboard_notification_delivery')).all())
        self.assertEqual(states,{'pending':'cancelled','retry':'cancelled','processing':'processing','delivered':'delivered'})
        self.assertEqual(cleanup_batch(self.session,now-timedelta(seconds=1)),0)
        self.assertEqual(cleanup_batch(self.session,now,preview=True),1)
        self.assertEqual(cleanup_batch(self.session,now),1)
        self.assertEqual(self.session.execute(text('SELECT count(*) FROM dashboard_notification_delivery')).scalar_one(),0)
        self.assertEqual(self.session.execute(text('SELECT count(*) FROM dashboard_bgs_alert_user_state')).scalar_one(),0)

    def test_active_invalid_dates_and_newly_resolved_are_retained(self):
        self.add_manual_alert()
        now=datetime.now(timezone.utc)
        for value in (None,'invalid',now.isoformat()):
            self.session.execute(text('UPDATE dashboard_bgs_alert SET resolved_at=:v'),{'v':value})
            self.assertEqual(cleanup_batch(self.session,now),0)

    def test_duplicate_protection_is_atomic_and_rule_local(self):
        rule=self.client.post('/api/dashboard/bgs/rules',headers={'authorization':'Bearer member'},json=self.payload('personal')).get_json()['data']
        self.add_manual_alert('personal',1)
        self.session.execute(text('UPDATE dashboard_bgs_alert SET rule_id=:r'),{'r':rule['id']})
        row=dict(self.session.execute(text('SELECT * FROM dashboard_bgs_alert')).mappings().one())
        self.session.execute(text('DELETE FROM dashboard_bgs_alert'))
        self.assertEqual(self.session.execute(text('SELECT count(*) FROM dashboard_bgs_alert_tombstone')).scalar_one(),1)
        columns=','.join(row)
        values=','.join(':'+c for c in row)
        insert=text('INSERT INTO dashboard_bgs_alert ('+columns+') VALUES ('+values+')')
        self.assertEqual(self.session.execute(insert,row).rowcount,0)
        row['fired_ticktime']='2099-01-01T00:00:00Z'
        self.assertEqual(self.session.execute(insert,row).rowcount,1)
        self.session.execute(text('DELETE FROM dashboard_bgs_rule WHERE id=:r'),{'r':rule['id']})
        self.assertEqual(self.session.execute(text('SELECT count(*) FROM dashboard_bgs_alert_tombstone')).scalar_one(),0)

    def test_cleanup_continues_after_one_tenant_failure(self):
        with patch('bgs_rule_scheduler._engine') as engine, patch('bgs_rule_scheduler.ensure_dashboard_schema',side_effect=[RuntimeError('test'),None]), patch('bgs_rule_scheduler.cleanup_batch',return_value=0) as cleanup:
            cleanup_all_tenants([{'name':'broken','db_uri':'a'},{'name':'healthy','db_uri':'b'}])
            self.assertEqual(engine.call_count,2)
            cleanup.assert_called_once()


if __name__=='__main__': unittest.main()
