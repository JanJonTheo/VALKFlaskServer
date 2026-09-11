"""Frozen alert comparisons and deterministic, in-memory Discord graphics."""
from datetime import datetime, timezone
from io import BytesIO
import json
import logging
import os
import textwrap

from dateutil.parser import isoparse
from sqlalchemy import create_engine, text
from bgs_discord import context, key, label, number, obj, safe, time_label

logger = logging.getLogger(__name__)


def instant(value):
    try:
        parsed = isoparse(str(value))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def snapshot_view(row, names):
    payload = obj(row.get('payload_json', row.get('payload')))
    faction_rows = {key(f.get('Name')): f for f in (payload.get('Factions') or []) if isinstance(f, dict)}
    factions = []
    for name in names:
        raw = faction_rows.get(key(name), {})
        influence = number(raw.get('Influence'))
        if influence is not None and abs(influence) <= 1:
            influence *= 100
        factions.append({'name': name, 'influence_pp': influence,
                         'active': raw.get('ActiveStates', []), 'pending': raw.get('PendingStates', [])})
    return {'available': bool(row), 'ticktime': row.get('ticktime'), 'factions': factions,
            'conflicts': [c for c in (payload.get('Conflicts') or []) if isinstance(c, dict)],
            'system': {'controller': obj(payload.get('SystemFaction')).get('Name'),
                       'allegiance': payload.get('SystemAllegiance'),
                       'government': payload.get('SystemGovernment_Localised') or payload.get('SystemGovernment'),
                       'economy': payload.get('SystemEconomy_Localised') or payload.get('SystemEconomy'),
                       'population': payload.get('Population')}}


def comparison(alert, snapshots=(), rule_type=None):
    facts = obj(alert.get('facts_json', alert.get('facts')))
    stored = obj(facts.get('presentation'))
    if stored.get('version') == 1:
        return stored
    names, reason, threshold = context(alert)
    tick = facts.get('ticktime') or alert.get('fired_ticktime')
    baseline = facts.get('baseline_ticktime')
    rows = [r for r in snapshots if instant(r.get('ticktime'))]
    latest = next((r for r in rows if instant(r.get('ticktime')) == instant(tick)), {})
    before = next((r for r in rows if baseline and instant(r.get('ticktime')) == instant(baseline)), {})
    # Non-window conditions use the preceding observation for display only.
    if not baseline and rule_type in ('controller_below', 'controller_gap') and instant(tick):
        candidates = [r for r in rows if instant(r.get('ticktime')) < instant(tick)]
        before = max(candidates, key=lambda r: instant(r['ticktime'])) if candidates else {}
        baseline = before.get('ticktime')
    previous, current = snapshot_view(before, names), snapshot_view(latest, names)
    previous['ticktime'], current['ticktime'] = baseline, tick
    # Stored numeric facts take precedence over potentially corrected snapshots.
    principal = facts.get('tenant_faction') or facts.get('monitored_faction') or facts.get('controlling_faction')
    change = obj(facts.get('strongest_change'))
    for prev, curr in zip(previous['factions'], current['factions']):
        if key(curr['name']) == key(principal):
            after = number(facts.get('tenant_influence_pp'))
            if after is None:
                after = number(facts.get('controller_influence_pp'))
            if after is not None:
                curr['influence_pp'] = after
            if number(facts.get('baseline_influence_pp')) is not None:
                prev['influence_pp'] = facts['baseline_influence_pp']
        if key(curr['name']) == key(change.get('faction')):
            for target, field in [(prev, 'baseline_influence_pp'), (curr, 'current_influence_pp')]:
                if number(change.get(field)) is not None:
                    target['influence_pp'] = change[field]
        if key(curr['name']) == key(facts.get('competitor')) and number(facts.get('competitor_influence_pp')) is not None:
            curr['influence_pp'] = facts['competitor_influence_pp']
    kind = ('conflict' if reason.startswith(('New conflict', 'Conflict update')) else 'gap' if reason.startswith('Gap')
            else 'change' if reason.startswith(('Gain', 'Loss')) else 'below' if reason.startswith('Influence') else 'unknown')
    entered = facts.get('entered_factions') or []
    gap_item = next((x for x in entered if isinstance(x, dict) and key(x.get('faction')) in {key(n) for n in names[1:]}), {})
    previous_gap = number(gap_item.get('previous_gap_pp'))
    current_gap = number(gap_item.get('gap_pp'))
    if current_gap is None:
        current_gap = number(facts.get('gap_pp'))
    if kind == 'gap':
        for view, gap in [(previous, previous_gap), (current, current_gap)]:
            if len(view['factions']) == 2 and gap is not None:
                a, b = [f['influence_pp'] for f in view['factions']]
                signed = rule_type == 'controller_gap' or 'competitor' in facts
                if number(a) is not None and number(b) is not None and abs((a-b if signed else abs(a-b)) - gap) > .01:
                    # Historical rows may have been corrected after this alarm.
                    # Preserve the recorded gap without inventing a rival value.
                    view['factions'][1]['influence_pp'] = None
    return {'version': 1, 'kind': kind, 'rule_type': rule_type, 'reason': reason, 'threshold': threshold,
            'previous_gap_pp': previous_gap, 'current_gap_pp': current_gap,
            'threshold_pp': number(facts.get('threshold_pp')), 'previous': previous, 'current': current}


def historical_comparison(alert):
    facts = obj(alert.get('facts_json', alert.get('facts')))
    if obj(facts.get('presentation')).get('version') == 1:
        return facts['presentation']
    ticks = [t for t in [facts.get('ticktime') or alert.get('fired_ticktime'), facts.get('baseline_ticktime')] if t]
    engine = None
    try:
        uri = os.getenv('SNAPSHOT_DB_URL', 'sqlite:///db/bgs_eddn_snapshots.db')
        engine = create_engine(uri, connect_args={'timeout': 2} if uri.startswith('sqlite') else {})
        with engine.connect() as conn:
            rows = conn.execute(text('SELECT ticktime,payload_json FROM system_tick_snapshot '
                                     'WHERE system_name=:system AND is_settled=1 AND ticktime IN (:current,:previous)'),
                                {'system': alert['system_name'], 'current': ticks[0] if ticks else '',
                                 'previous': ticks[1] if len(ticks) > 1 else ''}).mappings().all()
        return comparison(alert, [dict(r) for r in rows])
    except Exception:
        logger.warning('Historical comparison unavailable for alert %s', alert.get('alert_id', alert.get('id')))
        return comparison(alert)
    finally:
        if engine is not None:
            engine.dispose()


def percent(value):
    return f'{value:.2f}%' if number(value) is not None else 'Unavailable'


def conflict_text(view, names, details=False):
    if not view.get('ticktime'):
        return 'Unavailable'
    wanted = {key(n) for n in names}
    for c in view.get('conflicts', []):
        pair = {key(obj(c.get('Faction1')).get('Name')), key(obj(c.get('Faction2')).get('Name'))}
        if pair == wanted:
            result = label(c.get('WarType') or c.get('Type') or 'Conflict') + ' · ' + label(c.get('Status'))
            if details:
                for side in ('Faction1', 'Faction2'):
                    faction = obj(c.get(side))
                    days = faction.get('WonDays')
                    result += '\n' + str(faction.get('Name') or 'Unknown faction') + ': ' + str(days if days is not None else 'unknown') + ' won days · Stake: ' + str(faction.get('Stake') or 'none reported')
            return result
    return 'No matching conflict' if view.get('available') else 'Unavailable'


def render_graphic(data):
    # Pixel coordinates keep typography constant while removing unused rows.
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    names = [f['name'] for f in data['current']['factions']]
    conflict = data['kind'] == 'conflict'
    below = data['kind'] == 'below'
    height = 285 if conflict else 300 if below else 350 if len(names) > 1 else 240
    fig = Figure(figsize=(10, height / 100), dpi=100, facecolor='#10171e')
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set(xlim=(0, 1000), ylim=(height, 0))
    ax.axis('off')
    gold, white, muted, red = '#e3bd59', '#edf2f7', '#aab6c3', '#f1697c'
    ax.text(500, 18, data['reason'], ha='center', va='top', fontsize=22, weight='bold', color=gold)
    caption = data['threshold'] or 'Values at alert trigger'
    if data['kind'] == 'gap':
        old_gap = data.get('previous_gap_pp')
        caption += ' · Previous gap: ' + (f'{old_gap:.2f} pp' if number(old_gap) is not None else 'Unavailable')
    ax.text(500, 60, caption, ha='center', va='top', color=muted, fontsize=12)
    if conflict:
        for x, name in zip([250, 750], names):
            ax.text(x, 126, textwrap.fill(textwrap.shorten(name, 48, placeholder='…'), 24), ha='center', va='center', fontsize=15, color=white)
        ax.text(500, 126, 'VS', ha='center', va='center', color=red, fontsize=22, weight='bold')
        for x, view in [(250, data['previous']), (750, data['current'])]:
            ax.text(x, 200, textwrap.fill(conflict_text(view, names), 28), ha='center', va='center', color=gold, fontsize=13)
    else:
        for i, (prev, curr) in enumerate(zip(data['previous']['factions'], data['current']['factions'])):
            y = 153 + i * 112
            ax.text(500, y - 49, textwrap.fill(textwrap.shorten(curr['name'], 70, placeholder='…'), 40), ha='center', va='center', color=white, fontsize=13)
            a, b = prev['influence_pp'], curr['influence_pp']
            for x, value in [(230, a), (790, b)]:
                ax.text(x, y, percent(value), ha='center', va='center', fontsize=25 if number(value) is not None else 17, color=white, weight='bold')
            if number(a) is not None and number(b) is not None:
                delta = b - a
                color = red if delta < 0 else '#62d4a3' if delta > 0 else muted
                end = (475, y) if delta == 0 else (460, y + 18 if delta < 0 else y - 18)
                start = (435, y) if delta == 0 else (460, y - 18 if delta < 0 else y + 18)
                ax.annotate('', xy=end, xytext=start, arrowprops={'arrowstyle': '-|>', 'color': color, 'lw': 3, 'mutation_scale': 21})
                ax.text(493, y, f'{delta:+.2f} pp', ha='left', va='center', color=color, fontsize=14, weight='bold')
        if not names:
            ax.text(500, 146, 'Historical comparison unavailable', ha='center', color=muted, fontsize=18)
        if below and number(data.get('threshold_pp')) is not None:
            limit = data['threshold_pp']
            ax.plot([85, 915], [215, 215], color=muted, lw=3)
            marker = 85 + 830 * max(0, min(100, limit)) / 100
            ax.plot([marker, marker], [203, 227], color=red, lw=3)
            value = data['current']['factions'][0]['influence_pp'] if names else None
            if number(value) is not None:
                ax.plot(85 + 830 * max(0, min(100, value)) / 100, 215, 'o', color=gold, markersize=8)
            ax.text(500, 244, f'0–100% influence · threshold {limit:.2f}%', ha='center', color=muted, fontsize=11)
    ax.text(230, height - 20, 'Previous Snapshot', ha='center', va='center', color=muted, fontsize=11)
    ax.text(790, height - 20, 'Alert Snapshot', ha='center', va='center', color=muted, fontsize=11)
    output = BytesIO()
    fig.savefig(output, format='png', facecolor=fig.get_facecolor())
    return output.getvalue()


def system_metadata(alert, data):
    historical = dict(data['current'].get('system', {}))
    if all(historical.get(k) is not None and historical.get(k) != '' for k in ('controller', 'allegiance', 'government', 'economy', 'population')):
        return {**historical, 'source': 'Alert Snapshot', 'updated_at': data['current'].get('ticktime')}
    # EDDN BGS snapshots do not generally retain population or system taxonomy.
    # Use a single current source, explicitly labelled; never mix it into alarm values.
    from bgs_discord import current_system
    try:
        info, _ = current_system(alert['system_name'])
        if info:
            return {'controller': info.get('controlling_faction'), 'allegiance': info.get('allegiance'),
                    'government': info.get('government'), 'economy': info.get('economy'),
                    'population': info.get('population'), 'updated_at': info.get('updated_at'),
                    'source': 'Latest available System Data'}
    except Exception:
        logger.warning('System metadata unavailable for alert %s', alert.get('alert_id', alert.get('id')))
    return {**historical, 'source': 'Alert Snapshot', 'updated_at': data['current'].get('ticktime')}


def message_payload(alert, data, has_image=True, metadata=None):
    from bgs_discord import notification
    original = notification(alert)
    details = original['embeds'][0]
    fields = []
    names = [f['name'] for f in data['current']['factions']]
    for heading, view in [('Previous Snapshot', data['previous']), ('Alert Snapshot', data['current'])]:
        value = safe(time_label(view.get('ticktime'))) + '\n'
        value += '\n'.join(safe(f['name'], 160) + ': **' + percent(f['influence_pp']) + '**' for f in view['factions'])
        if data['kind'] == 'conflict':
            value += '\n' + safe(conflict_text(view, names, details=True), 800)
        if data['kind'] == 'gap':
            gap = data.get('previous_gap_pp' if heading == 'Previous Snapshot' else 'current_gap_pp')
            value += '\nGap: **' + (f'{gap:.2f} pp' if number(gap) is not None else 'Unavailable') + '**'
        fields.append({'name': heading, 'value': value[:1024], 'inline': True})
    system = metadata if metadata is not None else data['current'].get('system', {})
    population = system.get('population')
    if isinstance(population, int):
        population = f'{population:,}'
    fields.append({'name': 'System Data', 'value': safe(' · '.join(label(system.get(k)) for k in ['controller', 'allegiance', 'government', 'economy']), 700) + '\nPopulation: ' + safe(str(population) if population is not None else 'Unavailable', 40)})
    fields[-1]['value'] += '\n' + safe(system.get('source') or 'Alert Snapshot', 60) + ' · ' + safe(time_label(system.get('updated_at') or data['current'].get('ticktime')), 60)
    links = [f['value'] for f in details['fields'] if 'links' in f['name'].lower()]
    if links:
        joined = ' · '.join(links)
        if len(joined) > 1024:
            joined = '[All System/Map Links in Alert Center](' + details['url'] + ')'
        fields.append({'name': 'System/Map Links', 'value': joined})
    fields.append({'name': 'Dashboard', 'value': '[Alert Center](' + details['url'] + ')'})
    details.update(title='Snapshot Comparison', description='**' + safe(data['reason']) + '**\n' + safe(data['threshold']) + '\n' + safe(alert.get('message'), 700), fields=fields,
                   footer={'text': 'VALK · BGS Alert Center · historical alarm values'})
    details.pop('timestamp', None)
    fired = instant(alert.get('fired_at'))
    time = f'<t:{int(fired.timestamp())}:F>' if fired else 'Unavailable'
    content = '## ' + safe(alert.get('system_name'), 255).replace('\n', ' ') + '\nAlert time: ' + time
    content += '\n**' + safe(str(alert.get('severity', 'warning')).upper(), 20) + ' · ' + ('RESOLVED' if alert.get('resolved_at') else 'ACTIVE') + '**'
    embeds = ([{'color': details['color'], 'image': {'url': 'attachment://bgs-alert.png'}}] if has_image else []) + [details]
    payload = {'content': content, 'embeds': embeds, 'allowed_mentions': {'parse': []}}
    if has_image:
        payload['attachments'] = [{'id': 0, 'filename': 'bgs-alert.png', 'description': (data['reason'] + '; ' + data['threshold'])[:1024]}]
    return payload


def delivery_request(alert):
    data = historical_comparison(alert)
    metadata = system_metadata(alert, data)
    try:
        png = render_graphic(data)
    except Exception:
        logger.exception('BGS alert graphic could not be rendered')
        return {'json': message_payload(alert, data, False, metadata)}
    return {'data': {'payload_json': json.dumps(message_payload(alert, data, metadata=metadata))},
            'files': {'files[0]': ('bgs-alert.png', png, 'image/png')}}
