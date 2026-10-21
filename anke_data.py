"""Durable ANKe data. Uses the paired-device ACL and the same SQLite transaction lock.

No media, executable requests or user-controlled filesystem paths are stored here.
"""
import base64
import hashlib
import json
import re
import time
import zlib

from flask import request
from remote_screen import RemoteError
from anke_mission_protocol import empty_bundle, merge_bundles, stamp
from anke_receipts import validate_receipt
from datetime import datetime, timezone

RETENTION = 30 * 86400


class AnkeData:
    def __init__(self, remote):
        self.remote, self.db = remote, remote.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS anke_archive_device
            (id TEXT PRIMARY KEY, tenant TEXT, owner TEXT, name TEXT, removed_at REAL);
          CREATE TABLE IF NOT EXISTS anke_log
            (device TEXT, stream TEXT, name TEXT, kind TEXT, generation TEXT,
             offset INTEGER, data BLOB, updated REAL, PRIMARY KEY(device,stream));
          CREATE TABLE IF NOT EXISTS anke_log_chunk
            (device TEXT, stream TEXT, offset INTEGER, data BLOB, PRIMARY KEY(device,stream,offset));
          CREATE TABLE IF NOT EXISTS anke_event
            (sequence INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, id TEXT, at REAL,
             payload TEXT, UNIQUE(device,id));
          CREATE INDEX IF NOT EXISTS anke_event_device_sequence ON anke_event(device,sequence);
          CREATE TABLE IF NOT EXISTS anke_diagnostic_status
            (device TEXT PRIMARY KEY, at REAL, payload TEXT);
          CREATE TABLE IF NOT EXISTS anke_account
            (tenant TEXT, owner TEXT, fid TEXT, commander TEXT, revision INTEGER,
             bundle TEXT, order_json TEXT, holder TEXT, boot TEXT, generation INTEGER DEFAULT 0,
             lease_until REAL DEFAULT 0, PRIMARY KEY(tenant,owner,fid));
          CREATE TABLE IF NOT EXISTS anke_account_device
            (device TEXT, fid TEXT, PRIMARY KEY(device,fid));
          CREATE TABLE IF NOT EXISTS anke_account_history
            (tenant TEXT, owner TEXT, fid TEXT, revision INTEGER, payload TEXT, at REAL);
          CREATE TABLE IF NOT EXISTS anke_transaction
            (tenant TEXT, owner TEXT, fid TEXT, id TEXT, generation INTEGER, kind TEXT,
             reserved INTEGER, payload TEXT, receipt TEXT, PRIMARY KEY(tenant,owner,fid,id));
        ''')
        # Restart never inherits an execution grant; pending receipts survive.
        self.db.execute('UPDATE anke_account SET lease_until=0,generation=generation+1')
        self.db.commit()

    def remember(self, d):
        self.db.execute('INSERT INTO anke_archive_device VALUES (?,?,?,?,NULL) '
                        'ON CONFLICT(id) DO UPDATE SET name=excluded.name',
                        (d['id'], d['tenant'], d['owner'], d['name']))

    def unpair(self, d):
        self.remember(d)
        self.db.execute('UPDATE anke_archive_device SET removed_at=? WHERE id=?', (time.time(), d['id']))
        self.db.execute('DELETE FROM anke_account_device WHERE device=?', (d['id'],))
        self.db.execute('DELETE FROM anke_event WHERE device=?',(d['id'],))

    def archives(self,identity):
        rows=self.db.execute('SELECT id,name FROM anke_archive_device WHERE tenant=? AND owner=? AND removed_at>?',
                             (identity['tenant_id'],identity['sub'],time.time()-RETENTION)).fetchall()
        return [dict(id=key,name=name,owner=True,grants={},online=False,can_control=False,archived=True,
                     status={'capture':'unpaired'},operator=None) for key,name in rows]

    def readable(self, device, identity):
        try:
            self.remote.require(self.remote.device(device), identity)
        except RemoteError as exc:
            if exc.status != 404:
                raise
            row = self.db.execute('SELECT tenant,owner FROM anke_archive_device WHERE id=?', (device,)).fetchone()
            if not row or tuple(row) != (identity['tenant_id'], identity['sub']):
                raise RemoteError('Archive access denied', 403)

    def prune(self):
        cutoff = time.time() - RETENTION
        self.db.execute('DELETE FROM anke_log WHERE updated<?', (cutoff,))
        self.db.execute('DELETE FROM anke_log_chunk WHERE NOT EXISTS (SELECT 1 FROM anke_log WHERE anke_log.device=anke_log_chunk.device AND anke_log.stream=anke_log_chunk.stream)')
        # Last 25 events survive the archive TTL; historic events do not.
        self.db.execute('DELETE FROM anke_event WHERE at<? AND sequence NOT IN '
                        '(SELECT sequence FROM anke_event e WHERE e.device=anke_event.device '
                        'ORDER BY sequence DESC LIMIT 25)', (cutoff,))
        self.db.execute('DELETE FROM anke_event WHERE sequence NOT IN '
                        '(SELECT sequence FROM anke_event e WHERE e.device=anke_event.device '
                        'ORDER BY sequence DESC LIMIT 10000)')
        self.db.commit()

    def upload(self, d, b):
        self.prune()
        self.remember(d)
        stream, generation = token(b.get('stream')), token(b.get('generation'))
        name, kind = b.get('name'), b.get('kind')
        if (not isinstance(name, str) or not re.fullmatch(r'[\w .-]{1,160}', name)
                or kind not in ('autopilot', 'remote', 'journal')):
            raise RemoteError('Invalid log metadata')
        offset = integer(b.get('offset'), 0, 2**40)
        try:
            data = base64.b64decode(b.get('data', ''), validate=True)
            if b.get('encoding')=='gzip':
                decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
                data=decoder.decompress(data,128*1024+1)
                if decoder.unconsumed_tail or not decoder.eof:
                    raise ValueError('Compressed chunk exceeds limit')
            elif b.get('encoding') not in (None,'raw'):
                raise ValueError('Unsupported chunk encoding')
        except (ValueError, TypeError, zlib.error):
            raise RemoteError('Invalid log chunk') from None
        if not data or len(data) > 128 * 1024 or hashlib.sha256(data).hexdigest() != b.get('sha256'):
            raise RemoteError('Invalid log checksum or size')
        row = self.db.execute('SELECT generation,offset,data FROM anke_log WHERE device=? AND stream=?',
                              (d['id'], stream)).fetchone()
        if row:
            if row[0] != generation:
                raise RemoteError('Stream generation changed', 409)
            if offset < row[1]:
                # ACK only an exact retransmission, never accept divergent bytes.
                previous=self.db.execute('SELECT data FROM anke_log_chunk WHERE device=? AND stream=? AND offset=?',(d['id'],stream,offset)).fetchone()
                if not previous or bytes(previous[0]) != data:
                    raise RemoteError('Log retry differs', 409)
                return {'offset': row[1]}
            if offset != row[1]:
                return {'offset': row[1]}
        elif offset != 0:
            return {'offset': 0}
        size = (row[1] if row else 0) + len(data)
        if size > 64 * 1024 * 1024:
            raise RemoteError('Log generation exceeds 64 MiB; start next segment', 413)
        self.db.execute('INSERT INTO anke_log VALUES (?,?,?,?,?,?,?,?) '
                        'ON CONFLICT(device,stream) DO UPDATE SET offset=excluded.offset, '
                        'data=excluded.data,updated=excluded.updated',
                        (d['id'], stream, name, kind, generation, size,
                         b'', time.time()))
        self.db.execute('INSERT INTO anke_log_chunk VALUES (?,?,?,?)',(d['id'],stream,offset,data))
        self.db.commit()
        return {'offset': size}

    def events(self, d, b):
        rows = b.get('events', [])
        if not isinstance(rows, list) or len(rows) > 100:
            raise RemoteError('Invalid event batch')
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('message'), str)
                    or len(row['message']) > 8192 or not isinstance(row.get('compact'), str)
                    or len(row['compact']) > 120 or not isinstance(row.get('icon'), str)
                    or len(row['icon']) > 16):
                raise RemoteError('Invalid operational event')
            token(row.get('id'))
            row.setdefault('at',datetime.now(timezone.utc).isoformat())
            row.setdefault('clock',datetime.now(timezone.utc).strftime('%H:%M:%S'))
            if (not isinstance(row['at'],str) or len(row['at'])>80
                    or not isinstance(row['clock'],str) or not re.fullmatch(r'\d{2}:\d{2}:\d{2}',row['clock'])
                    or not isinstance(row.get('locations',[]),list) or len(row.get('locations',[]))>25):
                raise RemoteError('Invalid operational timestamp or locations')
            for location in row.get('locations',[]):
                if (not isinstance(location,(list,tuple)) or len(location)!=2 or location[0] not in ('FC','Sys','St')
                        or not isinstance(location[1],str) or len(location[1])>2048):
                    raise RemoteError('Invalid operational location')
        self.prune()
        self.remember(d)
        for row in rows:
            self.db.execute('INSERT OR IGNORE INTO anke_event(device,id,at,payload) VALUES (?,?,?,?)',
                            (d['id'], row['id'], time.time(), json.dumps(row, ensure_ascii=False)))
        status = b.get('status', {})
        if not isinstance(status, dict) or len(json.dumps(status)) > 4096:
            raise RemoteError('Invalid diagnostic status')
        if type(status.get('queued_bytes',0)) is not int or status.get('queued_bytes',0)<0 or (status.get('gap') is not None and (not isinstance(status['gap'],str) or len(status['gap'])>2048)):
            raise RemoteError('Invalid diagnostic queue status')
        self.db.execute('INSERT OR REPLACE INTO anke_diagnostic_status VALUES (?,?,?)',
                        (d['id'], time.time(), json.dumps(status)))
        self.db.commit()
        return {'accepted': [row['id'] for row in rows]}

    def diagnostics(self, device, identity):
        self.readable(device, identity)
        self.prune()
        rows = self.db.execute('SELECT sequence,payload FROM anke_event WHERE device=? '
                               'ORDER BY sequence DESC LIMIT 25', (device,)).fetchall()
        status = self.db.execute('SELECT at,payload FROM anke_diagnostic_status WHERE device=?', (device,)).fetchone()
        try:
            log_offset=integer(int(request.args.get('log_offset','0')),0,10**7)
        except ValueError:
            raise RemoteError('Invalid archive offset') from None
        logs = self.db.execute('SELECT stream,name,kind,offset,updated FROM anke_log WHERE device=? '
                              'ORDER BY updated DESC,stream LIMIT 251 OFFSET ?', (device,log_offset)).fetchall()
        return {'events': [dict(json.loads(payload), sequence=seq) for seq, payload in reversed(rows)],
                'received_at': status[0] if status else None, 'status': json.loads(status[1]) if status else {},
                'log_offset':log_offset,'has_more_logs':len(logs)>250,
                'logs': [dict(zip(('stream','name','kind','size','updated_at'), row)) for row in logs[:250]]}

    def log_page(self, device, stream, identity):
        self.readable(device, identity)
        self.prune()
        try:
            offset = integer(int(request.args.get('offset', '0')), 0, 2**40)
        except ValueError:
            raise RemoteError('Invalid log offset') from None
        row = self.db.execute('SELECT name,offset,data FROM anke_log WHERE device=? AND stream=?', (device, stream)).fetchone()
        if not row:
            raise RemoteError('Log not found', 404)
        query=request.args.get('q','')[:200].casefold()
        window=1024*1024 if query else 65536
        chunks=self.db.execute('SELECT offset,data FROM anke_log_chunk WHERE device=? AND stream=? AND offset<? AND offset+length(data)>? ORDER BY offset',(device,stream,offset+window,offset)).fetchall()
        page=b''.join(bytes(raw)[max(0,offset-start):min(len(raw),offset+window-start)] for start,raw in chunks)
        return {'name': row[0], 'offset': offset, 'next_offset': offset+len(page),
                'size': row[1], 'data': base64.b64encode(page).decode(),
                'text': '\n'.join(line for line in page.decode('utf-8', errors='replace').splitlines() if query in line.casefold()) if query else page.decode('utf-8', errors='replace')}

    def key(self, d, fid):
        if not isinstance(fid, str) or not re.fullmatch(r'F[0-9]+', fid):
            raise RemoteError('Verified Frontier FID required')
        if not self.db.execute('SELECT 1 FROM anke_account_device WHERE device=? AND fid=?', (d['id'], fid)).fetchone():
            raise RemoteError('Enable synchronization for this account on this client first', 403)
        return d['tenant'], d['owner'], fid

    def account(self, d, fid):
        key = self.key(d, fid)
        row = self.db.execute('SELECT commander,revision,bundle,order_json,holder,boot,generation,lease_until '
                              'FROM anke_account WHERE tenant=? AND owner=? AND fid=?', key).fetchone()
        if not row:
            raise RemoteError('Account not found', 404)
        return key, dict(zip(('commander','revision','bundle','order','holder','boot','generation','lease_until'), row))

    def bind(self, d, b):
        fid, commander = b.get('fid'), b.get('commander')
        if not isinstance(fid, str) or not re.fullmatch(r'F[0-9]+', fid) or not isinstance(commander, str) or not 0 < len(commander) <= 200:
            raise RemoteError('Verified account identity required')
        if b.get('enabled') is not True:
            self.db.execute('DELETE FROM anke_account_device WHERE device=? AND fid=?', (d['id'], fid))
        else:
            self.db.execute('INSERT OR IGNORE INTO anke_account VALUES (?,?,?,?,0,?,NULL,NULL,NULL,0,0)',
                            (d['tenant'], d['owner'], fid, commander, json.dumps(empty_bundle({'fid':fid,'commander':commander}))))
            self.db.execute('INSERT OR IGNORE INTO anke_account_device VALUES (?,?)', (d['id'], fid))
        self.db.commit()
        return {'ok': True}

    def sync(self, d, fid, b):
        key, a = self.account(d, fid)
        if b is not None:
            try:
                merged, report = merge_bundles(json.loads(a['bundle']), b['bundle'], {'fid':fid,'commander':a['commander']})
            except (ValueError, KeyError, TypeError) as exc:
                raise RemoteError(str(exc)) from None
            if report['conflicts']:
                error=RemoteError('Central mission evidence conflicts',409)
                error.details=report['conflicts']
                raise error
            if merged['missions'] != json.loads(a['bundle'])['missions']:
                self.db.execute('INSERT INTO anke_account_history VALUES (?,?,?,?,?,?)',(*key,a['revision'],a['bundle'],time.time()))
                self.db.execute('UPDATE anke_account SET bundle=?,revision=revision+1 WHERE tenant=? AND owner=? AND fid=?',
                                (json.dumps(merged), *key))
                self.db.commit()
                return self.sync(d, fid, None)
        order_id=json.loads(a['order'])['id'] if a['order'] else ''
        transactions = self.db.execute("SELECT reserved,receipt FROM anke_transaction WHERE tenant=? AND owner=? AND fid=? AND kind='buy' AND json_extract(payload,'$.order_id')=?", (*key,order_id)).fetchall()
        spent = sum(json.loads(r[1])['cost'] if r[1] else r[0] for r in transactions)
        pending = self.db.execute('SELECT id,kind,payload FROM anke_transaction WHERE tenant=? AND owner=? AND fid=? AND receipt IS NULL', key).fetchall()
        result = {'spent':spent, 'revision':a['revision'], 'bundle':json.loads(a['bundle']), 'order':json.loads(a['order']) if a['order'] else None,
                'pending':[dict(id=r[0], kind=r[1], intent=json.loads(r[2])) for r in pending]}
        if b is not None and b.get('known_revision')==a['revision']:
            result.pop('bundle')
            result['unchanged']=True
        return result

    def lease(self, d, fid, b):
        key, a = self.account(d, fid)
        boot = token(b.get('boot'))
        now = time.time()
        if b.get('release'):
            self.fence(d, a, b)
            self.db.execute('UPDATE anke_account SET lease_until=0 WHERE tenant=? AND owner=? AND fid=?', key)
        elif a['holder'] == d['id'] and a['boot'] == boot and a['lease_until'] > now:
            if b.get('generation') is not None:
                self.fence(d, a, b)
            self.db.execute('UPDATE anke_account SET lease_until=? WHERE tenant=? AND owner=? AND fid=?', (now+30,*key))
        else:
            if b.get('generation') is not None:
                raise RemoteError('Expired leases cannot renew or reacquire implicitly',409)
            if a['lease_until'] > now:
                raise RemoteError('Another client is running this account', 409)
            if self.db.execute('SELECT 1 FROM anke_transaction WHERE tenant=? AND owner=? AND fid=? AND receipt IS NULL',key).fetchone():
                raise RemoteError('Unresolved transaction; reconcile original evidence before Resume',409)
            a['generation'] += 1
            self.db.execute('UPDATE anke_account SET holder=?,boot=?,generation=?,lease_until=? WHERE tenant=? AND owner=? AND fid=?',
                            (d['id'],boot,a['generation'],now+30,*key))
        self.db.commit()
        return {'generation':a['generation'], 'valid_for':30}

    @staticmethod
    def fence(d, a, b):
        if (a['holder'] != d['id'] or a['boot'] != b.get('boot') or a['generation'] != b.get('generation')
                or a['lease_until'] <= time.time()):
            raise RemoteError('Wing Trade authorization expired',409)

    def order(self, d, fid, b):
        key, a = self.account(d, fid)
        self.fence(d,a,b)
        if b.get('revision') != a['revision']:
            raise RemoteError('Order changed; refresh before Resume',409)
        order = b.get('order')
        if (not isinstance(order,dict) or set(order) != {'id','selected_ids','markets','budget'}
                or not isinstance(order['selected_ids'],list) or not order['selected_ids']
                or len(order['selected_ids']) > 100 or any(type(x) is not int or x<=0 for x in order['selected_ids'])
                or not isinstance(order['markets'],list) or len(order['markets'])>200):
            raise RemoteError('Invalid portable order')
        token(order['id'])
        integer(order['budget'],0,10**15)
        # Whitelist portable navigation data, never accept local paths/configuration.
        for row in order['markets']:
            if (not isinstance(row,dict) or set(row)-{'SystemName','StationName','Skip'}
                    or any(not isinstance(row.get(k),str) or len(row[k])>200 for k in ('SystemName','StationName'))):
                raise RemoteError('Invalid portable supply market')
        old = json.loads(a['order']) if a['order'] else None
        if old == order:
            return self.sync(d,fid,None)
        if old and old != order:
            if self.db.execute('SELECT 1 FROM anke_transaction WHERE tenant=? AND owner=? AND fid=? AND receipt IS NULL',key).fetchone():
                raise RemoteError('Cannot replace an order with unresolved transactions',409)
            rows={r['mission_id']:r for r in json.loads(a['bundle'])['missions']}
            active_markets=lambda value:[(r['SystemName'],r['StationName']) for r in value['markets'] if not r.get('Skip',False)]
            extension=(order['id']==old['id'] and order['budget']==old['budget']
                       and active_markets(order)==active_markets(old)
                       and set(old['selected_ids']) <= set(order['selected_ids']))
            if not extension and any(mid not in rows or not (rows[mid]['terminal'] or rows[mid]['progress'] and rows[mid]['progress']['delivered']==rows[mid]['progress']['total']) for mid in old['selected_ids']):
                raise RemoteError('Existing order must be completed before replacing it',409)
        self.db.execute('UPDATE anke_account SET order_json=?,revision=revision+1 WHERE tenant=? AND owner=? AND fid=?', (json.dumps(order),*key))
        self.db.commit()
        return self.sync(d,fid,None)

    def reconcile(self,d,fid,b):
        key,a=self.account(d,fid)
        tid=token(b.get('id'))
        old=self.db.execute('SELECT kind,payload,receipt FROM anke_transaction WHERE tenant=? AND owner=? AND fid=? AND id=?',(*key,tid)).fetchone()
        if not old or json.loads(old[1]).get('source_device') != d['id']:
            raise RemoteError('Only the originating device may reconcile its sealed transaction',403)
        receipt=b.get('receipt')
        if not isinstance(receipt,dict):
            raise RemoteError('Portable receipt is required')
        # Reconciliation books the proven actual expense, including an overshoot.
        # Normal purchase authorization and receipt submission stay bounded.
        validate_receipt(old[0],json.loads(old[1]),receipt,fid,reconcile_purchase=True)
        if old[2]:
            if json.loads(old[2]) != receipt:
                raise RemoteError('Receipt differs',409)
            return {'ok':True,'id':tid}
        self.db.execute('UPDATE anke_transaction SET receipt=? WHERE tenant=? AND owner=? AND fid=? AND id=?',(json.dumps(receipt),*key,tid))
        self.apply_receipt(key,a,old[0],receipt)
        self.db.commit()
        return {'ok':True,'id':tid}

    def apply_receipt(self,key,a,kind,receipt):
        if kind != 'delivery':
            return
        bundle=empty_bundle({'fid':key[2],'commander':a['commander']})
        event=receipt['proof']['event']
        bundle['missions']=[{'mission_id':receipt['mission_id'],'acceptance':None,'terminal':None,
            'progress':{'delivered':receipt['items_delivered'],'total':event['TotalItemsToDeliver'],
                        'commodity':receipt['commodity'],'source':'cargo_depot','observed_at':receipt['timestamp']}}]
        merged,report=merge_bundles(json.loads(a['bundle']),bundle,bundle['account'])
        if report['conflicts']:
            raise RemoteError('Receipt conflicts with central mission evidence',409)
        self.db.execute('UPDATE anke_account SET bundle=?,revision=revision+1 WHERE tenant=? AND owner=? AND fid=?',(json.dumps(merged),*key))

    def transaction(self,d,fid,b):
        key,a=self.account(d,fid)
        self.fence(d,a,b)
        tid=token(b.get('id'))
        old=self.db.execute('SELECT kind,reserved,payload,receipt FROM anke_transaction WHERE tenant=? AND owner=? AND fid=? AND id=?',(*key,tid)).fetchone()
        receipt=b.get('receipt')
        if receipt is not None:
            if not old or not isinstance(receipt,dict):
                raise RemoteError('No prepared transaction')
            if old[3] and json.loads(old[3]) != receipt:
                raise RemoteError('Receipt differs',409)
            validate_receipt(old[0],json.loads(old[2]),receipt,fid)
            if old[3]:
                return {'ok':True,'id':tid}
            if not receipt.get('timestamp') or not isinstance(receipt.get('quantity'),int) or receipt['quantity']<=0:
                raise RemoteError('Verified receipt required')
            self.db.execute('UPDATE anke_transaction SET receipt=? WHERE tenant=? AND owner=? AND fid=? AND id=?',(json.dumps(receipt),*key,tid))
            self.apply_receipt(key,a,old[0],receipt)
        else:
            kind=b.get('kind'); intent=b.get('intent')
            if kind not in ('buy','delivery') or not isinstance(intent,dict):
                raise RemoteError('Invalid transaction')
            order=json.loads(a['order']) if a['order'] else None
            if not order:
                raise RemoteError('A portable order is required',409)
            intent=dict(intent,order_id=order['id'],source_device=d['id'])
            integer(intent.get('quantity'),1,10**9)
            integer(intent.get('market_id'),1,2**64-1)
            if intent.get('commodity') not in ('gold','silver','gallite','bertrandite','indite'):
                raise RemoteError('Unsupported transaction commodity')
            if kind=='buy':
                integer(intent.get('maximum_cost'),0,10**15)
            else:
                integer(intent.get('mission_id'),1,2**64-1)
                integer(intent.get('total'),1,10**9)
                integer(intent.get('delivered_before'),0,intent['total'])
                if intent['delivered_before']+intent['quantity']>intent['total']:
                    raise RemoteError('Delivery exceeds remaining mission quantity')
            facts={row['mission_id']:row for row in json.loads(a['bundle'])['missions']}
            if any(facts.get(mid,{}).get('terminal') for mid in order['selected_ids']):
                raise RemoteError('A selected mission became terminal; refresh the order',409)
            if kind=='delivery':
                central=facts.get(intent['mission_id'])
                if intent['mission_id'] not in order['selected_ids']:
                    raise RemoteError('Mission is outside this order',409)
                if central and central['progress'] and central['progress']['delivered']>intent['delivered_before']:
                    raise RemoteError('Central mission progress advanced; verify the depot again',409)
            reserve=integer(intent.get('maximum_cost',0),0,10**15)
            if old:
                if old[:3] != (kind,reserve,json.dumps(intent,sort_keys=True)):
                    raise RemoteError('Transaction intent differs',409)
            else:
                if self.db.execute('SELECT 1 FROM anke_transaction WHERE tenant=? AND owner=? AND fid=? AND receipt IS NULL',key).fetchone():
                    raise RemoteError('Previous transaction is unresolved',409)
                order=json.loads(a['order']) if a['order'] else None
                if not order:
                    raise RemoteError('A portable order is required',409)
                spent=sum((json.loads(r[1])['cost'] if r[1] else r[0]) for r in self.db.execute(
                    "SELECT reserved,receipt FROM anke_transaction WHERE tenant=? AND owner=? AND fid=? AND kind='buy' AND json_extract(payload,'$.order_id')=?",(*key,order['id'])))
                if spent+reserve>order['budget']:
                    raise RemoteError('Account credit ceiling reached',409)
                self.db.execute('INSERT INTO anke_transaction VALUES (?,?,?,?,?,?,?,?,NULL)',(*key,tid,a['generation'],kind,reserve,json.dumps(intent,sort_keys=True)))
        self.db.commit()
        return {'ok':True,'id':tid}


def token(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',value):
        raise RemoteError('Invalid identifier')
    return value


def integer(value,low,high):
    if type(value) is not int or not low<=value<=high:
        raise RemoteError('Invalid integer')
    return value


def register_anke_routes(store, route, body):
    data=AnkeData(store)
    def device():
        return store.authenticate_device(request.headers.get('Authorization',''))
    @route('/device/diagnostics/logs',['POST'],False)
    def diagnostic_upload():
        return data.upload(device(),body(256*1024))
    @route('/device/diagnostics/events',['POST'],False)
    def diagnostic_events():
        return data.events(device(),body(1024*1024))
    @route('/devices/<device_id>/diagnostics',['GET'])
    def diagnostic_read(identity,device_id):
        return data.diagnostics(device_id,identity)
    @route('/devices/<device_id>/diagnostics/logs/<stream>',['GET'])
    def diagnostic_log(identity,device_id,stream):
        return data.log_page(device_id,stream,identity)
    @route('/device/wing/accounts',['POST'],False)
    def wing_bind():
        return data.bind(device(),body())
    @route('/device/wing/<fid>/sync',['GET','POST'],False)
    def wing_sync(fid):
        return data.sync(device(),fid,body(9*1024*1024) if request.method=='POST' else None)
    @route('/device/wing/<fid>/lease',['POST'],False)
    def wing_lease(fid):
        return data.lease(device(),fid,body())
    @route('/device/wing/<fid>/order',['PUT'],False)
    def wing_order(fid):
        return data.order(device(),fid,body(256*1024))
    @route('/device/wing/<fid>/reconcile',['POST'],False)
    def wing_reconcile(fid):
        return data.reconcile(device(),fid,body(256*1024))
    @route('/device/wing/<fid>/transactions',['POST'],False)
    def wing_transaction(fid):
        return data.transaction(device(),fid,body(256*1024))
    return data
