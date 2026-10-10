#!/usr/bin/env python3
"""Badminton Lab: audit prudent des cotes publiques (shadow, aucun pari).

Usage: python odds_diagnostics.py --dir data/odds_shadow
Produit quote_diagnostics.csv et quote_diagnostics_summary.json.
Ne confirme jamais qu'une cote est executable en ligne.
"""
import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ('source operator observed_at_utc source_status player_1 player_2 odds_1 odds_2 '
          'listed_start_paris market_verification decision reason source_file').split()
SKIP = {'netbet_research', 'comparison', 'comparison_v3', 'value_reports'}


def latest_report(folder):
    latest = folder / 'latest.json'
    if latest.is_file():
        return latest
    candidates = sorted(p for p in folder.glob('*.json') if p.name != 'latest_summary.json')
    return candidates[-1] if candidates else None


def audit(root):
    records = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name in SKIP:
            continue
        report = latest_report(folder)
        if report is None:
            continue
        try:
            data = json.loads(report.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('Expected JSON object')
        except (ValueError, OSError) as exc:
            records.append(dict(source=folder.name, decision='ERROR', reason=str(exc), source_file=str(report)))
            continue
        base = dict(source=folder.name, operator=data.get('operator', ''),
                    observed_at_utc=data.get('observed_at_utc', ''),
                    source_status=data.get('status', ''), source_file=str(report))
        quotes = data.get('quotes') or []
        if not quotes:
            records.append(dict(**base, decision='NO_QUOTE',
                reason=data.get('parser_notes') or data.get('error') or data.get('robots_status') or data.get('status') or 'UNKNOWN'))
            continue
        seen = set()
        for q in quotes:
            if not isinstance(q, dict):
                continue
            key = (q.get('player_1_display'), q.get('player_2_display'), q.get('odds_1'),
                   q.get('odds_2'), q.get('listed_start_paris'), q.get('market'))
            if key in seen:
                continue
            seen.add(key)
            verification = q.get('market_verification') or q.get('verification') or ''
            if data.get('operator_kind') == 'RETAIL_NOT_ONLINE' or verification.startswith('RETAIL_'):
                reason = 'Point de vente uniquement; pas de cote executable en ligne confirmee'
            elif verification == 'LISTING_ONLY_NEEDS_DETAIL':
                reason = 'Page liste seulement; marche detail et heure de mise a jour non verifies'
            elif verification.startswith('LISTING_'):
                reason = 'Marche et heure de debut non verifies sur page detail'
            else:
                reason = 'Disponibilite, marche ou horodatage bookmaker non verifies'
            records.append(dict(**base, player_1=q.get('player_1_display', ''),
                player_2=q.get('player_2_display', ''), odds_1=q.get('odds_1', ''),
                odds_2=q.get('odds_2', ''), listed_start_paris=q.get('listed_start_paris', ''),
                market_verification=verification, decision='OBSERVED_NOT_ACTIONABLE', reason=reason))
    return records


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dir', type=Path, default=Path('data/odds_shadow'))
    args = p.parse_args()
    if not args.dir.is_dir():
        p.error('Dossier absent: ' + str(args.dir))
    rows = audit(args.dir)
    output = args.dir / 'quote_diagnostics.csv'
    with output.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    summary = {'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
               'policy': 'SHADOW_ONLY_NO_BET', 'total_rows': len(rows),
               'decisions': dict(Counter(r['decision'] for r in rows)),
               'sources': {source: {'rows': len(group),
                                    'observed': sum(r['decision'] == 'OBSERVED_NOT_ACTIONABLE' for r in group),
                                    'status': group[0].get('source_status', '')}
                           for source in sorted({r['source'] for r in rows})
                           for group in [[r for r in rows if r['source'] == source]]},
               'warning': 'Observed odds are not confirmed executable betting prices.'}
    (args.dir / 'quote_diagnostics_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(rows)} lignes : {output}')
    print(json.dumps(summary['decisions'], ensure_ascii=False))


if __name__ == '__main__':
    main()
