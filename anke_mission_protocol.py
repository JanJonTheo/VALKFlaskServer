"""Portable mission evidence shared by file exchange and future remote sync.

No game inputs, no journal writes, no session/cargo authority in a bundle.
Merges are deterministic, additive and reject conflicting mission facts.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile

import re

def _identity(value):
    if not isinstance(value,dict) or not isinstance(value.get("fid"),str) or not re.fullmatch(r"F[0-9]+",value["fid"]) or not isinstance(value.get("commander"),str) or not value["commander"].strip():
        raise ValueError("Verified FID and commander required")
    return {"fid":value["fid"],"commander":value["commander"].strip()}


FORMAT = 'anke.wing-missions'
VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
MAX_MISSIONS = 10000
ACCEPTANCE_FIELDS = ('name', 'title', 'faction', 'commodity', 'total', 'wing',
                     'destination_system', 'destination_station', 'expiry', 'accepted_at', 'reward')
TERMINAL_STATES = ('completed', 'abandoned', 'failed')


def date(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _clean(value):
    return ' '.join(value.split()).casefold() if isinstance(value, str) else value


def _shape(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(label + ': unvollständige oder unbekannte Felder.')


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


def validate_bundle(bundle, identity, *, now=None):
    SUPPORTED_COMMODITIES = {"bertrandite", "indite", "gallite", "silver", "gold"}
    classify_mission_kind = lambda value: re.match(r"^Mission_(?:Collect|Mining)(?:Wing)?(?:_|$)", value.strip("$;"), re.IGNORECASE)
    account = _identity(identity)
    _shape(bundle, ('format', 'version', 'account', 'exported_at', 'source_device', 'missions'), 'Missionspaket')
    if bundle['format'] != FORMAT or type(bundle['version']) is not int or bundle['version'] != VERSION:
        raise ValueError('Nicht unterstütztes Wing-Missionsformat.')
    _shape(bundle['account'], ('fid', 'commander'), 'Account')
    if _identity(bundle['account'])['fid'] != account['fid']:
        raise ValueError('Missionspaket gehört einer anderen Frontier-FID.')
    exported = date(bundle['exported_at'])
    current = now or datetime.now(timezone.utc)
    if (exported is None or exported > current + timedelta(seconds=30)
            or not _text(bundle['source_device']) or len(bundle['source_device']) > 255):
        raise ValueError('Exportzeit oder Quellrechner ist ungültig.')
    if not isinstance(bundle['missions'], list) or len(bundle['missions']) > MAX_MISSIONS:
        raise ValueError('Ungültige oder zu große Missionsliste.')
    ids = set()
    for row in bundle['missions']:
        _shape(row, ('mission_id', 'acceptance', 'progress', 'terminal'), 'Mission')
        mid = row['mission_id']
        if type(mid) is not int or mid <= 0 or mid > 2**64-1 or mid in ids:
            raise ValueError('Ungültige oder doppelte Missions-ID.')
        ids.add(mid)
        acceptance, progress, terminal = row['acceptance'], row['progress'], row['terminal']
        if acceptance is progress is terminal is None:
            raise ValueError('Mission ohne Annahme-, Liefer- oder Abschlussbeleg.')
        for evidence in (acceptance, progress, terminal):
            if evidence is not None and not isinstance(evidence, dict):
                raise ValueError('Missionsbeleg muss ein Objekt sein.')
        if acceptance is not None:
            _shape(acceptance, ACCEPTANCE_FIELDS, 'Missionsannahme')
            if (any(not _text(acceptance[key]) for key in ('name', 'title', 'faction', 'destination_system', 'destination_station'))
                    or not classify_mission_kind(acceptance['name']) or acceptance['commodity'] not in SUPPORTED_COMMODITIES
                    or acceptance['wing'] is not True or type(acceptance['total']) is not int
                    or not 0 < acceptance['total'] <= 10**9):
                raise ValueError('Unvollständige oder nicht unterstützte Wing-Missionsannahme.')
            accepted, expiry = date(acceptance['accepted_at']), date(acceptance['expiry'])
            if accepted is None or expiry is None or accepted > exported or expiry <= accepted:
                raise ValueError('Annahme-/Ablaufzeit ist ungültig.')
            reward = acceptance['reward']
            if reward is not None and (type(reward) is not int or not 0 <= reward <= 10**15):
                raise ValueError('Ungültige Missionsbelohnung.')
        if progress is not None:
            _shape(progress, ('delivered', 'total', 'commodity', 'observed_at', 'source'), 'Lieferbeleg')
            if (type(progress['total']) is not int or not 0 < progress['total'] <= 10**9
                    or type(progress['delivered']) is not int or not 0 <= progress['delivered'] <= progress['total']
                    or progress['commodity'] not in SUPPORTED_COMMODITIES
                    or progress['source'] not in ('cargo_depot', 'mission_board')
                    or date(progress['observed_at']) is None or date(progress['observed_at']) > exported):
                raise ValueError('Ungültiger absoluter Lieferbeleg.')
            if acceptance and (progress['total'] != acceptance['total'] or progress['commodity'] != acceptance['commodity']
                               or date(progress['observed_at']) < date(acceptance['accepted_at'])):
                raise ValueError('Lieferbeleg widerspricht der Missionsannahme.')
        if terminal is not None:
            _shape(terminal, ('state', 'observed_at'), 'Missionsende')
            if (terminal['state'] not in TERMINAL_STATES or date(terminal['observed_at']) is None
                    or date(terminal['observed_at']) > exported
                    or acceptance and date(terminal['observed_at']) < date(acceptance['accepted_at'])):
                raise ValueError('Ungültiger Missionsabschlussbeleg.')
    if len(json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_BYTES:
        raise ValueError('Missionspaket überschreitet 8 MiB.')
    return deepcopy(bundle)


def empty_bundle(identity, now=None):
    return {'format': FORMAT, 'version': VERSION, 'account': _identity(identity),
            'exported_at': stamp(now or datetime.now(timezone.utc)),
            'source_device': socket.gethostname(), 'missions': []}


def bundle_from_missions(missions, identity, *, now=None, acceptance_hints=None):
    """Export all known supported history, independently of the UI selection.

    An assumed zero is never a transferable delivery observation. Imported
    progress retains its original time/source and can be re-exported unchanged.
    """
    bundle = empty_bundle(identity, now)
    hints = {row['mission_id']: row['acceptance'] for row in (acceptance_hints or {}).get('missions', [])
             if row.get('acceptance')}
    for row in missions:
        if row.get('fid') != bundle['account']['fid'] or not row.get('supported_type'):
            continue
        if row.get('accepted_at') and not row.get('in_scope'):
            continue
        acceptance = None
        if row.get('accepted_at') and row.get('in_scope'):
            acceptance = {key: row.get(key) for key in ACCEPTANCE_FIELDS}
            # Keep incomplete original records diagnostic, rather than inventing facts.
            if any(not acceptance.get(key) for key in ('faction', 'destination_system', 'destination_station', 'expiry', 'title')):
                acceptance = None
        progress = None
        commodity = ((row.get('progress_commodity') or row.get('commodity'))
                     if row.get('progress_source') == 'cargo_depot' else row.get('commodity'))
        total = row.get('progress_total') if row.get('progress_source') == 'cargo_depot' else row.get('total')
        hint = hints.get(row['mission_id'])
        if hint and not row.get('accepted_at'):
            if (row.get('name', '').casefold().removesuffix('_name') != hint['name'].casefold().removesuffix('_name')
                    or row.get('total') is not None and row['total'] != hint['total']
                    or commodity and commodity != hint['commodity']):
                raise ValueError(f"Mission {row['mission_id']}: Import widerspricht lokalem Missions-/Depotbeleg.")
            if row.get('expiry') and abs((date(row['expiry'])-date(hint['expiry'])).total_seconds()) > 5:
                raise ValueError(f"Mission {row['mission_id']}: Import widerspricht lokaler Ablaufzeit.")
            if not commodity and row.get('total') == hint['total']:
                commodity = hint['commodity']
        if (row.get('progress_source') in ('cargo_depot', 'mission_board')
                and commodity and type(total) is int and type(row.get('delivered')) is int
                and 0 <= row['delivered'] <= total):
            progress = {'delivered': row['delivered'], 'total': total, 'commodity': commodity,
                        'observed_at': row['progress_observed_at'], 'source': row['progress_source']}
        terminal = ({'state': row['state'], 'observed_at': row.get('terminal_observed_at') or row['observed_at']}
                    if row.get('state') in TERMINAL_STATES else None)
        if acceptance is not None or progress is not None or terminal is not None:
            bundle['missions'].append({'mission_id': row['mission_id'], 'acceptance': acceptance,
                                       'progress': progress, 'terminal': terminal})
    bundle['missions'].sort(key=lambda row: row['mission_id'])
    return validate_bundle(bundle, identity, now=now)


def merge_bundles(base, incoming, identity, *, now=None):
    """Return a candidate plus a review report; conflicts never silently win."""
    left = validate_bundle(base, identity, now=now)
    right = validate_bundle(incoming, identity, now=now)
    result = empty_bundle(identity, now)
    rows = {row['mission_id']: deepcopy(row) for row in left['missions']}
    report = {'added': [], 'updated': [], 'unchanged': [], 'conflicts': []}
    for incoming_row in right['missions']:
        mid = incoming_row['mission_id']
        if mid not in rows:
            rows[mid] = deepcopy(incoming_row)
            report['added'].append(mid)
            continue
        original = rows[mid]
        candidate = deepcopy(original)
        conflicts = []
        for field in ('acceptance', 'progress', 'terminal'):
            old, new = original[field], incoming_row[field]
            if new is None:
                continue
            if old is None:
                candidate[field] = deepcopy(new)
                continue
            if field == 'acceptance':
                for key in ACCEPTANCE_FIELDS:
                    if key == 'reward' and old[key] is None:
                        candidate[field][key] = new[key]
                    elif key == 'reward' and new[key] is None:
                        continue
                    elif (date(old[key]) != date(new[key]) if key in ('accepted_at', 'expiry')
                          else _clean(old[key]) != _clean(new[key])):
                        conflicts.append('Annahme: '+key)
            elif field == 'progress':
                if old['total'] != new['total'] or old['commodity'] != new['commodity']:
                    conflicts.append('Lieferung: Ware/Gesamtmenge')
                elif date(new['observed_at']) == date(old['observed_at']) and new['delivered'] != old['delivered']:
                    conflicts.append('Lieferung: gleicher Zeitpunkt, andere Menge')
                elif date(new['observed_at']) > date(old['observed_at']):
                    if new['delivered'] < old['delivered']:
                        conflicts.append('Lieferung: neuerer Beleg reduziert Gesamtlieferung')
                    else:
                        candidate[field] = deepcopy(new)
                elif new['delivered'] > old['delivered']:
                    conflicts.append('Lieferung: älterer Beleg hat größere Gesamtlieferung')
            elif old['state'] != new['state']:
                conflicts.append('Widersprüchliches Missionsende')
            elif date(new['observed_at']) < date(old['observed_at']):
                candidate[field] = deepcopy(new)
        if not conflicts and candidate['acceptance'] and candidate['progress']:
            if (candidate['acceptance']['commodity'] != candidate['progress']['commodity']
                    or candidate['acceptance']['total'] != candidate['progress']['total']):
                conflicts.append('Annahme und Lieferung widersprechen einander')
        if conflicts:
            report['conflicts'].append({'mission_id': mid, 'fields': conflicts})
        else:
            rows[mid] = candidate
            report['updated' if candidate != original else 'unchanged'].append(mid)
    result['missions'] = sorted(rows.values(), key=lambda row: row['mission_id'])
    # Also validate combinations assembled from separate partial records.
    if not report['conflicts']:
        validate_bundle(result, identity, now=now)
    return result, report
