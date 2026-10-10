#!/usr/bin/env python3
"""Archive one immutable V3.2 shadow observation per GitHub Actions run. NO BET."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

FILES = {
    'model': 'data/model_v3_2_live/latest_report.json',
    'upcoming': 'data/model_v3_2_live/upcoming_latest.csv',
    'comparison': 'data/odds_shadow/comparison_v3_auto/latest.json',
    'quality': 'data/odds_shadow/quality_gate/latest.json',
    'unibet': 'data/odds_shadow/unibet_fr/latest.json',
    'netbet': 'data/odds_shadow/netbet_quotes/latest.json',
}


def archive(root, destination, run_id):
    if not re.fullmatch(r'[0-9]{1,20}', run_id):
        raise ValueError('run-id must be a GitHub numeric run id')
    target = destination / run_id
    if target.exists():
        raise FileExistsError(f'Immutable run already exists: {target}')
    required = ('model', 'upcoming', 'comparison', 'quality')
    missing = [key for key in required if not (root / FILES[key]).is_file()]
    if missing:
        raise FileNotFoundError('Required snapshot(s) missing: ' + ', '.join(missing))
    blobs = {}
    for key, path in FILES.items():
        source = root / path
        if source.is_file():
            blobs[key] = (path, source.read_bytes())
    comparison = json.loads(blobs['comparison'][1].decode('utf-8-sig'))
    quality = json.loads(blobs['quality'][1].decode('utf-8-sig'))
    if comparison.get('policy') != 'RESEARCH_ONLY_NO_BET_NO_T0':
        raise ValueError('Comparison not research-only')
    if quality.get('policy') != 'RESEARCH_ONLY_NO_BET':
        raise ValueError('Quality not research-only')
    if comparison.get('generated_at_utc') != quality.get('comparison_generated_at_utc'):
        raise ValueError('Quality does not match comparison snapshot')
    if comparison.get('comparison_count') != len(comparison.get('results', [])):
        raise ValueError('Comparison count mismatch')
    if quality.get('comparison_count') != len(quality.get('rows', [])):
        raise ValueError('Quality count mismatch')
    if any(row.get('decision') != 'NO_BET' for row in comparison['results']):
        raise ValueError('Unexpected betting decision in comparison')
    if any(row.get('decision') != 'NO_BET' for row in quality['rows']):
        raise ValueError('Unexpected betting decision in quality')
    manifest = {
        'schema_version': 1,
        'run_id': run_id,
        'archived_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'policy': 'SHADOW_ONLY_NO_BET',
        'comparison_count': comparison['comparison_count'],
        'source_audit': comparison.get('source_audit', {}),
        'files': {key: {'source': path, 'sha256': hashlib.sha256(data).hexdigest(),
                        'size_bytes': len(data)}
                  for key, (path, data) in blobs.items()},
    }
    target.mkdir(parents=True, exist_ok=False)
    for key, (path, data) in blobs.items():
        (target / Path(path).name.replace('latest', key)).write_bytes(data)
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path('.'))
    p.add_argument('--out', type=Path, default=Path('data/odds_shadow/run_ledger'))
    p.add_argument('--run-id', required=True)
    args = p.parse_args()
    manifest = archive(args.root, args.out, args.run_id)
    print(json.dumps({'run_id': manifest['run_id'], 'files': len(manifest['files']),
                      'comparison_count': manifest['comparison_count'], 'policy': manifest['policy']}))


if __name__ == '__main__':
    main()
