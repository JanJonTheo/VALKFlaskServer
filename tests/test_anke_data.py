import base64
import hashlib
import json
import time

from test_remote_screen import service
from remote_screen import PREFIX, RemoteError
from anke_data import AnkeData
from anke_mission_protocol import empty_bundle


def device(s,path,body=None,credentials=None,method='POST'):
    c=credentials or s.credentials
    return s.client.open(PREFIX+'/device/'+path,method=method,json=body,
                         headers={'Authorization':'Device '+c['device_id']+'.'+c['secret']})


def paired(s,who='owner'):
    code=s.call('/pairings','POST',who=who,json={}).json['code']
    return s.client.post(PREFIX+'/pair',json={'code':code,'name':'Second PC'}).json


def setup_wing(s):
    assert device(s,'wing/accounts',{'fid':'F1','commander':'Eydis','enabled':True}).status_code==200
    lease=device(s,'wing/F1/lease',{'boot':'bootA'}).json
    fence={'boot':'bootA','generation':lease['generation']}
    order={'id':'order1','selected_ids':[123],'markets':[{'SystemName':'Sol','StationName':'Galileo'}],'budget':1000}
    assert device(s,'wing/F1/order',dict(fence,revision=0,order=order),method='PUT').status_code==200
    return fence


def test_resumable_logs_acl_revoke_and_unpair_archive(service):
    s=service
    raw=b'first\nsecond\n'
    chunk={'stream':'s1','generation':'s1','name':'Journal.1.log','kind':'journal','offset':0,
           'data':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()}
    assert device(s,'diagnostics/logs',chunk).json['offset']==len(raw)
    assert device(s,'diagnostics/logs',chunk).json['offset']==len(raw)
    chunk['data']=base64.b64encode(b'different').decode(); chunk['sha256']=hashlib.sha256(b'different').hexdigest()
    assert device(s,'diagnostics/logs',chunk).status_code==409
    path=f'/devices/{s.device}/diagnostics'
    assert s.call(path,who='admin').status_code==403
    assert s.call(path,tenant='tenant-b').status_code==403
    s.call(f'/devices/{s.device}/grants','PUT',json={'user_id':'friend','rights':['view']})
    assert s.call(path+'/logs/s1',who='friend').json['text']==raw.decode()
    s.call(f'/devices/{s.device}/grants','PUT',json={'user_id':'friend','rights':[]})
    assert s.call(path+'/logs/s1',who='friend').status_code==403
    assert s.call(f'/devices/{s.device}','DELETE').status_code==200
    assert s.call(path+'/logs/s1').status_code==200
    assert device(s,'diagnostics/logs',chunk).status_code==401


def test_exactly_25_events_survive_retention_and_deduplicate(service):
    s=service
    events=[{'id':f'e{i}','message':f'message {i}','compact':f'msg {i}','icon':'📦','locations':[['Sys','Sol']]} for i in range(30)]
    assert device(s,'diagnostics/events',{'events':events,'status':{'queued_bytes':42}}).status_code==200
    device(s,'diagnostics/events',{'events':events})
    data=s.client.application.extensions['anke_data']
    with s.store.lock:
        data.db.execute('UPDATE anke_event SET at=?',(time.time()-31*86400,));data.db.commit()
    result=s.call(f'/devices/{s.device}/diagnostics').json
    assert len(result['events'])==25
    assert result['events'][0]['id']=='e5'
    assert result['events'][-1]['id']=='e29'


def test_log_expiry_does_not_delete_missions(service):
    s=service
    setup_wing(s)
    data=s.client.application.extensions['anke_data']
    data.db.execute('INSERT INTO anke_log VALUES (?,?,?,?,?,?,?,?)',(s.device,'x','autopilot.log','autopilot','x',1,b'x',time.time()-31*86400))
    data.db.commit()
    assert not s.call(f'/devices/{s.device}/diagnostics').json['logs']
    assert device(s,'wing/F1/sync',method='GET').json['order']['id']=='order1'


def test_owner_account_isolation_and_explicit_device_binding(service):
    s=service;setup_wing(s)
    pc2=paired(s)
    assert device(s,'wing/F1/sync',credentials=pc2,method='GET').status_code==403
    device(s,'wing/accounts',{'fid':'F1','commander':'Eydis','enabled':True},credentials=pc2)
    assert device(s,'wing/F1/sync',credentials=pc2,method='GET').json['order']['id']=='order1'
    other=paired(s,'friend')
    device(s,'wing/accounts',{'fid':'F1','commander':'Eydis','enabled':True},credentials=other)
    assert device(s,'wing/F1/sync',credentials=other,method='GET').json['order'] is None


def test_lease_fencing_pending_and_credit_reservation(service):
    s=service;fence=setup_wing(s)
    pc2=paired(s)
    device(s,'wing/accounts',{'fid':'F1','commander':'Eydis','enabled':True},credentials=pc2)
    assert device(s,'wing/F1/lease',{'boot':'bootB'},credentials=pc2).status_code==409
    intent={'commodity':'gold','quantity':2,'maximum_cost':600,'market_id':99,'started_at':'2026-10-20T10:00:00Z'}
    body=dict(fence,id='tx1',kind='buy',intent=intent)
    assert device(s,'wing/F1/transactions',body).status_code==200
    assert device(s,'wing/F1/transactions',body).status_code==200
    assert device(s,'wing/F1/transactions',dict(body,id='tx2')).status_code==409
    data=s.client.application.extensions['anke_data']
    data.db.execute('UPDATE anke_account SET lease_until=0');data.db.commit()
    assert device(s,'wing/F1/lease',{'boot':'bootB'},credentials=pc2).status_code==409
    assert device(s,'wing/F1/lease',fence).status_code==409


def test_verified_cost_is_account_wide_and_not_double_counted(service):
    s=service;fence=setup_wing(s)
    intent={'commodity':'gold','quantity':1,'market_id':99,'maximum_cost':600,'started_at':'2026-10-20T10:00:00Z'}
    assert device(s,'wing/F1/transactions',dict(fence,id='tx1',kind='buy',intent=intent)).status_code==200
    receipt=buy_receipt()
    assert device(s,'wing/F1/transactions',dict(fence,id='tx1',receipt=receipt)).status_code==200
    assert device(s,'wing/F1/transactions',dict(fence,id='tx1',receipt=receipt)).status_code==200
    assert device(s,'wing/F1/sync',method='GET').json['spent']==500
    assert device(s,'wing/F1/transactions',dict(fence,id='tx2',kind='buy',intent=intent)).status_code==409
    data=s.client.application.extensions['anke_data']
    assert data.db.execute('SELECT COUNT(*) FROM anke_transaction').fetchone()[0]==1


def test_bundle_terminal_tombstone_and_conflicting_progress(service):
    s=service;setup_wing(s)
    bundle=empty_bundle({'fid':'F1','commander':'Eydis'})
    bundle['missions']=[{'mission_id':123,'acceptance':None,'progress':{'commodity':'gold','total':10,'delivered':5,
       'source':'cargo_depot','observed_at':'2026-10-19T10:00:00Z'},'terminal':{'state':'completed','observed_at':'2026-10-19T11:00:00Z'}}]
    assert device(s,'wing/F1/sync',{'bundle':bundle}).status_code==200
    bundle['missions'][0]['progress']['delivered']=3
    conflict=device(s,'wing/F1/sync',{'bundle':bundle})
    assert conflict.status_code==409
    assert conflict.json['error']['details']
    result=device(s,'wing/F1/sync',method='GET').json
    assert result['bundle']['missions'][0]['progress']['delivered']==5
    assert result['bundle']['missions'][0]['terminal']['state']=='completed'


def buy_receipt():
    return {'cost':500,'quantity':1,'commodity':'gold','market_id':99,'timestamp':'2026-10-20T10:00:00Z',
      'proof':{'fid':'F1','event':{'event':'MarketBuy','MarketID':99,'Type':'gold','Count':1,'TotalCost':500,'timestamp':'2026-10-20T10:00:00Z'},
       'cargo_before':{'Count':0,'Inventory':[],'timestamp':'2026-10-20T09:59:59Z'},
       'cargo_after':{'Count':1,'Inventory':[{'Name':'gold','Count':1}],'timestamp':'2026-10-20T10:00:00Z'}}}


def test_original_device_can_reconcile_after_lost_ack_and_restart(service):
    s=service;fence=setup_wing(s)
    intent={'commodity':'gold','quantity':1,'market_id':99,'maximum_cost':600,'started_at':'2026-10-20T10:00:00Z'}
    device(s,'wing/F1/transactions',dict(fence,id='tx1',kind='buy',intent=intent))
    pc2=paired(s)
    device(s,'wing/accounts',{'fid':'F1','commander':'Eydis','enabled':True},credentials=pc2)
    s.store.db.execute('UPDATE anke_account SET lease_until=0');s.store.db.commit()
    assert device(s,'wing/F1/reconcile',{'id':'tx1','receipt':buy_receipt()},credentials=pc2).status_code==403
    bad=buy_receipt();bad['proof']['cargo_after']['Inventory'][0]['Count']=2
    assert device(s,'wing/F1/reconcile',{'id':'tx1','receipt':bad}).status_code==400
    assert device(s,'wing/F1/reconcile',{'id':'tx1','receipt':buy_receipt()}).status_code==200
    assert device(s,'wing/F1/sync',credentials=pc2,method='GET').json['spent']==500
    assert device(s,'wing/F1/lease',{'boot':'bootB'},credentials=pc2).status_code==200
    assert device(s,'wing/F1/transactions',dict(fence,id='tx2',kind='buy',intent=intent)).status_code==409


def test_cargo_depot_proof_updates_central_absolute_progress(service):
    s=service;fence=setup_wing(s)
    intent={'commodity':'gold','quantity':2,'market_id':99,'mission_id':123,'total':10,
            'delivered_before':3,'started_at':'2026-10-19T10:00:00Z'}
    assert device(s,'wing/F1/transactions',dict(fence,id='tx1',kind='delivery',intent=intent)).status_code==200
    receipt={'quantity':2,'commodity':'gold','market_id':99,'mission_id':123,'items_delivered':5,'timestamp':'2026-10-19T10:00:00Z',
     'proof':{'fid':'F1','event':{'event':'CargoDepot','UpdateType':'Deliver','EndMarketID':99,'MissionID':123,
       'CargoType':'gold','Count':2,'ItemsDelivered':5,'TotalItemsToDeliver':10,'timestamp':'2026-10-19T10:00:00Z'},
       'cargo_before':{'Count':2,'Inventory':[{'Name':'gold','Count':2}]},
       'cargo_after':{'Count':0,'Inventory':[],'timestamp':'2026-10-19T10:00:00Z'}}}
    assert device(s,'wing/F1/transactions',dict(fence,id='tx1',receipt=receipt)).status_code==200
    central=device(s,'wing/F1/sync',method='GET').json
    assert central['bundle']['missions'][0]['progress']['delivered']==5
    assert central['pending']==[]


def test_gzip_chunks_search_and_diagnostic_pulses_without_video(service):
    import gzip
    s=service
    raw=b'ordinary line\nMissionID 123 delivery\n'
    body={'stream':'gz1','generation':'gz1','name':'Journal.1.log','kind':'journal','offset':0,
          'encoding':'gzip','data':base64.b64encode(gzip.compress(raw)).decode(),'sha256':hashlib.sha256(raw).hexdigest()}
    assert device(s,'diagnostics/logs',body).json['offset']==len(raw)
    result=s.call(f'/devices/{s.device}/diagnostics/logs/gz1?q=123').json
    assert result['text']=='MissionID 123 delivery'
    assert base64.b64decode(result['data'])==raw
    for i in range(6):
        pc=paired(s)
        pulse=s.client.post(PREFIX+'/device/pulse',json={'boot_id':str(i),'status':{'enabled':False}},
          headers={'Authorization':'Device '+pc['device_id']+'.'+pc['secret']})
        assert pulse.status_code==200
        assert pulse.json['media'] is None
    device(s,'wing/accounts',{'fid':'F1','commander':'Eydis','enabled':True})
    bundle=empty_bundle({'fid':'F1','commander':'Eydis'})
    revision=device(s,'wing/F1/sync',{'bundle':bundle}).json['revision']
    unchanged=device(s,'wing/F1/sync',{'bundle':bundle,'known_revision':revision}).json
    assert unchanged['unchanged'] is True
    assert 'bundle' not in unchanged
