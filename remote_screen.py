"""Opt-in ANKe remote sessions. Single-process service, independent of BGS writes.

Only device metadata and ACLs persist; rooms, pairing and commands die on restart.
All runtime transitions use one lock. No media or raw input is stored here.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path

from flask import jsonify, request, g
import jwt

PREFIX = '/api/remote-screen/v1'
ASSISTS = ('FSD Route Assist', 'Supercruise Assist', 'Waypoint Assist',
           'Colonisation Assist', 'RC Build Assist', 'RC Build Assist (Renew Snapshot fc_loading)',
           'Robigo Assist', 'AFK Combat Assist', 'DSS Assist')


class RemoteError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class RemoteStore:
    def __init__(self, path, validate_identity, clock=time.monotonic):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS remote_device '
                        '(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, owner TEXT NOT NULL, '
                        'name TEXT NOT NULL, secret_hash TEXT NOT NULL, grants TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS remote_audit '
                        '(at REAL, tenant TEXT, device TEXT, actor TEXT, action TEXT)')
        self.db.commit()
        self.lock = threading.RLock()
        self.clock, self.validate = clock, validate_identity
        self.pairs, self.runtime = {}, {}

    def audit(self, device, actor, action):
        self.db.execute('INSERT INTO remote_audit VALUES (?,?,?,?,?)',
                        (time.time(), device['tenant'], device['id'], actor, action))
        self.db.execute('DELETE FROM remote_audit WHERE rowid NOT IN '
                        '(SELECT rowid FROM remote_audit ORDER BY rowid DESC LIMIT 10000)')
        self.db.commit()

    def device(self, device_id):
        row = self.db.execute('SELECT * FROM remote_device WHERE id=?', (device_id,)).fetchone()
        if row is None:
            raise RemoteError('Device not found', 404)
        return dict(zip(('id', 'tenant', 'owner', 'name', 'secret_hash', 'grants'),
                        (*row[:5], json.loads(row[5]))))

    def state(self, device_id):
        return self.runtime.setdefault(device_id, dict(room=secrets.token_hex(16), viewers={},
            lease=None, commands={}, last_seen=-999, status={}, publisher_identity='', token=None))

    def allowed(self, d, identity, right='view'):
        if d['tenant'] != identity['tenant_id']:
            return False
        return (d['owner'] == identity['sub'] or
                right in d['grants'].get(identity['sub'], []))

    def require(self, d, identity, right='view'):
        if not self.allowed(d, identity, right):
            raise RemoteError('Device access denied', 403)

    def rotate(self, s):
        s['room'], s['token'], s['lease'] = secrets.token_hex(16), None, None
        for command in s['commands'].values():
            if command['state'] == 'pending':
                command.update(state='expired', result={'error': 'Session changed'})

    def prune(self, d):
        s, now = self.state(d['id']), self.clock()
        expired = []
        for sid, v in s['viewers'].items():
            if v['until'] <= now or not self.allowed(d, v['identity']) or not self.validate(v['identity']):
                expired.append(sid)
        if expired:
            for sid in expired:
                del s['viewers'][sid]
            self.rotate(s)
        lease = s['lease']
        if lease and (lease['until'] <= now or lease['viewer'] not in s['viewers'] or
                      not self.allowed(d, s['viewers'][lease['viewer']]['identity'], 'control')):
            s['lease'] = None
        for key, cmd in list(s['commands'].items()):
            if cmd['expires'] <= now and cmd['state'] == 'pending':
                cmd.update(state='expired', result={'error': 'Command expired'})
            if cmd['expires'] + 120 < now:
                del s['commands'][key]
        return s

    def pairing(self, identity):
        now = self.clock()
        self.pairs = {k: v for k, v in self.pairs.items() if v['until'] > now}
        if len(self.pairs) >= 100:
            raise RemoteError('Too many pending pairings', 429)
        code = secrets.token_urlsafe(24)
        self.pairs[digest(code)] = dict(identity=dict(identity), until=now + 600)
        return {'code': code, 'expires_in': 600}

    def pair(self, code, name):
        pair = self.pairs.get(digest(code))
        if not pair or pair['until'] <= self.clock() or not self.validate(pair['identity']):
            raise RemoteError('Pairing code is invalid or expired', 401)
        identity = pair['identity']
        count = self.db.execute('SELECT COUNT(*) FROM remote_device WHERE tenant=? AND owner=?',
                               (identity['tenant_id'], identity['sub'])).fetchone()[0]
        if count >= 20:
            raise RemoteError('Device limit reached', 409)
        self.pairs.pop(digest(code))
        device_id, secret = secrets.token_hex(16), secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO remote_device VALUES (?,?,?,?,?,?)',
                        (device_id, identity['tenant_id'], identity['sub'], name[:80], digest(secret), '{}'))
        self.db.commit()
        self.audit(self.device(device_id), identity['sub'], 'paired')
        return {'device_id': device_id, 'secret': secret}

    def authenticate_device(self, authorization):
        try:
            scheme, credentials = authorization.split(' ', 1)
            device_id, secret = credentials.split('.', 1)
            if scheme != 'Device':
                raise ValueError()
            d = self.device(device_id)
            if not secrets.compare_digest(d['secret_hash'], digest(secret)):
                raise ValueError()
            return d
        except (ValueError, RemoteError):
            raise RemoteError('Device authentication required', 401) from None

    def public(self, d, identity):
        self.require(d, identity)
        s = self.prune(d)
        return dict(id=d['id'], name=d['name'], owner=d['owner'] == identity['sub'],
            grants=d['grants'] if d['owner'] == identity['sub'] else {},
            can_control=self.allowed(d, identity, 'control'),
            online=self.clock() - s['last_seen'] < 5, status=s['status'],
            operator=s['lease']['user'] if s['lease'] else None)

    def viewer(self, d, identity, viewer_id=None):
        self.require(d, identity)
        s = self.prune(d)
        if self.clock() - s['last_seen'] >= 5 or not s['status'].get('enabled'):
            raise RemoteError('Device is offline or transmission is disabled', 409)
        if viewer_id:
            v = s['viewers'].get(viewer_id)
            if not v or v['identity']['sub'] != identity['sub'] or v['identity'].get('sid') != identity.get('sid'):
                raise RemoteError('Viewer session expired', 409)
        else:
            if len(s['viewers']) >= 3:
                raise RemoteError('Three viewers are already connected', 409)
            viewer_id = secrets.token_hex(16)
            v = s['viewers'][viewer_id] = dict(identity=dict(identity), participant='viewer-' + viewer_id)
        v['until'] = self.clock() + 5
        return s, viewer_id, v

    def media_token(self, room, identity, publisher=False, control=False):
        key, secret, url = (os.getenv(k) for k in ('REMOTE_LIVEKIT_KEY', 'REMOTE_LIVEKIT_SECRET', 'REMOTE_LIVEKIT_URL'))
        if not key or not secret or not url or not url.startswith('wss://'):
            raise RemoteError('Remote media server is not configured', 503)
        now = int(time.time())
        token = jwt.encode(dict(iss=key, sub=identity, nbf=now - 5, exp=now + 60,
            video=dict(roomJoin=True, room=room, canPublish=publisher, canSubscribe=not publisher,
                       canPublishData=publisher or control,
                       canPublishSources=['screen_share'] if publisher else [])), secret, algorithm='HS256')
        return dict(url=url, token=token, room=room, identity=identity)

    def policy(self, d, body):
        s = self.prune(d)
        now = self.clock()
        active = sum(now - x['last_seen'] < 5 and x['status'].get('enabled',False) for x in self.runtime.values())
        wants_video = isinstance(body.get('status'),dict) and body['status'].get('enabled') is True
        if wants_video and (now - s['last_seen'] >= 5 or not s['status'].get('enabled')) and active >= 5:
            raise RemoteError('Five clients are already active', 409)
        boot = str(body.get('boot_id', ''))[:80]
        if boot != s.get('boot_id'):
            if s.get('boot_id') and now - s['last_seen'] < 5:
                raise RemoteError('This device is already active in another ANKe process', 409)
            self.rotate(s)
            s['boot_id'] = boot
        s['last_seen'] = now
        status = body.get('status', {})
        if not isinstance(status, dict):
            raise RemoteError('Invalid status')
        s['status'] = {k: status.get(k) for k in ('commander', 'fid', 'assist', 'capture', 'frame_at', 'manual_ready')}
        s['status'].update(enabled=status.get('enabled') is True, control_enabled=status.get('control_enabled') is True)
        if not s['status']['enabled']:
            s['viewers'].clear()
            self.rotate(s)
        if not s['status']['control_enabled']:
            s['lease'] = None
        rejected = body.get('reject_lease')
        if s['lease'] and rejected == s['lease']['epoch']:
            s['lease'] = None
        results = body.get('results', {})
        if not isinstance(results, dict) or len(results) > 32:
            raise RemoteError('Invalid results')
        for key, result in results.items():
            cmd = s['commands'].get(key)
            if cmd and cmd['state'] == 'pending' and isinstance(result, dict):
                cmd.update(state='failed' if 'error' in result else 'completed', result=result)
        media = None
        if s['viewers']:
            s['publisher_identity'] = 'device-' + d['id']
            media = self.media_token(s['room'], s['publisher_identity'], publisher=True)
        lease = s['lease']
        public_lease = None if not lease else {**lease, 'remaining': max(0, lease['until'] - now)}
        commands = [{**c, 'remaining': c['expires'] - now} for c in s['commands'].values()
                    if c['state'] == 'pending' and (c['action'] == 'stop' or
                    (lease and c['epoch'] == lease['epoch']))]
        return dict(media=media, lease=public_lease, commands=commands,
                    profile='detail' if any(v.get('profile') == 'detail' for v in s['viewers'].values()) else 'standard',
                    viewers=[v['participant'] for v in s['viewers'].values()], valid_for=5, server_time=time.time())


def register_remote_screen_routes(app, db, require_api_key, tenants, bind_tenant):
    from dashboard_auth import tenant_for_claim
    from dashboard_users import validate_dashboard_identity

    def validate(identity):
        try:
            tenant = tenant_for_claim(tenants, identity.get('tenant_id'))
            if not tenant:
                return False
            bind_tenant(tenant)
            user = validate_dashboard_identity(db.session, identity, require_session=True)
            return not user.get('must_change_password', False)
        except Exception:
            return False

    store = RemoteStore(os.getenv('REMOTE_SCREEN_DATABASE', str(Path(app.instance_path) / 'remote-screen.sqlite')), validate)
    app.extensions['remote_screen'] = store

    def enabled():
        if os.getenv('REMOTE_SCREEN_ENABLED', 'false').lower() != 'true':
            raise RemoteError('Remote screen is disabled on this server', 503)

    def body(limit=65536):
        if request.content_length is None or request.content_length > limit:
            raise RemoteError('Request body missing or exceeds limit', 413)
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise RemoteError('Expected a JSON object')
        return value

    def route(path, methods, authenticated=True):
        def decorator(fn):
            @wraps(fn)
            def wrapped(**kwargs):
                try:
                    enabled()
                    with store.lock:
                        if authenticated:
                            identity = getattr(g, 'dashboard_identity', None)
                            if not identity or not validate(identity):
                                raise RemoteError('A current dashboard session is required', 401)
                            response = jsonify(fn(identity, **kwargs))
                        else:
                            response = jsonify(fn(**kwargs))
                        response.headers['Cache-Control'] = 'no-store'
                        return response
                except RemoteError as exc:
                    store.db.rollback()
                    return jsonify(error={'code': 'REMOTE_SCREEN', 'message': str(exc), 'details':getattr(exc,'details',None)}), exc.status
            if authenticated:
                wrapped = require_api_key(wrapped)
            app.add_url_rule(PREFIX + path, 'remote_' + fn.__name__, wrapped, methods=methods)
            return fn
        return decorator

    from anke_data import register_anke_routes
    data_store = register_anke_routes(store, route, body)
    app.extensions['anke_data'] = data_store

    @route('/pairings', ['POST'])
    def pairing(identity):
        return store.pairing(identity)

    @route('/pair', ['POST'], False)
    def pair():
        data = body()
        return store.pair(str(data.get('code', '')), str(data.get('name', 'ANKe')))

    @route('/devices', ['GET'])
    def devices(identity):
        ids = store.db.execute('SELECT id FROM remote_device WHERE tenant=?', (identity['tenant_id'],)).fetchall()
        return {'devices': [store.public(d, identity) for (key,) in ids
                            if store.allowed(d := store.device(key), identity)] + data_store.archives(identity)}

    @route('/devices/<device_id>/share-candidates', ['GET'])
    def share_candidates(identity, device_id):
        d = store.device(device_id)
        store.require(d, identity)
        if d['owner'] != identity['sub']:
            raise RemoteError('Only the owner can share a device', 403)
        from sqlalchemy import text
        q = request.args.get('q', '').strip()[:80]
        if len(q) < 2:
            return {'users': []}
        escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        rows = db.session.execute(text("SELECT id, username FROM users WHERE active=1 "
            "AND username LIKE :query ESCAPE '\\' ORDER BY username LIMIT 25"), {'query': '%' + escaped + '%'}).mappings()
        return {'users': [{'id': str(row['id']), 'name': row['username']} for row in rows]}

    @route('/device/pulse', ['POST'], False)
    def pulse():
        d = store.authenticate_device(request.headers.get('Authorization', ''))
        return store.policy(d, body())

    @route('/devices/<device_id>/grants', ['PUT'])
    def grants(identity, device_id):
        d = store.device(device_id)
        store.require(d, identity)
        if d['owner'] != identity['sub']:
            raise RemoteError('Only the owner can share a device', 403)
        data = body()
        user_id, rights = str(data.get('user_id', '')), data.get('rights', [])
        if not isinstance(rights, list) or any(x not in ('view', 'control') for x in rights):
            raise RemoteError('Invalid rights')
        # canonical_user only searches the currently bound tenant's database.
        from dashboard_users import canonical_user
        user = canonical_user(db.session, user_id)
        if not user:
            raise RemoteError('Unknown tenant user')
        user_id = str(user['id'])
        if 'control' in rights and 'view' not in rights:
            rights.append('view')
        if rights:
            d['grants'][user_id] = sorted(set(rights))
        else:
            d['grants'].pop(user_id, None)
        store.db.execute('UPDATE remote_device SET grants=? WHERE id=?', (json.dumps(d['grants']), device_id))
        store.audit(d, identity['sub'], 'grants_changed')
        store.rotate(store.state(device_id))
        store.prune(d)
        return {'ok': True}

    @route('/devices/<device_id>', ['DELETE'])
    def unpair(identity, device_id):
        d = store.device(device_id)
        store.require(d, identity)
        if d['owner'] != identity['sub']:
            raise RemoteError('Only the owner can unpair', 403)
        data_store.unpair(d)
        store.audit(d, identity['sub'], 'unpaired')
        store.db.execute('DELETE FROM remote_device WHERE id=?', (device_id,))
        store.db.commit()
        store.runtime.pop(device_id, None)
        return {'ok': True}

    @route('/devices/<device_id>/viewers', ['POST'])
    def join(identity, device_id):
        d = store.device(device_id)
        s, sid, v = store.viewer(d, identity)
        return dict(viewer_id=sid, media=store.media_token(s['room'], v['participant'],
                    control=store.allowed(d, identity, 'control')))

    @route('/devices/<device_id>/viewers/<viewer_id>', ['POST', 'DELETE'])
    def viewer_pulse(identity, device_id, viewer_id):
        d = store.device(device_id)
        s, sid, v = store.viewer(d, identity, viewer_id)
        if request.method == 'DELETE':
            del s['viewers'][sid]
            store.rotate(s)
            return {'ok': True}
        data = body()
        v['profile'] = 'detail' if data.get('profile') == 'detail' else 'standard'
        lease = s['lease']
        if lease and lease['viewer'] == sid:
            if data.get('control') is True:
                lease['until'] = store.clock() + 3
            # Release is explicit (DELETE). A viewing heartbeat that was already
            # in flight when control was acquired must not cancel the new lease.
        return dict(media=store.media_token(s['room'], v['participant'], control=store.allowed(d, identity, 'control')),
                    lease=s['lease'], status=s['status'], server_time=time.time())

    @route('/devices/<device_id>/viewers/<viewer_id>/control', ['POST', 'DELETE'])
    def control(identity, device_id, viewer_id):
        d = store.device(device_id)
        store.require(d, identity, 'control')
        s, sid, v = store.viewer(d, identity, viewer_id)
        if request.method == 'DELETE':
            if s['lease'] and s['lease']['viewer'] == sid:
                s['lease'] = None
            return {'ok': True}
        if not s['status'].get('control_enabled'):
            raise RemoteError('Remote control is disabled locally', 403)
        mode = body().get('mode')
        if mode not in ('manual', 'assist'):
            raise RemoteError('Invalid control mode')
        if s['lease'] and s['lease']['viewer'] != sid:
            raise RemoteError('Another operator holds control', 409)
        s['lease'] = dict(viewer=sid, participant=v['participant'], user=identity['sub'],
                          epoch=secrets.token_hex(16), mode=mode, until=store.clock() + 3)
        store.audit(d, identity['sub'], 'control_' + mode)
        return {'lease': s['lease']}

    @route('/devices/<device_id>/commands', ['POST'])
    def command(identity, device_id):
        d = store.device(device_id)
        store.require(d, identity, 'control')
        s = store.prune(d)
        if store.clock() - s['last_seen'] >= 5 or not s['status'].get('enabled') or not s['status'].get('control_enabled'):
            raise RemoteError('Device is unavailable for control', 409)
        data, lease = body(), s['lease']
        action = data.get('action')
        if action not in ('prepare', 'confirm', 'stop'):
            raise RemoteError('Unknown action')
        if action != 'stop' and (not lease or lease['user'] != identity['sub'] or
                lease['viewer'] != data.get('viewer_id') or lease['mode'] != 'assist' or
                s['viewers'][lease['viewer']]['identity'].get('sid') != identity.get('sid')):
            raise RemoteError('Assist control lease required', 409)
        if action == 'prepare' and data.get('assist') not in ASSISTS:
            raise RemoteError('Unknown assist')
        if action == 'stop':
            s['lease'] = None
            for cmd in s['commands'].values():
                if cmd['state'] == 'pending':
                    cmd['state'] = 'expired'
        if sum(c['state'] == 'pending' for c in s['commands'].values()) >= 8:
            raise RemoteError('Too many pending commands', 429)
        key = secrets.token_hex(16)
        cmd = dict(id=key, action=action, assist=data.get('assist'), review_id=data.get('review_id'),
                   checks=data.get('checks', []), epoch=lease['epoch'] if lease else '',
                   expires=store.clock() + (3 if action == 'stop' else 30), state='pending', result=None)
        s['commands'][key] = cmd
        store.audit(d, identity['sub'], action)
        return {'command_id': key}

    @route('/devices/<device_id>/commands/<command_id>', ['GET'])
    def result(identity, device_id, command_id):
        d = store.device(device_id)
        store.require(d, identity, 'control')
        cmd = store.prune(d)['commands'].get(command_id)
        if not cmd:
            raise RemoteError('Command not found', 404)
        return {'state': cmd['state'], 'result': cmd['result']}
