#!/usr/bin/env python3
"""Synchronisation incrémentale prudente des résultats BWF (stdlib seulement).

Source technique : endpoint non documenté observé dans SahilMotyar/badminton-elo.
La disponibilité publique et les conditions de réutilisation doivent être vérifiées.
Pas d'authentification, pas de contournement des restrictions ou protections.

Exemples :
    python bwf_sync.py --self-test
    python bwf_sync.py --start 2026-10-01 --end 2026-10-08 --dry-run
    python bwf_sync.py --start 2026-10-01 --end 2026-10-08 --data-dir ./data/bwf

Le script écrit index.json et matches/<id>.json.gz compatibles avec le chargeur
`badminton.bwf` du dépôt d'origine. Il rafraîchit chaque tournoi récent à chaque
passage et ne remplace jamais une rencontre terminée par un état non terminé.
Une exécution programmée nécessite un environnement persistant distinct.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

API = 'https://extranet-lv.bwfbadminton.com/api'
UA = 'BadmintonResearchSync/0.1 (public read-only; contact site operator for permissions)'
SPAN = re.compile(r'<span>\s*(\d+)\s*</span>')
ELITE = {
    'World Superseries', 'World Superseries Premier', 'World Superseries Finals',
    'Grand Prix', 'Grand Prix Gold', 'HSBC BWF World Tour Super 300',
    'HSBC BWF World Tour Super 500', 'HSBC BWF World Tour Super 750',
    'HSBC BWF World Tour Super 1000', 'HSBC BWF World Tour Finals',
    'BWF Tour Super 100',
    'Continental Individual Championships',
    'Continental Team Championships',
    'Continental Individual Games',
    'Continental Team Games',
    'Multi-Sport Games',
    'Multi-Sport Games - Team Tournaments',
}


def is_elite(t: dict) -> bool:
    category = (t.get('category') or '').strip()
    name = (t.get('name') or '').lower()
    if '(cancelled)' in name or '(postponed)' in name:
        return False
    if category in ELITE:
        return True
    if category.startswith('Grade 1') and 'Individual Tournaments' in category:
        return not any(word in name for word in ('junior', 'youth', 'senior', 'university'))
    if category == 'Grade 1 – Team Tournaments':
        return not any(x in name for x in ('junior', 'youth', 'u19', 'under 19'))
    if category == 'Other' and any(x in name for x in (
            'asian games', 'commonwealth games', 'mediterranean games',
            'african games', 'sea games', 'pacific games')):
        return not any(x in name for x in (
            'para', 'junior', 'youth', 'senior', 'postponed', 'cancelled'))
    return False


def extract_matches(payload: dict) -> dict[str, dict]:
    res = payload.get('results') or {}
    if not isinstance(res, dict):
        raise ValueError('Invalid tournament results object')
    matches: dict[str, dict] = {}
    raw = (res.get('by_time') or {}).get('time_group') or []
    if isinstance(raw, list):
        for m in raw:
            if isinstance(m, dict) and m.get('id') is not None:
                matches[str(m['id'])] = m
    for group in (res.get('by_court') or {}).values():
        if not isinstance(group, dict):
            continue
        for m in group.values():
            if isinstance(m, dict) and m.get('id') is not None:
                k = str(m['id'])
                if k not in matches or (played(m) and not played(matches[k])):
                    matches[k] = m
    return matches


def played(m: dict) -> bool:
    if m.get('winner') not in (1, 2):
        return False
    first = SPAN.findall(m.get('team1Score') or '')
    second = SPAN.findall(m.get('team2Score') or '')
    return bool(first) and len(first) == len(second)


def merge_match_payload(old: dict | None, fresh: dict) -> tuple[dict, int, int, int]:
    """Merge by BWF match id, favor fresh completed result, never regress.

    Returns payload, newly completed, total completed, total unique IDs.
    The by_time structure is accepted by the upstream `badminton.bwf._matches`.
    """
    older = extract_matches(old) if old else {}
    newer = extract_matches(fresh)
    merged = dict(older)
    new_finished = 0
    for mid, match in newer.items():
        prev = merged.get(mid)
        if prev is None:
            merged[mid] = match
            new_finished += int(played(match))
        elif played(match) or not played(prev):
            if played(match) and not played(prev):
                new_finished += 1
            merged[mid] = match
    output = dict(fresh)
    result = dict(fresh.get('results') or {})
    result['by_time'] = {'time_group': list(merged.values())}
    result['by_court'] = {}  # Already merged; avoid duplicated IDs
    output['results'] = result
    done = sum(played(m) for m in merged.values())
    return output, new_finished, done, len(merged)


def api_get(path: str, params: dict, timeout: int) -> dict:
    url = API + path + '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError('Expected BWF JSON object')
    return payload


def read_json(path: Path):
    if not path.exists():
        return None
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8') as fh:
            return json.load(fh)
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


def atomic_write_json(path: Path, obj, zipped=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    os.close(fd)
    try:
        if zipped:
            with gzip.open(temporary, 'wt', encoding='utf-8') as fh:
                json.dump(obj, fh, ensure_ascii=False)
        else:
            with open(temporary, 'w', encoding='utf-8') as fh:
                json.dump(obj, fh, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def get_tournaments(start: date, end: date, timeout: int,
                    get: Callable[[str, dict, int], dict], delay: float = 0.0) -> list[dict]:
    seen: dict[str, dict] = {}
    page = 1
    while True:
        data = get('/vue-tournaments-search', {
            'startDate': start.isoformat(), 'endDate': end.isoformat(),
            'page': page, 'perPage': 100, 'drawCount': 1, 'activeTab': 6,
        }, timeout)
        results = data.get('results')
        if not isinstance(results, dict) or not isinstance(results.get('data'), list):
            raise ValueError('BWF index JSON has unknown schema')
        for t in results['data']:
            if isinstance(t, dict) and t.get('id') is not None:
                seen[str(t['id'])] = t
        last = int(results.get('last_page') or page)
        if page >= last:
            break
        if page >= 20:
            raise ValueError('More than 20 index pages, stop instead of unbounded scraping')
        page += 1
        if delay:
            time.sleep(delay)
    return list(seen.values())


def sync(start: date, end: date, data_dir: Path, max_tournaments: int, timeout: int,
         delay: float, dry_run: bool = False,
         get: Callable[[str, dict, int], dict] = api_get) -> dict:
    if end < start:
        raise ValueError('End must be on/after start')
    all_recent = get_tournaments(start, end, timeout, get, delay)
    current = read_json(data_dir / 'index.json') or []
    index = {str(t['id']): t for t in current if isinstance(t, dict) and t.get('id') is not None}
    for t in all_recent:
        index[str(t['id'])] = t
    elite = [t for t in all_recent if is_elite(t)]
    if len(elite) > max_tournaments:
        raise ValueError(f'{len(elite)} tournaments > safety max {max_tournaments}; narrow date range')
    print(f'Tournaments indexed={len(all_recent)}, elite-to-refresh={len(elite)}', flush=True)
    if not dry_run:
        atomic_write_json(data_dir / 'index.json', list(index.values()))
    summary = {'indexed': len(all_recent), 'elite_checked': len(elite),
               'tournaments_saved': 0, 'new_completed_matches': 0,
               'unique_match_ids': 0, 'errors': []}
    for i, t in enumerate(elite, 1):
        tid = str(t['id'])
        dest = data_dir / 'matches' / f'{tid}.json.gz'
        try:
            fresh = get('/vue-tournament-matches', {
                'drawCount': 0, 'searchKey': '', 'tmtId': tid,
                'tmtType': 0, 'isPara': 'false',
            }, timeout)
            older = read_json(dest)
            merged, new_done, total_done, total = merge_match_payload(older, fresh)
            if total == 0:
                print(f'[{i}/{len(elite)}] {t.get("name")} -- no match data, skip cache', flush=True)
                continue
            if not dry_run:
                atomic_write_json(dest, merged, zipped=True)
            summary['tournaments_saved'] += 1
            summary['new_completed_matches'] += new_done
            summary['unique_match_ids'] += total
            print(f'[{i}/{len(elite)}] {t.get("name")} -- {total} matches, '
                  f'{total_done} completed, +{new_done} newly completed', flush=True)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, KeyError) as exc:
            # A failed tournament request must not damage the existing cache.
            summary['errors'].append({'tournament': tid, 'error': str(exc)[:180]})
            print(f'[{i}/{len(elite)}] {tid} ERROR {exc}', file=sys.stderr)
        if delay:
            time.sleep(delay)
    return summary


def self_test() -> None:
    finished = lambda mid, winner=1: {'id': mid, 'winner': winner,
        'team1Score': '<span>21</span><span>21</span>',
        'team2Score': '<span>16</span><span>18</span>'}
    partial = lambda mid: {'id': mid, 'winner': None, 'team1Score': '', 'team2Score': ''}
    calls = {'n': 0}
    index = {'results': {'data': [
        {'id': 321, 'name': 'Arctic Open Test', 'category': 'HSBC BWF World Tour Super 500'}],
        'last_page': 1}}
    snapshots = [
        {'results': {'by_time': {'time_group': [finished(1), partial(2)]},
                     'by_court': {'Court A': {'duplicate': finished(1)}}}},
        {'results': {'by_time': {'time_group': [finished(1), finished(2)]}}},
        {'results': {'by_time': {'time_group': [partial(1), finished(2)]}}},
    ]
    def fake_get(path, params, timeout):
        if path == '/vue-tournaments-search':
            return index
        result = snapshots[min(calls['n'], 2)]
        calls['n'] += 1
        return result
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        day = date(2026, 10, 8)
        results = []
        for _ in range(3):
            results.append(sync(day, day, base, 3, 2, 0, get=fake_get))
        assert [r['new_completed_matches'] for r in results] == [1, 1, 0], results
        saved = read_json(base / 'matches' / '321.json.gz')
        games = extract_matches(saved)
        assert len(games) == 2 and all(played(m) for m in games.values())
        assert len(read_json(base / 'index.json')) == 1
        def down(path, params, timeout):
            if path == '/vue-tournaments-search':
                return index
            raise urllib.error.URLError('simulated outage')
        before = (base / 'matches' / '321.json.gz').read_bytes()
        failed = sync(day, day, base, 3, 2, 0, get=down)
        assert len(failed['errors']) == 1 and (base / 'matches' / '321.json.gz').read_bytes() == before
        print('ALL LOCAL TESTS PASSED: 3 incremental refreshes, dedupe, preserve completed '
              'scores, index reuse, simulated HTTP outage without data loss')


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-test', action='store_true')
    p.add_argument('--start', help='YYYY-MM-DD inclusive; default = 21 days ago')
    p.add_argument('--end', help='YYYY-MM-DD inclusive; default = today')
    p.add_argument('--data-dir', default=str(Path(__file__).resolve().parent / 'data' / 'bwf'))
    p.add_argument('--max-tournaments', type=int, default=30)
    p.add_argument('--timeout', type=int, default=15)
    p.add_argument('--delay', type=float, default=1.3)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.self_test:
        self_test()
        return 0
    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=21)
    try:
        outcome = sync(start, end, Path(args.data_dir), args.max_tournaments,
                       args.timeout, args.delay, dry_run=args.dry_run)
        print('SUMMARY', json.dumps(outcome, ensure_ascii=False))
        return 0 if not outcome['errors'] else 2
    except Exception as exc:
        print(f'FAILED: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
