import json
from functools import wraps
from types import SimpleNamespace

import jwt
import pytest
from flask import Flask, g, request

from remote_screen import RemoteStore, RemoteError, register_remote_screen_routes, PREFIX


def test_authentication_diagnostics_never_log_keys_and_are_throttled(service,caplog):
    store=service.client.application.extensions['remote_screen']
    secret='must-never-appear-in-authentication-logs'
    for _ in range(2):
        with pytest.raises(RemoteError):store.authenticate_device('Device '+service.device+'.'+secret)
    messages=[r.getMessage() for r in caplog.records if 'authentication rejected' in r.getMessage()]
    assert len(messages)==1
    assert 'credential-mismatch' in messages[0]
    assert secret not in messages[0] and service.device not in messages[0]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('REMOTE_SCREEN_ENABLED', 'true')
    monkeypatch.setenv('REMOTE_SCREEN_DATABASE', str(tmp_path / 'remote.db'))
    monkeypatch.setenv('REMOTE_LIVEKIT_KEY', 'test-key')
    monkeypatch.setenv('REMOTE_LIVEKIT_SECRET', 'test-media-secret-' * 3)
    monkeypatch.setenv('REMOTE_LIVEKIT_URL', 'wss://media.test')
    users = {'owner', 'friend', 'admin'}
    revoked = set()
    def validate(session, identity, require_session=False):
        if identity.get('sid') in revoked or not identity.get('sid') or identity['sub'] not in users:
            raise PermissionError('Revoked')
        return {'must_change_password': False}
    monkeypatch.setattr('dashboard_users.validate_dashboard_identity', validate)
    monkeypatch.setattr('dashboard_users.canonical_user', lambda session, uid: {'id': uid} if uid in users else None)
    app = Flask(__name__)
    def auth(fn):
        @wraps(fn)
        def wrap(**kwargs):
            g.dashboard_identity = json.loads(request.headers['X-Identity']) if 'X-Identity' in request.headers else None
            return fn(**kwargs)
        return wrap
    database = SimpleNamespace(session=None)
    register_remote_screen_routes(app, database, auth,
        [{'id': 'tenant-a'}, {'id': 'tenant-b'}], lambda tenant: None)
    clock = [100.0]
    store = app.extensions['remote_screen']
    store.clock = lambda: clock[0]
    client = app.test_client()
    def call(path, method='GET', who='owner', tenant='tenant-a', **kwargs):
        identity = dict(sub=who, tenant_id=tenant, sid=who + '-session', role='admin' if who == 'admin' else 'member')
        return client.open(PREFIX + path, method=method, headers={'X-Identity': json.dumps(identity)}, **kwargs)
    pair = call('/pairings', 'POST', json={}).json
    credentials = client.post(PREFIX + '/pair', json={'code': pair['code'], 'name': 'Test Elite'}).json
    device_id = credentials['device_id']
    def pulse(**kwargs):
        body = dict(boot_id='boot-1', status={'enabled': True, 'control_enabled': True}, results={})
        body.update(kwargs)
        return client.post(PREFIX + '/device/pulse', json=body,
            headers={'Authorization': 'Device ' + device_id + '.' + credentials['secret']})
    assert pulse().status_code == 200
    return SimpleNamespace(client=client, call=call, store=store, clock=clock, pulse=pulse,
                           device=device_id, credentials=credentials, revoked=revoked, database=database)


def test_pair_code_is_one_time_and_secret_is_hashed(service):
    s = service
    code = s.call('/pairings', 'POST', json={}).json['code']
    assert s.client.post(PREFIX + '/pair', json={'code': code}).status_code == 200
    assert s.client.post(PREFIX + '/pair', json={'code': code}).status_code == 401
    assert s.store.device(s.device)['secret_hash'] != s.credentials['secret']
    code = s.call('/pairings', 'POST', json={}).json['code']
    s.clock[0] += 601
    assert s.client.post(PREFIX + '/pair', json={'code': code}).status_code == 401


def test_no_legacy_admin_or_cross_tenant_access(service):
    s = service
    assert s.client.get(PREFIX + '/devices', headers={'apikey': 'legacy'}).status_code == 401
    for who, tenant in [('admin', 'tenant-a'), ('owner', 'tenant-b')]:
        assert s.call('/devices', who=who, tenant=tenant).json == {'devices': []}
        assert s.call(f'/devices/{s.device}/viewers', 'POST', who=who, tenant=tenant, json={}).status_code == 403


def test_view_only_cannot_control_and_only_owner_can_share(service):
    s = service
    base = '/devices/' + s.device
    assert s.call(base + '/grants', 'PUT', json={'user_id': 'friend', 'rights': ['view']}).status_code == 200
    viewer = s.call(base + '/viewers', 'POST', who='friend', json={}).json
    assert s.call(base + '/viewers/' + viewer['viewer_id'] + '/control', 'POST', who='friend', json={'mode': 'manual'}).status_code == 403
    assert s.call(base + '/grants', 'PUT', who='friend', json={'user_id': 'admin', 'rights': ['control']}).status_code == 403


def test_exclusive_lease_expiry_and_unqueued_start(service):
    s = service
    base = '/devices/' + s.device
    one = s.call(base + '/viewers', 'POST', json={}).json['viewer_id']
    two = s.call(base + '/viewers', 'POST', json={}).json['viewer_id']
    lease = s.call(f'{base}/viewers/{one}/control', 'POST', json={'mode': 'assist'}).json['lease']
    assert s.call(f'{base}/viewers/{one}', 'POST', json={'control': False}).json['lease']['epoch'] == lease['epoch']
    assert s.call(f'{base}/viewers/{two}/control', 'POST', json={'mode': 'manual'}).status_code == 409
    command = s.call(base + '/commands', 'POST', json={'action': 'prepare', 'assist': 'Waypoint Assist', 'viewer_id': one}).json
    assert s.pulse().json['commands'][0]['id'] == command['command_id']
    s.clock[0] += 3.1
    assert s.pulse().json['lease'] is None
    assert s.pulse().json['commands'] == []
    assert s.call(base + '/commands', 'POST', json={'action': 'confirm', 'viewer_id': one}).status_code == 409
    assert lease['epoch']


def test_logout_and_acl_revocation_rotate_media_room(service):
    s = service
    base = '/devices/' + s.device
    s.call(base + '/grants', 'PUT', json={'user_id': 'friend', 'rights': ['view', 'control']})
    joined = s.call(base + '/viewers', 'POST', who='friend', json={}).json
    old_room = joined['media']['room']
    assert s.pulse().json['media']['room'] == old_room
    s.call(base + '/grants', 'PUT', json={'user_id': 'friend', 'rights': []})
    assert s.pulse().json['media'] is None
    assert s.store.state(s.device)['room'] != old_room
    joined = s.call(base + '/viewers', 'POST', json={}).json
    s.revoked.add('owner-session')
    assert s.pulse().json['media'] is None
    assert s.store.state(s.device)['room'] != joined['media']['room']


def test_local_disable_boot_and_stop_invalidate_control(service):
    s = service
    base = '/devices/' + s.device
    sid = s.call(base + '/viewers', 'POST', json={}).json['viewer_id']
    s.call(f'{base}/viewers/{sid}/control', 'POST', json={'mode': 'manual'})
    assert s.pulse(status={'enabled': True, 'control_enabled': False}).json['lease'] is None
    s.pulse()
    s.call(f'{base}/viewers/{sid}/control', 'POST', json={'mode': 'assist'})
    stop = s.call(base + '/commands', 'POST', json={'action': 'stop'}).json
    policy = s.pulse().json
    assert policy['lease'] is None
    assert policy['commands'][0]['id'] == stop['command_id']
    assert s.pulse(boot_id='boot-2').status_code == 409
    s.clock[0] += 5.1
    assert s.pulse(boot_id='boot-2').json['commands'] == []
    s.clock[0] += 5.1
    assert s.pulse(status={'enabled': False}).json['media'] is None


def test_viewer_limit_media_grants_and_restart(service, tmp_path):
    s = service
    base = '/devices/' + s.device
    joined = [s.call(base + '/viewers', 'POST', json={}) for _ in range(3)]
    assert all(x.status_code == 200 for x in joined)
    assert s.call(base + '/viewers', 'POST', json={}).status_code == 409
    claims = jwt.decode(joined[0].json['media']['token'], options={'verify_signature': False})
    assert claims['video']['canPublish'] is False
    assert claims['video']['room'] == joined[0].json['media']['room']
    s.clock[0] += 6
    assert s.pulse().json['media'] is None
    restarted = RemoteStore(str(tmp_path / 'remote.db'), lambda identity: True)
    assert restarted.device(s.device)['owner'] == 'owner'
    assert restarted.state(s.device)['viewers'] == {}
    assert restarted.state(s.device)['room'] != claims['video']['room']


def test_device_token_cannot_impersonate_other_device(service):
    s = service
    bad = s.client.post(PREFIX + '/device/pulse', json={}, headers={'Authorization': 'Device ' + s.device + '.wrong'})
    assert bad.status_code == 401
    assert s.call('/devices/' + s.device, 'DELETE').status_code == 200
    assert s.pulse().status_code == 401


def test_five_active_devices_limit(service):
    s = service
    for i in range(5):
        code = s.call('/pairings', 'POST', json={}).json['code']
        paired = s.client.post(PREFIX + '/pair', json={'code': code}).json
        result = s.client.post(PREFIX + '/device/pulse', json={'boot_id': 'new', 'status': {'enabled': True}},
            headers={'Authorization': 'Device ' + paired['device_id'] + '.' + paired['secret']})
        assert result.status_code == (200 if i < 4 else 409)


def test_share_search_is_owner_only_and_escapes_sql_wildcards(service):
    s = service
    base = '/devices/' + s.device
    assert s.call(base + '/share-candidates?q=friend', who='admin').status_code == 403
    assert s.call(base + '/share-candidates?q=friend', tenant='tenant-b').status_code == 403
    assert s.call(base + '/share-candidates?q=x').json == {'users': []}
    from sqlalchemy import create_engine, text
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        s.database.session = connection
        connection.execute(text('CREATE TABLE users (id INTEGER, username TEXT, active INTEGER)'))
        connection.execute(text("INSERT INTO users VALUES (1,'friend_one',1),(2,'friendXtwo',1),(3,'friend_off',0)"))
        assert s.call(base + '/share-candidates?q=friend_').json == {'users': [{'id': '1', 'name': 'friend_one'}]}
    engine.dispose()


def test_touch_bindings_are_forwarded_and_bounded(service):
    s = service
    bindings = {'UI_Up': ['ShiftLeft', 'ArrowUp'], 'FocusLeftPanel': ['Digit1']}
    assert s.pulse(status={'enabled': True, 'control_enabled': True, 'input_bindings': bindings}).status_code == 200
    assert s.call('/devices').json['devices'][0]['status']['input_bindings'] == bindings
    viewer = s.call(f'/devices/{s.device}/viewers', 'POST', json={}).json['viewer_id']
    assert s.call(f'/devices/{s.device}/viewers/{viewer}', 'POST', json={}).json['status']['input_bindings'] == bindings
    for invalid in ([], {'UI_Up': ['ArrowUp'] * 6}, {'UI_Up': ['bad code']},
                    {'Action' + str(i): ['KeyA'] for i in range(65)}):
        assert s.pulse(status={'enabled': True, 'input_bindings': invalid}).status_code == 400
        assert s.call('/devices').json['devices'][0]['status']['input_bindings'] == bindings
    assert s.pulse().status_code == 200
    assert 'input_bindings' not in s.call('/devices').json['devices'][0]['status']
