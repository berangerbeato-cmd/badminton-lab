#!/usr/bin/env python3
"""Badminton Lab: date-safe BWF Elo historical analysis for 2025 and 2026.

Research only. No historical bookmaker odds, no paper-bet selection, no ROI.
Replays the already-frozen Elo K and 2024 Platt coefficients without refitting
from 2025 or 2026 outcomes. Each UTC calendar day uses ratings from BEFORE that
calendar day. Cross-checks the 2025 replay against the existing holdout CSV.

Run on the existing GitHub repository using Python 3.12 standard library:
  python badminton_historical_backtest.py
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from itertools import groupby
from pathlib import Path
import sys

from elo_model import elo_p, load_matches
from elo_calibrate_daily import calibrate

SCHEMA = 'badminton_historical_backtest_v1'
POLICY = 'HISTORICAL_MODEL_EVALUATION_ONLY_NO_ODDS_NO_PNL_NO_ROI_NO_T0'
YEAR_START = 2025
YEAR_END = 2026
CSV_FIELDS = [
    'match_id','date','year','tournament','round','player_a_id','player_a',
    'player_b_id','player_b','winner','score','cold_start',
    'elo_a_pre','elo_b_pre','raw_p_a','calibrated_p_a','fair_odds_a',
    'fair_odds_b','favorite_id','favorite','favorite_probability',
    'favorite_won','brier_calibrated','log_loss_calibrated',
    'historical_bookmaker_odds','historical_bookmaker_verified','betting_roi'
]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _load_configuration(model_dir: Path):
    baseline_bytes = (model_dir/'elo_report.json').read_bytes()
    calib_bytes = (model_dir/'elo_calibration_report.json').read_bytes()
    raw = json.loads(baseline_bytes)
    cal = json.loads(calib_bytes)
    if (raw.get('model') != 'bwf_elo_ms_v0' or
            raw.get('validation_year') != 2024 or
            raw.get('holdout_test_year') != 2025 or
            cal.get('model') != 'bwf_elo_ms_platt_v1' or
            cal.get('baseline_model') != 'bwf_elo_ms_v0' or
            cal.get('fit_year') != 2024 or cal.get('holdout_year') != 2025):
        raise ValueError('Model provenance no longer matches the pre-2025 training protocol')
    k = int(raw['chosen_k'])
    if not 0 < k < 200 or int(cal['k']) != k:
        raise ValueError('Calibration K differs from original frozen Elo selection')
    slope, intercept = float(cal['platt_slope']), float(cal['platt_intercept'])
    if not all(math.isfinite(v) for v in (slope,intercept)) or not .1 <= slope <= 3:
        raise ValueError('Invalid 2024 calibration coefficients')
    return k, slope, intercept, raw, cal, {
        'baseline_report_sha256': hashlib.sha256(baseline_bytes).hexdigest(),
        'calibration_report_sha256': hashlib.sha256(calib_bytes).hexdigest(),
    }


def chronological_replay(matches, k: int, slope: float, intercept: float,
                         years=(2025, 2026)) -> list[dict]:
    """Same day never sees any other result from that day, regardless of order."""
    if tuple(years) != (2025, 2026):
        raise ValueError('This pre-registered analysis uses exactly 2025 and 2026')
    sorted_matches = sorted(matches, key=lambda m: (m.date, m.key))
    rating = defaultdict(lambda: 1500.0)
    seen = Counter()
    rows = []
    unique = set()
    for date, day_iter in groupby(sorted_matches, key=lambda m: m.date):
        batch = list(day_iter)
        # Compute all forecasts from the previous day's state, then update Elo.
        delta = defaultdict(float)
        for match in batch:
            if match.key in unique:
                raise ValueError('Duplicate BWF match identifier: ' + match.key)
            unique.add(match.key)
            if match.p1_id == match.p2_id or match.winner not in (1,2):
                raise ValueError('Invalid historical match')
            raw_p = elo_p(rating[match.p1_id], rating[match.p2_id])
            y = int(match.winner == 1)
            if int(date[:4]) in years:
                p = calibrate(raw_p, slope, intercept)
                favorite_a = p >= .5
                favorite_p = p if favorite_a else 1-p
                outcome_p = p if y else 1-p
                rows.append({
                    'match_id':match.key, 'date':date,'year':int(date[:4]),
                    'tournament':match.tourney,'round':match.round,
                    'player_a_id':match.p1_id,'player_a':match.p1_name,
                    'player_b_id':match.p2_id,'player_b':match.p2_name,
                    'winner':match.winner,'score':match.score,
                    'cold_start':int(seen[match.p1_id]==0 or seen[match.p2_id]==0),
                    'elo_a_pre':round(rating[match.p1_id],4),
                    'elo_b_pre':round(rating[match.p2_id],4),
                    'raw_p_a':round(raw_p,8),
                    'calibrated_p_a':round(p,8),
                    'fair_odds_a':round(1/p,4),
                    'fair_odds_b':round(1/(1-p),4),
                    'favorite_id':match.p1_id if favorite_a else match.p2_id,
                    'favorite':match.p1_name if favorite_a else match.p2_name,
                    'favorite_probability':round(favorite_p,8),
                    'favorite_won':int((favorite_a and y==1) or (not favorite_a and y==0)),
                    'brier_calibrated':(p-y)**2,
                    'log_loss_calibrated':-math.log(outcome_p),
                    'historical_bookmaker_odds':'',
                    'historical_bookmaker_verified':False,
                    'betting_roi':'',
                })
            change = k*(y-raw_p)
            delta[match.p1_id] += change
            delta[match.p2_id] -= change
        for match in batch:
            seen[match.p1_id] += 1
            seen[match.p2_id] += 1
        for pid, increment in delta.items():
            rating[pid] += increment
    return rows


def validate_against_holdout(rows: list[dict], model_dir: Path, baseline, calibrated) -> dict:
    """Hard fail when BWF replay differs from the already-archived 2025 test."""
    path = model_dir/'holdout_2025.csv'
    with path.open(encoding='utf-8',newline='') as handle:
        originals = list(csv.DictReader(handle))
    actual = [r for r in rows if r['year']==2025]
    if len(originals) < 100 or len(actual) != len(originals):
        raise ValueError(f'2025 archive mismatch: replay={len(actual)}, holdout={len(originals)}')
    by_id = {x['match_id']:x for x in actual}
    if len(by_id) != len(actual):
        raise ValueError('Duplicate 2025 identifiers in replay')
    for old in originals:
        r=by_id.get(old['match_id'])
        if not r or (r['date'],r['player_a_id'],r['player_b_id'],r['winner']) != (
                old['date'],old['player_a_id'],old['player_b_id'],int(old['winner'])):
            raise ValueError('Holdout match identity/outcome changed: '+old['match_id'])
        if abs(r['raw_p_a']-float(old['p_a']))>0.000000015:
            raise ValueError('Holdout probability changed: '+old['match_id'])
        if int(r['cold_start']) != int(old['cold_start']):
            raise ValueError('Holdout cold-start status changed: '+old['match_id'])
    expected=baseline['test_2025']
    claimed=calibrated['holdout_2025']
    summary=summarize(actual)
    if len(actual)!=expected['n'] or len(actual)!=claimed['n']:
        raise ValueError('2025 sample count disagrees with baseline')
    # Brier for raw Elo is validated directly rather than comparing rounded values.
    raw_brier=sum((r['raw_p_a']-int(r['winner']==1))**2 for r in actual)/len(actual)
    if abs(raw_brier-expected['brier'])>0.000002:
        raise ValueError('Raw 2025 Brier disagrees with baseline')
    if abs(summary['brier']-claimed['calibrated_brier'])>0.000002:
        raise ValueError('Calibrated 2025 Brier disagrees with frozen report')
    if abs(summary['accuracy']-claimed['accuracy'])>0.000002:
        raise ValueError('2025 accuracy disagrees with frozen report')
    return {'holdout_rows_verified':len(originals),
            'holdout_csv_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'max_probability_difference':max(abs(by_id[o['match_id']]['raw_p_a']-float(o['p_a'])) for o in originals)}


def wilson_interval(wins: int, n: int) -> list[float] | None:
    if not n: return None
    z=1.959963984540054
    p=wins/n
    den=1+z*z/n
    cen=(p+z*z/(2*n))/den
    half=z/den*math.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return [round(max(0,cen-half),4),round(min(1,cen+half),4)]


def summarize(rows: list[dict]) -> dict:
    n=len(rows)
    if not n:
        return {'n':0,'accuracy':None,'brier':None,'log_loss':None,
                'favorite_wins':0,'confidence_interval_95':None,'cold_start':0,
                'upsets':0}
    wins=sum(int(r['favorite_won']) for r in rows)
    return {
        'n':n,
        'favorite_wins':wins,
        'accuracy':round(wins/n,6),
        'confidence_interval_95':wilson_interval(wins,n),
        'brier':round(sum(r['brier_calibrated'] for r in rows)/n,6),
        'log_loss':round(sum(r['log_loss_calibrated'] for r in rows)/n,6),
        'cold_start':sum(int(r['cold_start']) for r in rows),
        'upsets':n-wins,
    }


def group_confidence(rows: list[dict]) -> list[dict]:
    bands=((.5,.6),(.6,.7),(.7,.8),(.8,.9),(.9,1.000001))
    output=[]
    for lo,hi in bands:
        segment=[r for r in rows if lo <= r['favorite_probability'] < hi]
        total=len(segment)
        output.append({
            'band':f'{int(lo*100)}-{int(min(100,hi*100))}%',
            'n':total,
            'favorite_wins':sum(r['favorite_won'] for r in segment),
            'mean_predicted_probability':round(sum(r['favorite_probability'] for r in segment)/total,6) if total else None,
            'observed_win_rate':round(sum(r['favorite_won'] for r in segment)/total,6) if total else None,
        })
    return output


def report_for(rows: list[dict], baseline: dict, provenance: dict, quality: dict,
               verification: dict) -> dict:
    by_year={str(year):summarize([r for r in rows if r['year']==year]) for year in (2025,2026)}
    by_month={}
    for month in sorted(set(r['date'][:7] for r in rows)):
        by_month[month]=summarize([r for r in rows if r['date'].startswith(month)])
    report={
        'schema_version':SCHEMA,'generated_at_utc':utc_now(),'policy':POLICY,
        'scope':'BWF completed MS, years 2025 and 2026; 2026 as-of archive available when run',
        'train_period':'2018–2023','elo_k_fitted_on':2024,
        'platt_calibration_fitted_on':2024,
        'independent_holdout_year':2025,
        'retrospective_forward_replay_year':2026,
        'elo_k':baseline['chosen_k'],
        'historical_bookmaker_odds_present':False,
        'historical_market_roi_eur':None,
        'validated_historical_bets':0,
        'historical_pre_match_quote_verified':False,
        'model_metrics':{'2025':by_year['2025'],'2026':by_year['2026'],
                         'combined_exploratory':summarize(rows)},
        'confidence_calibration':{year:group_confidence([r for r in rows if r['year']==int(year)])
                                  for year in ('2025','2026')},
        'monthly_metrics':by_month,
        'model_report_source_sha256':provenance,
        'holdout_replay_verification':verification,
        'historical_source_quality':quality,
        'methodology':[
            'Frozen Elo K and Platt coefficients were chosen using 2024, never on the 2025 holdout.',
            '2025 predictions must match every row of pre-existing holdout_2025.csv.',
            'The 2026 replay uses outcomes from earlier UTC calendar days only.',
            'All matches on a given day use the same previous-day rating state.',
            '2026 is a retrospective chronological replay, not archived live T0 predictions.',
            'No bookmaker quote history exists in this dataset; fair odds are mathematical inverses of model probabilities, NOT offered prices.',
            'Favorite hit rate and Brier score are model evaluation, not return on betting.',
            'Confidence subgroups are descriptive and must not be optimized as if they were a clean future test.',
        ]
    }
    return report


def write_outputs(report: dict, rows: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True,exist_ok=True)
    # Derived outputs only. No edits to BWF, old snapshots, odds, sheets, or the paper ledger.
    (out_dir/'latest.json').write_text(json.dumps(report,indent=2,sort_keys=True,ensure_ascii=False)+'\n',encoding='utf-8')
    with (out_dir/'predictions_2025_2026.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            safe={k:r.get(k,'') for k in CSV_FIELDS}
            writer.writerow(safe)
    with (out_dir/'confidence_2025_2026.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=['year','band','n','favorite_wins','mean_predicted_probability','observed_win_rate'])
        writer.writeheader()
        for year in ('2025','2026'):
            for b in report['confidence_calibration'][year]:
                writer.writerow({'year':year,**b})


def run(root: Path, output: Path) -> dict:
    model_dir=root/'data'/'model'
    k,slope,intercept,baseline,calibrated,hashes=_load_configuration(model_dir)
    matches,quality=load_matches(root/'data'/'bwf')
    predictions=chronological_replay(matches,k,slope,intercept)
    verification=validate_against_holdout(predictions,model_dir,baseline,calibrated)
    report=report_for(predictions,baseline,hashes,quality,verification)
    target=output.resolve()
    required=(root/'data'/'historical_backtest').resolve()
    if target!=required:
        raise ValueError('Output directory must equal data/historical_backtest (source protection)')
    write_outputs(report,predictions,target)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('.'))
    p.add_argument('--output',type=Path,default=None)
    args=p.parse_args()
    root=args.root.resolve()
    output=(args.output or root/'data'/'historical_backtest')
    try:
        r=run(root,output)
    except (OSError,ValueError,KeyError,TypeError,OverflowError) as exc:
        print(f'Backtest rejected: {type(exc).__name__}: {exc}',file=sys.stderr)
        sys.exit(2)
    print(json.dumps({'status':'HISTORICAL_MODEL_ANALYSIS_ONLY',
                      'model_metrics':r['model_metrics'],
                      'holdout_verified':r['holdout_replay_verification']['holdout_rows_verified'],
                      'bookmaker_roi':None,'validated_bets':0},ensure_ascii=False))

if __name__=='__main__':
    main()
