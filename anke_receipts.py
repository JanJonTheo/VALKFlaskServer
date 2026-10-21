"""Validate portable journal/cargo proof against one sealed transaction intent."""
from collections import defaultdict
from anke_mission_protocol import date
from remote_screen import RemoteError


def cargo_vector(cargo):
    result=defaultdict(lambda:[0,0])
    if not isinstance(cargo,dict) or not isinstance(cargo.get('Inventory'),list):
        raise RemoteError('Portable cargo proof is missing')
    total=0
    for row in cargo['Inventory']:
        count,stolen=row.get('Count'),row.get('Stolen',0)
        if type(count) is not int or type(stolen) is not int or not 0<=stolen<=count:
            raise RemoteError('Invalid portable cargo counts')
        commodity=str(row.get('Name','')).lower().strip('$;').removesuffix('_name')
        key=(commodity,str(row.get('MissionID') or ''))
        result[key][0]+=count; result[key][1]+=stolen; total+=count
    if total != cargo.get('Count'):
        raise RemoteError('Portable cargo total differs')
    return result,total


def validate_receipt(kind,intent,receipt,fid,*,reconcile_purchase=False):
    proof=receipt.get('proof')
    if not isinstance(proof,dict) or proof.get('fid') != fid:
        raise RemoteError('Account-bound portable proof is required')
    event=proof.get('event'); before=proof.get('cargo_before'); after=proof.get('cargo_after')
    if not isinstance(event,dict):
        raise RemoteError('Original journal receipt required')
    quantity=receipt.get('quantity')
    commodity=intent.get('commodity'); market=intent.get('market_id')
    variance=kind=='buy' and reconcile_purchase
    if (type(quantity) is not int or quantity<=0 or type(intent.get('quantity')) is not int or (quantity>intent['quantity'] and not variance)
            or receipt.get('commodity') != commodity or receipt.get('market_id') != market
            or receipt.get('timestamp') != event.get('timestamp') or date(receipt.get('timestamp')) is None):
        raise RemoteError('Receipt identity differs from prepared intent')
    when=date(event['timestamp'])
    started=date(intent.get('started_at'))
    if started is None or when<started.replace(microsecond=0):
        raise RemoteError('Journal receipt predates prepared intent')
    if intent.get('cargo_before') is not None and intent['cargo_before']!=before:
        raise RemoteError('Before-cargo differs from the sealed intent')
    old,total_before=cargo_vector(before); new,total_after=cargo_vector(after)
    if date(after.get('timestamp')) is None or date(after['timestamp'])<when:
        raise RemoteError('Fresh after-cargo proof required')
    delta={key:[new[key][i]-old[key][i] for i in (0,1)] for key in set(old)|set(new)}
    delta={key:value for key,value in delta.items() if value != [0,0]}
    direction=1 if kind=='buy' else -1
    if delta != {(commodity,''):[direction*quantity,0]} or total_after-total_before!=direction*quantity:
        raise RemoteError('Portable cargo delta does not corroborate exactly one transaction')
    if kind=='buy':
        if (event.get('event')!='MarketBuy' or event.get('MarketID')!=market
                or str(event.get('Type','')).lower().strip('$;').removesuffix('_name')!=commodity
                or event.get('Count')!=quantity or event.get('TotalCost')!=receipt.get('cost')
                or type(receipt.get('cost')) is not int or receipt['cost']<0
                or (receipt['cost']>intent['maximum_cost'] and not variance)):
            raise RemoteError('MarketBuy proof differs from prepared purchase')
    elif (event.get('event')!='CargoDepot' or str(event.get('UpdateType','')).lower()!='deliver'
          or event.get('MissionID')!=intent.get('mission_id') or event.get('EndMarketID')!=market
          or str(event.get('CargoType','')).lower().strip('$;').removesuffix('_name')!=commodity
          or event.get('Count')!=quantity or event.get('TotalItemsToDeliver')!=intent.get('total')
          or event.get('ItemsDelivered')!=intent['delivered_before']+quantity
          or receipt.get('items_delivered')!=event.get('ItemsDelivered') or receipt.get('mission_id')!=intent.get('mission_id')):
        raise RemoteError('CargoDepot proof differs from prepared delivery')
    return receipt
