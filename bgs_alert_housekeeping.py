"""Tenant-local alert retention and explicit lifecycle actions."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

RETENTION_DAYS = 10
BATCH_SIZE = 250


def ensure_housekeeping_schema(conn):
    conn.execute(text('''CREATE TABLE IF NOT EXISTS dashboard_bgs_alert_tombstone (
        rule_id TEXT NOT NULL, system_key TEXT NOT NULL, event_key TEXT NOT NULL,
        fired_ticktime TEXT NOT NULL,
        PRIMARY KEY(rule_id, system_key, event_key, fired_ticktime),
        FOREIGN KEY(rule_id) REFERENCES dashboard_bgs_rule(id) ON DELETE CASCADE)'''))
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_bgs_alert_resolved_at ON dashboard_bgs_alert(resolved_at)'))
    # Triggers make deletion and suppression atomic even across scheduler processes.
    conn.execute(text('''CREATE TRIGGER IF NOT EXISTS bgs_alert_delete_cleanup
        BEFORE DELETE ON dashboard_bgs_alert BEGIN
        INSERT OR IGNORE INTO dashboard_bgs_alert_tombstone
        SELECT OLD.rule_id, OLD.system_key, OLD.event_key, OLD.fired_ticktime
        WHERE OLD.rule_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM dashboard_bgs_rule WHERE id=OLD.rule_id);
        DELETE FROM dashboard_bgs_alert_user_state WHERE alert_id=OLD.id;
        DELETE FROM dashboard_notification_delivery WHERE alert_id=OLD.id;
        END'''))
    conn.execute(text('''CREATE TRIGGER IF NOT EXISTS bgs_alert_suppress_deleted
        BEFORE INSERT ON dashboard_bgs_alert
        WHEN EXISTS (SELECT 1 FROM dashboard_bgs_alert_tombstone
        WHERE rule_id=NEW.rule_id AND system_key=NEW.system_key
        AND event_key=NEW.event_key AND fired_ticktime=NEW.fired_ticktime)
        BEGIN SELECT RAISE(IGNORE); END'''))
    conn.execute(text('''CREATE TRIGGER IF NOT EXISTS bgs_rule_delete_tombstones
        AFTER DELETE ON dashboard_bgs_rule BEGIN
        DELETE FROM dashboard_bgs_alert_tombstone WHERE rule_id=OLD.id; END'''))


def can_manage_alert(alert, user_id, role):
    return (alert['owner_scope'] == 'personal' and alert['owner_user_id'] == user_id) or (
        alert['owner_scope'] == 'tenant' and role == 'admin')


def resolve_alert(conn, alert_id, now):
    conn.execute(text('UPDATE dashboard_bgs_alert SET resolved_at=:now '
                      'WHERE id=:id AND resolved_at IS NULL'), {'now': now, 'id': alert_id})
    conn.execute(text("UPDATE dashboard_notification_delivery SET status='cancelled', "
                      "updated_at=:now, lease_until=NULL WHERE alert_id=:id "
                      "AND status IN ('pending','retry')"), {'now': now, 'id': alert_id})


def cleanup_batch(conn, now=None, preview=False):
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
    # julianday accepts both legacy Z and offset timestamps. Invalid dates stay intact.
    predicate = 'resolved_at IS NOT NULL AND julianday(resolved_at) <= julianday(:cutoff)'
    if preview:
        return conn.execute(text('SELECT count(*) FROM dashboard_bgs_alert WHERE ' + predicate),
                            {'cutoff': cutoff}).scalar_one()
    result = conn.execute(text('DELETE FROM dashboard_bgs_alert WHERE id IN ('
        'SELECT id FROM dashboard_bgs_alert WHERE ' + predicate +
        ' ORDER BY resolved_at LIMIT :limit)'), {'cutoff': cutoff, 'limit': BATCH_SIZE})
    return result.rowcount
