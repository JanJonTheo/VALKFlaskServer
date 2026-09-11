"""Preview/delete redundant gap alerts with an SQLite backup and identity tombstones."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from sqlalchemy.engine import make_url
from bgs_rule_scheduler import _gap_conflict_opponents


def alert_pair(alert):
    facts = json.loads(alert['facts_json'])
    if alert['condition_type'] == 'controller_gap':
        return facts.get('controlling_faction'), facts.get('competitor')
    faction = facts.get('tenant_faction') or facts.get('monitored_faction')
    entries = facts.get('entered_factions') or []
    if len(entries) == 1:
        return faction, entries[0].get('faction')
    key = alert['event_key'] or ''
    return faction, key[4:] if key.startswith('gap:') else None


def redundant(alert, snapshot):
    faction, rival = alert_pair(alert)
    return bool(faction and rival and rival.casefold() in _gap_conflict_opponents(snapshot, faction))


def runtime():
    pid = int(subprocess.check_output(['systemctl', 'show', 'valkflask', '--property=MainPID', '--value'], text=True))
    cwd = Path(f'/proc/{pid}/cwd').resolve()
    env = dict(item.split('=', 1) for item in Path(f'/proc/{pid}/environ').read_bytes().decode().split('\0') if '=' in item)
    from dotenv import dotenv_values
    values = {**dotenv_values(cwd / '.env'), **env}
    os.chdir(cwd)
    return Path(make_url(values.get('SNAPSHOT_DB_URL', 'sqlite:///db/bgs_eddn_snapshots.db')).database).resolve()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    snapshot_path = runtime()
    print(json.dumps({'snapshot_database': str(snapshot_path)}))
    snapshots = sqlite3.connect(f'file:{snapshot_path}?mode=ro', uri=True)
    raw = json.loads(Path('/home/valk/valk/tenant.json').read_text())
    tenants = raw.get('tenants', raw) if isinstance(raw, dict) else raw
    if isinstance(tenants, dict): tenants = list(tenants.values())
    backup = Path('/home/valk/dashboard-v2/shared') / ('conflict-gap-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    if args.apply: backup.mkdir(mode=0o700)
    for i, tenant in enumerate(tenants):
        path = Path(make_url(tenant['db_uri']).database).resolve()
        c = sqlite3.connect(f'file:{path}?mode={"rw" if args.apply else "ro"}', uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT a.*, r.condition_type FROM dashboard_bgs_alert a LEFT JOIN dashboard_bgs_rule r ON r.id=a.rule_id WHERE r.condition_type IN ('tenant_faction_gap','controller_gap')").fetchall()
        candidates = []
        for alert in rows:
            historical = snapshots.execute('SELECT payload_json FROM system_tick_snapshot WHERE system_name=? AND ticktime=? AND is_settled=1', (alert['system_name'], alert['fired_ticktime'])).fetchone()
            latest = snapshots.execute('SELECT payload_json FROM system_tick_snapshot WHERE system_name=? AND is_settled=1 ORDER BY ticktime DESC LIMIT 1', (alert['system_name'],)).fetchone() if alert['resolved_at'] is None else None
            if historical and redundant(alert, json.loads(historical[0])):
                reason = 'conflict_at_trigger'
            elif latest and redundant(alert, json.loads(latest[0])):
                reason = 'active_gap_now_in_conflict'
            else:
                continue
            candidates.append({'id': alert['id'], 'system': alert['system_name'], 'pair': alert_pair(alert), 'reason': reason})
        if args.apply and candidates:
            target = backup / f'tenant-{i}.db'
            with sqlite3.connect(target) as dest: c.backup(dest)
            target.chmod(0o600)
            assert c.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN ('bgs_alert_delete_cleanup','bgs_alert_suppress_deleted')").fetchone()[0] == 2
            with c:
                for item in candidates:
                    c.execute('DELETE FROM dashboard_bgs_alert WHERE id=?', (item['id'],))
                    assert not c.execute('SELECT 1 FROM dashboard_notification_delivery WHERE alert_id=?', (item['id'],)).fetchone()
                    assert not c.execute('SELECT 1 FROM dashboard_bgs_alert_user_state WHERE alert_id=?', (item['id'],)).fetchone()
            assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        report = {'tenant': tenant['name'], 'gap_alerts_before': len(rows), 'deleted' if args.apply else 'eligible': len(candidates), 'candidates': candidates}
        print(json.dumps(report))
        if args.apply:
            (backup / f'tenant-{i}-report.json').write_text(json.dumps(report, indent=2))
        c.close()
    if args.apply: print(json.dumps({'backup': str(backup)}))


if __name__ == '__main__': main()
