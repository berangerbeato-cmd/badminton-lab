#!/usr/bin/env python3
"""Aggregate immutable V3.2 shadow observations; never infer realized ROI."""
import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def summarize(ledger):
    runs = []
    flags = Counter()
    sources = Counter()
    unique_matches = set()
    observations = 0
    for folder in sorted(ledger.iterdir()) if ledger.is_dir() else []:
        if not folder.is_dir() or not folder.name.isdigit():
            continue
        manifest_file = folder / 'manifest.json'
        comparison_file = folder / 'comparison.json'
        if not manifest_file.is_file() or not comparison_file.is_file():
            continue
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        comparison = json.loads(comparison_file.read_text(encoding='utf-8-sig'))
        if manifest.get('run_id') != folder.name:
            raise ValueError(f'Ledger run id mismatch: {folder}')
        if comparison.get('policy') != 'RESEARCH_ONLY_NO_BET_NO_T0':
            raise ValueError(f'Unexpected policy: {folder}')
        rows = comparison.get('results', [])
        if not isinstance(rows, list):
            raise ValueError(f'Invalid results: {folder}')
        for row in rows:
            if row.get('decision') != 'NO_BET':
                raise ValueError(f'Unexpected decision: {folder}')
            observations += 1
            flags.update(row.get('flags', []))
            if row.get('bwf_match_id'):
                unique_matches.add(row['bwf_match_id'])
        sources.update(f"{name}:{info.get('status', 'UNKNOWN')}" for name, info in
                       comparison.get('source_audit', {}).items())
        runs.append({'run_id': folder.name, 'comparison_count': len(rows)})
    return {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'policy': 'SHADOW_ONLY_NO_BET',
        'run_count': len(runs),
        'observation_count': observations,
        'unique_bwf_matches': len(unique_matches),
        'quality_flag_counts': dict(flags),
        'source_status_counts': dict(sources),
        'roi': None,
        'roi_status': 'NOT_MEASURABLE_WITHOUT_VERIFIED_PREMATCH_ODDS_AND_SETTLED_RESULTS',
        'runs': runs,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger', type=Path, default=Path('data/odds_shadow/run_ledger'))
    p.add_argument('--out', type=Path, default=Path('data/odds_shadow/ledger_summary.json'))
    args = p.parse_args()
    report = summarize(args.ledger)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'runs': report['run_count'], 'observations': report['observation_count'],
                      'roi_status': report['roi_status']}))


if __name__ == '__main__':
    main()
