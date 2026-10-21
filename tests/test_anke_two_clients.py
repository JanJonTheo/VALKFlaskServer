"""Real client serializers/stores against the Flask API; isolated journals only."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from test_remote_screen import service
from test_anke_data import device, paired


def test_mission_restore_order_handoff_and_shared_credit_ceiling(service,tmp_path,monkeypatch):
    client_source=Path(os.getenv('ANKE_CLIENT_SOURCE',str(Path(__file__).resolve().parents[2]/'EDAPGui-ANKe')))
    if not (client_source/'EDRemoteData.py').is_file():
        pytest.skip('Set ANKE_CLIENT_SOURCE to the ANKe checkout for the two-client integration test')
    monkeypatch.syspath_prepend(str(client_source))
    # Exercise the dormant sync protocol without enabling it in normal clients.
    import EDWingRemoteRecovery
    monkeypatch.setattr(EDWingRemoteRecovery, 'WING_SYNC_AVAILABLE', True)
    from EDRemoteData import RemoteData
    from EDWingMissionTracker import WingMissionTracker
    s=service
    account={'fid':'F1','commander':'Eydis'}
    now=datetime.now(timezone.utc)
    stamp=lambda seconds:(now+timedelta(seconds=seconds)).isoformat().replace('+00:00','Z')
    def journals(pc,accepted):
        directory=tmp_path/pc/'journals';directory.mkdir(parents=True)
        rows=[{'event':'Commander','FID':'F1','Name':'Eydis','timestamp':stamp(-600)},
              {'event':'LoadGame','FID':'F1','Commander':'Eydis','timestamp':stamp(-599)}]
        if accepted:
            rows.append({'event':'MissionAccepted','MissionID':123,'Name':'Mission_Collect','LocalisedName':'Source gold',
             'Faction':'Example','Commodity':'gold','Count':10,'Wing':True,'DestinationSystem':'Sol',
             'DestinationStation':'Galileo','Expiry':stamp(86400),'Reward':100000,'timestamp':stamp(-590)})
        rows.extend([{'event':'Missions','Active':[{'MissionID':123,'Name':'Mission_Collect','Expires':stamp(86400)}],
                     'Complete':[],'Failed':[],'timestamp':stamp(-580)},
                     {'event':'Cargo','Vessel':'Ship','Count':0,'Inventory':[],'timestamp':stamp(-570)}])
        path=directory/'Journal.001.log';path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        return path
    def client(pc,credentials,accepted):
        value=object.__new__(RemoteData)
        value.remote=SimpleNamespace(boot_id=pc,credentials=dict(credentials,url='https://server.test'))
        value.ap=SimpleNamespace(jn=SimpleNamespace(current_log=str(journals(pc,accepted))),
                                 config={'WingRemoteAccounts':['F1']},wing_trade_assist=SimpleNamespace(active=False))
        value.wing_root=tmp_path/pc/'wing-trade';value.lock=threading.RLock()
        value.lease_lock=threading.RLock()
        value.lease=None;value.pending=None;value.last_sync=0;value.spent=0
        def connection(path,body=None,method=None):
            result=device(s,path.removeprefix('device/'),body,credentials,method or ('POST' if body is not None else 'GET'))
            if result.status_code>=400:
                raise ValueError(result.json['error']['message'])
            return result.json
        value.connection=connection
        connection('device/wing/accounts',dict(account,enabled=True))
        return value
    a=client('pcA',s.credentials,True)
    b=client('pcB',paired(s),False)
    assert len(a.sync(account)['bundle']['missions'])==1
    metadata=a.wing_root/'accounts'/'F1'/'remote-order.json'
    first_sync=metadata.read_bytes(),metadata.stat().st_mtime_ns
    a.sync(account)
    assert (metadata.read_bytes(),metadata.stat().st_mtime_ns)==first_sync
    b.sync(account)
    tracker=WingMissionTracker(sync_root=b.wing_root)
    [restored]=tracker.refresh_directory(Path(b.ap.jn.current_log).parent,commander_fid='F1')
    assert restored['mission_id']==123
    assert restored['total']==10
    assert restored['destination_station']=='Galileo'
    assert restored['acceptance_source']=='mission_sync'
    assert restored['delivered']==0
    request={'account':account,'selected_ids':[123],'markets':[{'SystemName':'Sol','StationName':'Galileo'}],'budget':1000}
    assert a.begin(request)
    with pytest.raises(ValueError,match='Another client'):
        b.begin(request)
    state={'pending_buy':{'commodity':'gold','quantity':1,'maximum_cost':600,'market_id':99,'started_at':stamp(-20)},'purchases':[]}
    a.checkpoint(state)
    receipt={'commodity':'gold','quantity':1,'cost':500,'market_id':99,'timestamp':stamp(-19),
       'proof':{'fid':'F1','event':{'event':'MarketBuy','Type':'gold','MarketID':99,'Count':1,'TotalCost':500,'timestamp':stamp(-19)},
          'cargo_before':{'Count':0,'Inventory':[]},'cargo_after':{'Count':1,'Inventory':[{'Name':'gold','Count':1}],'timestamp':stamp(-19)}}}
    state.update(pending_buy=None,purchases=[receipt]);a.checkpoint(state)
    a.end()
    assert b.begin(request)
    assert b.spent==500
    with pytest.raises(ValueError,match='credit ceiling'):
        b.checkpoint({'pending_buy':{'commodity':'gold','quantity':1,'maximum_cost':600,'market_id':99,'started_at':stamp(-10)}})
    b.end()
