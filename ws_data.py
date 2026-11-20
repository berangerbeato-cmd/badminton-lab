"""Strict WS archive reader. Raw archives and all MS code stay untouched."""
import gzip
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from elo_model import Match, GAME_SPAN, _matches
from bwf_sync import is_elite


def players(m):
    if str(m.get('draw_name', '')).strip().split(' ')[0].upper() != 'WS':
        raise ValueError('not_WS')
    a, b = m.get('t1p1_detail'), m.get('t2p1_detail')
    if any((m.get(k) or {}).get('id') for k in ('t1p2_detail', 't2p2_detail')):
        raise ValueError('doubles')
    if not all(isinstance(p, dict) and str(p.get('id', '')).isdigit() and int(p['id']) > 0 for p in (a, b)):
        raise ValueError('missing_player_ids')
    if str(a['id']) == str(b['id']):
        raise ValueError('same_player')
    if any(p.get('gender_id') not in (None, 2, '2') for p in (a, b)):
        raise ValueError('gender_conflict')
    return a, b


def parse(tid, name, m, asof):
    a, b = players(m)
    if m.get('winner') not in (1, 2):
        raise ValueError('no_winner')
    status = str(m.get('status_name') or '').lower()
    if re.search(r'retir|walk|withdraw|w/o|abandon|cancel|disqual', status) or m.get('code_status') not in (None, 0, '0'):
        raise ValueError('abnormal_status')
    scores = [[int(x) for x in GAME_SPAN.findall(m.get(k) or '')] for k in ('team1Score', 'team2Score')]
    x, y = scores
    if len(x) not in (2, 3) or len(x) != len(y):
        raise ValueError('incomplete_score')
    wins = [0, 0]
    for u, v in zip(x, y):
        hi, lo = max(u, v), min(u, v)
        if max(wins) == 2 or not ((hi == 21 and 0 <= lo <= 19) or (22 <= hi <= 29 and hi-lo == 2) or (hi == 30 and lo in (28, 29))):
            raise ValueError('invalid_game_score')
        wins[int(v > u)] += 1
    if wins[m['winner']-1] != 2:
        raise ValueError('winner_score_conflict')
    try:
        day = datetime.fromtimestamp(int(m['start_time']), timezone.utc).date().isoformat()
    except (KeyError, ValueError, TypeError, OverflowError):
        raise ValueError('invalid_date') from None
    if day < '2018-01-01' or day >= asof:
        raise ValueError('outside_asof_window')
    return Match(f'{tid}:{m["id"]}', day, name, str(m.get('round_name') or ''),
                 str(a['id']), a.get('name_display') or str(a['id']),
                 str(b['id']), b.get('name_display') or str(b['id']), m['winner'],
                 ' '.join(f'{u}-{v}' for u, v in zip(x, y)))


def load_ws(root=Path('data/bwf'), asof=None):
    asof = asof or datetime.now(timezone.utc).date().isoformat()
    index = {str(t['id']): t for t in json.loads((root/'index.json').read_text())}
    files = sorted((root/'matches').glob('*.json.gz'))
    rejected, results, fixtures = Counter(), {}, {}
    raw_ws = 0
    for file in files:
        tid = file.name.split('.')[0]
        name = index.get(tid, {}).get('name', tid)
        for m in _matches(json.load(gzip.open(file, 'rt'))):
            if str(m.get('draw_name') or '').strip().split(' ')[0].upper() != 'WS':
                continue
            raw_ws += 1
            try:
                entry = parse(tid, name, m, asof)
                if entry.key in results:
                    rejected['duplicate'] += 1
                else:
                    results[entry.key] = entry
            except ValueError as exc:
                rejected[str(exc)] += 1
                if str(exc) == 'no_winner':
                    try:
                        a,b = players(m)
                        start = datetime.fromtimestamp(int(m['start_time']), timezone.utc)
                        if start.date().isoformat() >= asof:
                            fixtures[f'{tid}:{m["id"]}'] = dict(match_id=f'{tid}:{m["id"]}', start_utc=start.isoformat(),
                                player_a_id=str(a['id']), player_b_id=str(b['id']), player_a=a.get('name_display'),
                                player_b=b.get('name_display'), tournament=name, discipline='WS')
                    except (ValueError, TypeError, KeyError, OverflowError):
                        pass
    matches = sorted(results.values(), key=lambda m: (m.date, m.key))
    archived = {f.name.split('.')[0] for f in files}
    missing = [dict(id=t['id'], name=t['name'], start=t.get('start_date'), category=t.get('category'))
               for tid,t in index.items() if tid not in archived and is_elite(t)
               and '2018' <= str(t.get('start_date','')) < asof]
    report = dict(asof_exclusive=asof, archives=len(files), raw_ws=raw_ws, valid_ws=len(matches),
        rejected=dict(rejected), players=len({p for m in matches for p in (m.p1_id,m.p2_id)}),
        tournaments=len({m.key.split(':')[0] for m in matches}),
        years=dict(sorted(Counter(m.date[:4] for m in matches).items())),
        months=dict(sorted(Counter(m.date[:7] for m in matches).items())),
        latest_match=matches[-1].date if matches else None, missing_indexed_elite=missing,
        completeness='UNPROVEN: indexed missing archives may include cancellations; cache is not the complete calendar',
        partial_collection_years=[2020,2021,2023])
    return matches, report, list(fixtures.values()), index
