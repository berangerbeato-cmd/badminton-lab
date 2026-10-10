#!/usr/bin/env python3
"""V3.2 vs public odds, research-only. No bet, no executable-price claim."""
import argparse
import csv
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path


def norm(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    return re.sub(r'[^a-z0-9]', '', ''.join(c for c in value if not unicodedata.combining(c)).lower())


def match_name(short, full):
    s = norm(short)
    f = norm(full)
    if not s or not f:
        return False
    if s == f:
        return True
    # Verified BWF display-order exception: LEE Zii Jia -> ZJ.Lee.
    if s == 'zjlee' and f == 'leeziijia':
        return True
    # Initial + family name, e.g. A.Antonsen or ZJ.Lee.
    words = re.findall(r'[A-Za-z]+', unicodedata.normalize('NFKD', full))
    short_parts = re.findall(r'[A-Za-z]+', unicodedata.normalize('NFKD', short))
    if len(words) >= 2 and len(short_parts) >= 2:
        family = norm(words[-1])
        initials = ''.join(norm(w)[0] for w in words[:-1] if norm(w))
        return norm(short_parts[-1]) == family and norm(''.join(short_parts[:-1])) == initials
    return False


def load_json(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--odds-dir', type=Path, default=Path('data/odds_shadow'))
    p.add_argument('--out', type=Path, default=Path('data/odds_shadow/comparison_v3_auto'))
    args = p.parse_args()
    with args.model.open(encoding='utf-8-sig', newline='') as f:
        models = list(csv.DictReader(f))
    observations = []
    rejected = []
    source_audit = {}
    now = datetime.now(timezone.utc)
    for source in ('unibet_fr', 'netbet_quotes'):
        folder = args.odds_dir / source
        if not folder.is_dir():
            source_audit[source] = {'status': 'SOURCE_DIRECTORY_MISSING', 'quotes_read': 0}
            continue
        candidates = sorted(folder.glob('*.json'))
        if not candidates:
            source_audit[source] = {'status': 'NO_SNAPSHOTS', 'quotes_read': 0}
            continue
        file = folder / 'latest.json' if (folder / 'latest.json').exists() else candidates[-1]
        data = load_json(file)
        source_audit[source] = {'status': 'SNAPSHOT_READ', 'snapshot_file': str(file), 'quotes_read': len(data.get('quotes', [])) if isinstance(data.get('quotes'), list) else 0}
        try:
            observed_at = datetime.fromisoformat(str(data.get('observed_at_utc', '')).replace('Z', '+00:00'))
            if observed_at.tzinfo is None:
                raise ValueError('timezone missing')
            observed_at = observed_at.astimezone(timezone.utc)
        except (ValueError, TypeError):
            source_audit[source]['status'] = 'OBSERVATION_TIMESTAMP_INVALID'
            rejected.append({'source': source, 'reason': 'OBSERVATION_TIMESTAMP_INVALID'})
            continue
        if observed_at > now + timedelta(minutes=2) or now - observed_at > timedelta(minutes=45):
            source_audit[source]['status'] = 'OBSERVATION_NOT_FRESH'
            rejected.append({'source': source, 'reason': 'OBSERVATION_NOT_FRESH', 'observed_at_utc': observed_at.isoformat()})
            continue
        source_audit[source]['status'] = 'FRESH_SNAPSHOT'
        for quote in data.get('quotes', []):
            if isinstance(quote, dict) and quote.get('market') == 'H2H_FULL_MATCH':
                observations.append((source, observed_at.isoformat(), quote))
    results = []
    for source, observed, quote in observations:
        a, b = quote.get('player_1_display'), quote.get('player_2_display')
        candidates = [m for m in models if match_name(a, m.get('player_a')) and match_name(b, m.get('player_b'))]
        if len(candidates) != 1:
            results.append({'source': source, 'players': [a, b], 'status': 'MATCH_UNRESOLVED', 'matches': len(candidates)})
            continue
        m = candidates[0]
        listed_day = quote.get('listed_day_paris')
        model_day = str(m.get('start_utc', ''))[:10]
        if listed_day and model_day and listed_day != model_day:
            results.append({'source': source, 'players': [a, b], 'bwf_match_id': m.get('bwf_match_id'),
                            'status': 'EVENT_DAY_MISMATCH', 'decision': 'NO_BET'})
            continue
        try:
            model_at = datetime.fromisoformat(m['asof_utc'].replace('Z', '+00:00'))
            seen_at = datetime.fromisoformat(observed.replace('Z', '+00:00'))
            age = (seen_at - model_at).total_seconds()
            prob = float(m['v3_2_p_a'])
            prices = [float(quote['odds_1']), float(quote['odds_2'])]
            if model_at.tzinfo is None or seen_at.tzinfo is None:
                raise ValueError('timezone missing')
            if not 0 < prob < 1 or min(prices) <= 1:
                raise ValueError('Invalid probability or price')
        except (ValueError, TypeError, KeyError, AttributeError):
            results.append({'source': source, 'players': [a, b], 'status': 'INVALID_INPUT'})
            continue
        flags = ['MARKET_DETAIL_UNVERIFIED', 'KICKOFF_UNVERIFIED', 'EXECUTABLE_PRICE_UNVERIFIED', 'BOOKMAKER_PRICE_TIMESTAMP_UNKNOWN']
        if age < 0:
            flags.append('MODEL_AFTER_OBSERVATION')
        if age > 2700:
            flags.append('MODEL_OLDER_THAN_45_MIN')
        if str(m.get('status', '')).startswith('DATE_ONLY_'):
            flags.append('BWF_KICKOFF_DATE_ONLY')
        results.append({'source': source, 'bwf_match_id': m['bwf_match_id'], 'players': [m['player_a'], m['player_b']],
                        'observed_at_utc': observed, 'model_asof_utc': m['asof_utc'], 'model_age_seconds': round(age),
                        'model_probability_a': prob, 'odds': prices,
                        'illustrative_ev_pct': [round((prob * prices[0] - 1) * 100, 2), round(((1-prob) * prices[1] - 1) * 100, 2)],
                        'flags': flags, 'status': 'UNVERIFIED_SHADOW_ONLY', 'decision': 'NO_BET'})
    output = {'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'policy': 'RESEARCH_ONLY_NO_BET_NO_T0', 'comparison_count': len(results),
              'rejected_sources': rejected, 'source_audit': source_audit, 'results': results}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'latest.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"V3.2 comparisons: {len(results)}; all unverified, no bets")


if __name__ == '__main__':
    main()
