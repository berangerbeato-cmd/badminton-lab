#!/usr/bin/env python3
"""Audit des cotes shadow Badminton Lab. Lecture seule, aucun pari.

Usage: python odds_diagnostics.py --dir data/odds_shadow
Sortie: data/odds_shadow/quote_diagnostics.csv
"""
import argparse
import csv
import json
from pathlib import Path

FIELDS = ['source', 'operator', 'observed_at_utc', 'source_status', 'player_1',
          'player_2', 'odds_1', 'odds_2', 'listed_start_paris',
          'market_verification', 'decision', 'reason', 'source_file']


def audit(root):
    records = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name == 'netbet_research':
            continue
        files = sorted(folder.glob('*.json'))
        if not files:
            continue
        latest = files[-1]
        try:
            data = json.loads(latest.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            records.append(dict(source=folder.name, decision='ERROR', reason=str(exc), source_file=str(latest)))
            continue
        quotes = data.get('quotes') or []
        if not quotes:
            records.append(dict(source=folder.name, operator=data.get('operator'),
                                observed_at_utc=data.get('observed_at_utc'),
                                source_status=data.get('status'), decision='NO_QUOTE',
                                reason=data.get('parser_notes') or data.get('error') or data.get('robots_status') or data.get('status'),
                                source_file=str(latest)))
        for q in quotes:
            kind = data.get('operator_kind', '')
            validation = q.get('market_verification', '')
            if kind == 'RETAIL_NOT_ONLINE':
                reason = 'Cote point de vente, non executable en ligne'
            elif validation == 'LISTING_ONLY_NEEDS_DETAIL':
                reason = 'Page liste uniquement; marche detail et horaire bookmaker non confirmes'
            else:
                reason = 'Verification prematch et executabilite insuffisantes'
            records.append(dict(source=folder.name, operator=data.get('operator'),
                                observed_at_utc=data.get('observed_at_utc'),
                                source_status=data.get('status'), player_1=q.get('player_1_display'),
                                player_2=q.get('player_2_display'), odds_1=q.get('odds_1'),
                                odds_2=q.get('odds_2'), listed_start_paris=q.get('listed_start_paris'),
                                market_verification=validation, decision='OBSERVED_NOT_ACTIONABLE',
                                reason=reason, source_file=str(latest)))
    return records


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dir', type=Path, default=Path('data/odds_shadow'))
    args = p.parse_args()
    if not args.dir.is_dir():
        p.error('Dossier de donnees absent: ' + str(args.dir))
    rows = audit(args.dir)
    dest = args.dir / 'quote_diagnostics.csv'
    with dest.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    print(f'{len(rows)} lignes de diagnostic dans {dest}')
    print('Ce rapport ne prouve aucune cote executable et ne calcule aucun pari.')

if __name__ == '__main__':
    main()
