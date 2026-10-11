#!/usr/bin/env python3
"""Summarize V3.2 bookmaker evidence without manufacturing a validated ROI."""
import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def summarize(data):
    rows = data.get('results', [])
    matched = [r for r in rows if r.get('status') == 'UNVERIFIED_SHADOW_ONLY'
               and r.get('bwf_match_id')]
    fixtures = {str(r['bwf_match_id']) for r in matched}
    blockers = Counter(flag for row in matched for flag in set(row.get('flags', [])))
    sources = Counter(row.get('source', 'UNKNOWN') for row in matched)
    timestamp = data.get('generated_at_utc')
    try:
        generated = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
        if generated.tzinfo is None:
            raise ValueError('missing timezone')
        age_hours = round((datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds() / 3600, 2)
        freshness = 'FRESH' if 0 <= age_hours <= 24 else 'STALE_OR_FUTURE'
    except (ValueError, TypeError):
        age_hours, freshness = None, 'UNKNOWN_TIMESTAMP'
    return {
        'snapshot_age_hours': age_hours,
        'snapshot_freshness': freshness,
        'generated_at_utc': data.get('generated_at_utc'),
        'policy': 'RESEARCH_ONLY_NO_BET',
        'model_fixtures': data.get('verification_summary', {}).get('model_fixture_count', 0),
        'public_quotes': data.get('verification_summary', {}).get('public_quote_count', 0),
        'unique_matched_fixtures': len(fixtures),
        'matched_bookmaker_observations': len(matched),
        'unmatched_observations': len(rows) - len(matched),
        'quotes_by_bookmaker': dict(sorted(sources.items())),
        'blockers': dict(sorted(blockers.items())),
        'verified_prematch_opportunities': data.get('verification_summary', {}).get('verified_prematch_count', 0),
        'validated_roi_pct': None,
        'conclusion': ('STALE_SNAPSHOT_NOT_ACTIONABLE' if freshness != 'FRESH' else
                       'INSUFFICIENT_VERIFIED_DATA_FOR_PROFITABILITY_ASSESSMENT'),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, default=Path('data/odds_shadow/comparison_v3_auto/latest.json'))
    p.add_argument('--out', type=Path, default=Path('data/odds_shadow/v32_decision_report/latest.json'))
    args = p.parse_args()
    report = summarize(json.loads(args.input.read_text(encoding='utf-8')))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
