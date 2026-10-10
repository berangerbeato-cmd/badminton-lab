#!/usr/bin/env python3
"""Quality audit for Badminton Lab V3.2 shadow comparisons. No bets.

Reads comparison_v3_auto/latest.json; writes quality_gate/latest.json and quality_gate/latest.csv.
Never treats a public listing as an executable betting price.
"""
import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MAX_MODEL_AGE_SECONDS = 2700
HARD_BLOCK_FLAGS = {
    'MARKET_DETAIL_UNVERIFIED', 'KICKOFF_UNVERIFIED',
    'EXECUTABLE_PRICE_UNVERIFIED', 'BOOKMAKER_PRICE_TIMESTAMP_UNKNOWN',
    'MODEL_AFTER_OBSERVATION', 'MODEL_OLDER_THAN_45_MIN',
}
FIELDS = ['source', 'bwf_match_id', 'player_a', 'player_b', 'model_age_minutes',
          'ev_a_pct', 'ev_b_pct', 'quality_status', 'reasons', 'decision']


def assess(item):
    flags = set(item.get('flags') or [])
    reasons = sorted(flags & HARD_BLOCK_FLAGS)
    age = item.get('model_age_seconds')
    if not isinstance(age, (int, float)):
        reasons.append('MODEL_AGE_UNKNOWN')
    elif age < 0:
        reasons.append('MODEL_AFTER_OBSERVATION')
    elif age > MAX_MODEL_AGE_SECONDS:
        reasons.append('MODEL_OLDER_THAN_45_MIN')
    if item.get('status') != 'UNVERIFIED_SHADOW_ONLY':
        reasons.append('UNEXPECTED_SOURCE_STATUS')
    if item.get('decision') != 'NO_BET':
        reasons.append('UNEXPECTED_DECISION')
    if not item.get('bwf_match_id'):
        reasons.append('MATCH_ID_MISSING')
    ev = item.get('illustrative_ev_pct')
    if not isinstance(ev, list) or len(ev) != 2 or any(not isinstance(x, (int, float)) for x in ev):
        reasons.append('EV_MISSING_OR_INVALID')
        ev = [None, None]
    players = item.get('players') or ['', '']
    if len(players) != 2:
        players = ['', '']
        reasons.append('PLAYERS_MISSING')
    reasons = sorted(set(reasons))
    return {
        'source': item.get('source', ''),
        'bwf_match_id': item.get('bwf_match_id', ''),
        'player_a': players[0], 'player_b': players[1],
        'model_age_minutes': round(age / 60, 1) if isinstance(age, (int, float)) else '',
        'ev_a_pct': ev[0], 'ev_b_pct': ev[1],
        'quality_status': 'BLOCKED_UNVERIFIED' if reasons else 'REQUIRES_MANUAL_CONFIRMATION',
        'reasons': '|'.join(reasons), 'decision': 'NO_BET',
    }


def run(source, destination):
    if not source.is_file():
        raise FileNotFoundError(f'Missing comparison input: {source}')
    data = json.loads(source.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise ValueError('Comparison JSON must contain a results array')
    rows = [assess(item) for item in data['results'] if isinstance(item, dict)]
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / 'latest.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'comparison_generated_at_utc': data.get('generated_at_utc'),
        'policy': 'RESEARCH_ONLY_NO_BET',
        'comparison_count': len(rows),
        'status_counts': dict(Counter(row['quality_status'] for row in rows)),
        'reason_counts': dict(Counter(reason for row in rows for reason in row['reasons'].split('|') if reason)),
        'rows': rows,
        'note': 'Quality audit only; never confirms a market or executable price.',
    }
    (destination / 'latest.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'comparisons': len(rows), 'statuses': summary['status_counts']}, ensure_ascii=False))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('data/odds_shadow/comparison_v3_auto/latest.json'))
    parser.add_argument('--out', type=Path, default=Path('data/odds_shadow/quality_gate'))
    args = parser.parse_args()
    run(args.input, args.out)


if __name__ == '__main__':
    main()
