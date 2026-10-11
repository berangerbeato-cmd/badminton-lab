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


def timing_evidence(model_at, observed_at, fixture_start, fixture_status):
    """Conservative chronology: a calendar-only midnight is not a kickoff."""
    if str(fixture_status or '').startswith('DATE_ONLY_'):
        return 'KICKOFF_DATE_ONLY'
    if not fixture_start:
        return 'KICKOFF_MISSING'
    try:
        kickoff = datetime.fromisoformat(str(fixture_start).replace('Z', '+00:00'))
        if kickoff.tzinfo is None:
            return 'KICKOFF_TIMEZONE_MISSING'
        kickoff = kickoff.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return 'KICKOFF_INVALID'
    if model_at >= kickoff:
        return 'MODEL_NOT_BEFORE_KICKOFF'
    if observed_at >= kickoff:
        return 'ODDS_OBSERVATION_NOT_BEFORE_KICKOFF'
    return 'CHRONOLOGY_BEFORE_REPORTED_KICKOFF_NOT_VERIFIED'


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
        quotes = data.get('quotes', [])
        if not isinstance(quotes, list):
            source_audit[source]['status'] = 'INVALID_QUOTES_FORMAT'
            rejected.append({'source': source, 'reason': 'INVALID_QUOTES_FORMAT'})
            continue
        source_audit[source]['status'] = 'FRESH_SNAPSHOT' if quotes else 'FRESH_EMPTY'
        source_audit[source]['collector_status'] = data.get('status')
        for quote in quotes:
            if isinstance(quote, dict) and quote.get('market') == 'H2H_FULL_MATCH':
                observations.append((source, observed_at.isoformat(), quote))
    results = []
    for source, observed, quote in observations:
        a, b = quote.get('player_1_display'), quote.get('player_2_display')
        candidates = []
        for m in models:
            direct = match_name(a, m.get('player_a')) and match_name(b, m.get('player_b'))
            reversed_order = match_name(a, m.get('player_b')) and match_name(b, m.get('player_a'))
            if direct or reversed_order:
                candidates.append((m, bool(reversed_order and not direct)))
        if len(candidates) != 1:
            listed_day = quote.get('listed_day_paris')
            same_day = sum(1 for m in models if listed_day and
                           str(m.get('start_utc', ''))[:10] == listed_day)
            status = ('FIXTURE_ABSENT_FROM_MODEL' if not candidates else
                      'AMBIGUOUS_MODEL_FIXTURE')
            results.append({'source': source, 'players': [a, b], 'status': status,
                            'matches': len(candidates),
                            'model_fixtures_on_listed_day': same_day,
                            'listed_day_paris': listed_day, 'decision': 'NO_BET'})
            continue
        m, reversed_order = candidates[0]
        # A player pair can recur at a different tournament. Require a
        # compatible event name when the bookmaker provides one.
        competition = norm(quote.get('competition_display', '')).replace('articopen', 'arcticopen')
        tournament = norm(m.get('tournament', ''))
        if competition and (not tournament or competition not in tournament):
            results.append({'source': source, 'players': [a, b],
                            'bwf_match_id': m.get('bwf_match_id'),
                            'status': 'TOURNAMENT_MISMATCH', 'decision': 'NO_BET'})
            continue
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
            if reversed_order:
                prob = 1.0 - prob
            if not 0 < prob < 1 or min(prices) <= 1:
                raise ValueError('Invalid probability or price')
        except (ValueError, TypeError, KeyError, AttributeError):
            results.append({'source': source, 'players': [a, b], 'status': 'INVALID_INPUT'})
            continue
        timing_status = timing_evidence(model_at, seen_at, m.get('start_utc'), m.get('status'))
        flags = ['MARKET_DETAIL_UNVERIFIED', 'KICKOFF_UNVERIFIED', 'EXECUTABLE_PRICE_UNVERIFIED', 'BOOKMAKER_PRICE_TIMESTAMP_UNKNOWN']
        # A public listing can establish a displayed day, not an actual start
        # time or the bookmaker's price update time. Preserve both separately.
        quote_day_basis = quote.get('day_basis')
        quote_verification = quote.get('verification')
        overround = sum(1 / x for x in prices) - 1
        observed_before_model_fixture_day = bool(
            listed_day and observed[:10] < listed_day)
        # This calendar check is diagnostic only, never kickoff confirmation.
        if age < 0:
            flags.append('MODEL_AFTER_OBSERVATION')
        if age > 2700:
            flags.append('MODEL_OLDER_THAN_45_MIN')
        if str(m.get('status', '')).startswith('DATE_ONLY_'):
            flags.append('BWF_KICKOFF_DATE_ONLY')
        results.append({'source': source, 'bwf_match_id': m['bwf_match_id'], 'players': [m['player_b'], m['player_a']] if reversed_order else [m['player_a'], m['player_b']],
                        'prematch_timing_status': timing_status,
                        'kickoff_source_verified': False,
                        'observed_at_utc': observed, 'model_asof_utc': m['asof_utc'], 'model_age_seconds': round(age),
                        'model_probability_a': prob,
                        'bookmaker_player_order_reversed': reversed_order,
                        'odds': prices,
                        'market_overround_pct': round(overround * 100, 2),
                        'market_implied_probability_raw': [round(1 / x, 6) for x in prices],
                        'market_implied_probability_normalized': [round((1 / x) / (1 + overround), 6) for x in prices],
                        'quote_listed_day_paris': listed_day,
                        'quote_day_basis': quote_day_basis,
                        'quote_verification': quote_verification,
                        'bookmaker_price_updated_at_utc': None,
                        'actual_kickoff_at_utc': None,
                        'observation_before_listed_calendar_day': observed_before_model_fixture_day,
                        'ev_interpretation': 'ILLUSTRATIVE_ONLY_NOT_VALIDATED_PREMATCH',
                        'illustrative_ev_pct': [round((prob * prices[0] - 1) * 100, 2), round(((1-prob) * prices[1] - 1) * 100, 2)],
                        'flags': flags, 'status': 'UNVERIFIED_SHADOW_ONLY', 'decision': 'NO_BET'})
    # Report coverage separately from illustrative EV. Repeated observations
    # and missing timestamps must never be counted as validated opportunities.
    status_counts = {}
    for item in results:
        status = item.get('status', 'UNKNOWN')
        status_counts[status] = status_counts.get(status, 0) + 1
    verified_prematch_count = sum(
        1 for item in results if item.get('valid_pre_match_T0') is True
        and item.get('executable_price_verified') is True
        and item.get('decision') != 'NO_BET'
    )
    # A bookmaker observation is not an independent BWF match. Count unique
    # fixtures separately to prevent inflated sample sizes in reporting.
    matched_fixture_ids = sorted({
        str(item['bwf_match_id']) for item in results
        if item.get('bwf_match_id') and item.get('status') == 'UNVERIFIED_SHADOW_ONLY'
    })
    unmatched_observations = [
        {'source': item.get('source'), 'players': item.get('players'),
         'status': item.get('status'), 'listed_day_paris': item.get('listed_day_paris')}
        for item in results if item.get('status') != 'UNVERIFIED_SHADOW_ONLY'
    ]
    verification_summary = {
        'unique_matched_fixture_count': len(matched_fixture_ids),
        'matched_fixture_ids': matched_fixture_ids,
        'unmatched_observation_count': len(unmatched_observations),
        'unmatched_observations': unmatched_observations,
        'prematch_timing_status_counts': {
            key: sum(1 for item in results if item.get('prematch_timing_status') == key)
            for key in sorted({item['prematch_timing_status'] for item in results if item.get('prematch_timing_status')})
        },
        'model_fixture_count': len(models),
        'public_quote_count': len(observations),
        'comparison_count': len(results),
        'status_counts': status_counts,
        'verified_prematch_count': verified_prematch_count,
        'validated_roi_pct': None,
        'missing_kickoff_count': sum(
            1 for item in results if 'KICKOFF_UNVERIFIED' in item.get('flags', [])
            or 'BWF_KICKOFF_DATE_ONLY' in item.get('flags', [])
        ),
        'missing_executable_price_count': sum(
            1 for item in results if 'EXECUTABLE_PRICE_UNVERIFIED' in item.get('flags', [])
        ),
    }
    output = {'verification_summary': verification_summary, 'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'policy': 'RESEARCH_ONLY_NO_BET_NO_T0', 'comparison_count': len(results),
              'rejected_sources': rejected, 'source_audit': source_audit, 'results': results}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'latest.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"V3.2 comparisons: {len(results)}; all unverified, no bets")


if __name__ == '__main__':
    main()
