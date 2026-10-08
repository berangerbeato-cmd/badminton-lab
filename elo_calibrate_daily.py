#!/usr/bin/env python3
"""Calibrate the existing BWF Elo MS baseline and publish genuine future-only forecasts.

Standard-library only. Training decisions: K selected using 2024 in elo_model.py;
Platt slope/intercept trained on 2024 only; 2025 held out for final evaluation.
The upcoming feed excludes started/completed matches and never reconstructs T0.

python elo_calibrate_daily.py --data-dir data/bwf --model-dir data/model
python elo_calibrate_daily.py --self-test
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from itertools import groupby
import json
import math
from pathlib import Path
from typing import Iterable
import gzip

from elo_model import load_matches, simulate, elo_p

EPS = 1e-7
MODEL_NAME = 'bwf_elo_ms_platt_v1'


def logit(p: float) -> float:
    p = min(1.0 - EPS, max(EPS, p))
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        e = math.exp(-min(700, x))
        return 1.0 / (1.0 + e)
    e = math.exp(max(-700, x))
    return e / (1.0 + e)


def fit_platt(records: Iterable[dict], ridge: float = 5.0) -> tuple[float, float]:
    """Regularized binary logistic fit of outcome on raw Elo logit."""
    data = [(logit(float(m['p_a'])), 1.0 if m['winner'] == 1 else 0.0) for m in records]
    if len(data) < 100:
        raise ValueError('Cannot fit calibration with fewer than 100 validation matches')
    a, b = 1.0, 0.0
    for _ in range(60):
        ga, gb = ridge * (a-1.0), ridge * b
        haa, hab, hbb = ridge, 0.0, ridge
        for x, y in data:
            p = sigmoid(a*x+b)
            d = p-y
            w = p*(1-p)
            ga += d*x
            gb += d
            haa += w*x*x
            hab += w*x
            hbb += w
        det = haa*hbb - hab*hab
        if det <= 0:
            raise ValueError('Singular calibration Hessian')
        step_a = (ga*hbb-gb*hab)/det
        step_b = (gb*haa-ga*hab)/det
        # Control steps for pathological datasets; practical Elo logits are bounded.
        step_a = max(-0.5, min(0.5, step_a))
        step_b = max(-0.5, min(0.5, step_b))
        a -= step_a
        b -= step_b
        if abs(step_a) + abs(step_b) < 1e-9:
            break
    if not (math.isfinite(a) and math.isfinite(b) and 0.1 <= a <= 3):
        raise ValueError('Calibration failed sanity checks')
    return a, b


def calibrate(p: float, a: float, b: float) -> float:
    return min(1-EPS, max(EPS, sigmoid(a*logit(p)+b)))


def measure(rows: list[dict], slope: float, intercept: float) -> dict:
    if not rows:
        raise ValueError('Empty evaluation dataset')
    n, brier, log_loss, correct, raw_brier, raw_loss = 0, 0., 0., 0, 0., 0.
    bin_stats = defaultdict(lambda: [0, 0., 0])
    for r in rows:
        y = int(r['winner'] == 1)
        raw_p = float(r['p_a'])
        p = calibrate(raw_p, slope, intercept)
        n += 1
        brier += (p-y)**2
        raw_brier += (raw_p-y)**2
        log_loss -= y*math.log(p)+(1-y)*math.log1p(-p)
        raw_loss -= y*math.log(raw_p)+(1-y)*math.log1p(-raw_p)
        correct += int((p>=.5)==bool(y))
        k = min(9, int(p*10))
        bin_stats[k][0] += 1
        bin_stats[k][1] += p
        bin_stats[k][2] += y
    return {'n': n, 'accuracy': round(correct/n, 6),
            'calibrated_brier': round(brier/n, 6),
            'raw_brier': round(raw_brier/n, 6),
            'calibrated_log_loss': round(log_loss/n, 6),
            'raw_log_loss': round(raw_loss/n, 6),
            'calibration_bins': [
                {'range': f'{k*10}-{(k+1)*10}%', 'n': z[0],
                 'predicted_mean': round(z[1]/z[0], 4),
                 'actual_win_rate': round(z[2]/z[0], 4)}
                for k,z in sorted(bin_stats.items())]}


def snapshot_ratings(matches, k: float, cutoff_day: str):
    """Use ONLY dated results strictly before current UTC day, no intraday leakage."""
    ratings = defaultdict(lambda: 1500.0)
    seen = Counter()
    for day, group_iter in groupby(matches, key=lambda m: m.date):
        if day >= cutoff_day:
            break
        delta = defaultdict(float)
        for m in group_iter:
            p = elo_p(ratings[m.p1_id], ratings[m.p2_id])
            change = k*((1 if m.winner == 1 else 0)-p)
            delta[m.p1_id] += change
            delta[m.p2_id] -= change
            seen[m.p1_id] += 1
            seen[m.p2_id] += 1
        for pid, change in delta.items():
            ratings[pid] += change
    return ratings, seen


def fixture_rows(data_dir: Path, ratings: dict, seen: Counter,
                 slope: float, intercept: float, k: int,
                 now: datetime, days: int = 7) -> list[dict]:
    """Source fixtures only from already collected BWF archives, never invent start times."""
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    indexes = json.loads((data_dir/'index.json').read_text(encoding='utf-8'))
    tournaments = {str(t['id']):t for t in indexes if isinstance(t, dict) and t.get('id') is not None}
    future = {}
    finished_keys = set()
    for file in sorted((data_dir/'matches').glob('*.json.gz')):
        tid = file.name.split('.')[0]
        with gzip.open(file, 'rt', encoding='utf-8') as f:
            raw = json.load(f)
        result = raw.get('results') or {}
        group = (result.get('by_time') or {}).get('time_group') or []
        # Different tournament responses may retain the old by_court structure.
        more = []
        for c in (result.get('by_court') or {}).values():
            if isinstance(c, dict):
                more.extend(c.values())
        for m in list(group)+more:
            if not isinstance(m, dict) or m.get('id') is None:
                continue
            if (m.get('draw_name') or '').split(' ')[0].upper() != 'MS':
                continue
            key=f'{tid}:{m["id"]}'
            if m.get('winner') in (1,2):
                finished_keys.add(key)
                future.pop(key, None)
                continue
            if key in finished_keys:
                continue
            p1,p2 = m.get('t1p1_detail') or {},m.get('t2p1_detail') or {}
            if not isinstance(p1,dict) or not isinstance(p2,dict) or not p1.get('id') or not p2.get('id'):
                continue
            if (m.get('t1p2_detail') or {}).get('id') or (m.get('t2p2_detail') or {}).get('id'):
                continue
            try:
                start = datetime.fromtimestamp(int(m['start_time']), tz=timezone.utc)
            except (KeyError,TypeError,ValueError,OverflowError):
                continue
            # BWF may put UTC midnight into start_time as a DATE PLACEHOLDER.
            # Its calendar day is informative, but midnight is NOT a verified
            # tip-off time. Keep the fixture visible throughout that day while
            # refusing to label it as an actionable T0 forecast.
            date_only = (start.hour == 0 and start.minute == 0 and start.second == 0)
            if date_only:
                if not (now.date() <= start.date() <= (now + timedelta(days=days)).date()):
                    continue
            elif not (now < start <= now + timedelta(days=days)):
                continue
            a_id,b_id = str(p1['id']),str(p2['id'])
            if a_id==b_id:
                continue
            rawp=elo_p(ratings[a_id],ratings[b_id])
            calibrated=calibrate(rawp,slope,intercept)
            future[key]={
                'bwf_match_id':key, 'start_utc':start.isoformat(timespec='seconds'),
                'tournament':(tournaments.get(tid) or {}).get('name') or f'BWF tournament {tid}',
                'round':str(m.get('round_name') or ''),
                'player_a_id':a_id,'player_a':p1.get('name_display') or '',
                'player_b_id':b_id,'player_b':p2.get('name_display') or '',
                'elo_a_asof':round(ratings[a_id],2),'elo_b_asof':round(ratings[b_id],2),
                'raw_p_a':round(rawp,8),'calibrated_p_a':round(calibrated,8),
                'fair_odds_a':round(1/calibrated,4),
                'fair_odds_b':round(1/(1-calibrated),4),
                'k':k,'slope':round(slope,8),'intercept':round(intercept,8),
                'asof_utc':now.isoformat(timespec='seconds'),
                'model':MODEL_NAME,
                'cold_start':int(seen[a_id]==0 or seen[b_id]==0),
                'status':('DATE_ONLY_START_UNVERIFIED_NO_BET' if date_only else 'MODEL_ONLY_NO_VERIFIED_BOOKMAKER_ODDS'),
                'source':'https://github.com/berangerbeato-cmd/badminton-lab/tree/main/data/bwf',
            }
    return sorted(future.values(),key=lambda r:(r['start_utc'],r['bwf_match_id']))


FIELDS = ['bwf_match_id','start_utc','tournament','round','player_a_id','player_a',
          'player_b_id','player_b','elo_a_asof','elo_b_asof','raw_p_a',
          'calibrated_p_a','fair_odds_a','fair_odds_b','k','slope','intercept',
          'asof_utc','model','cold_start','status','source']


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def run(data_dir: Path, model_dir: Path, now: datetime, days: int):
    report = json.loads((model_dir/'elo_report.json').read_text(encoding='utf-8'))
    if report.get('model') != 'bwf_elo_ms_v0':
        raise ValueError('Unsupported baseline model')
    k=int(report['chosen_k'])
    matches,quality=load_matches(data_dir)
    val,test,*_=simulate(matches,k)
    if len(val) < 100 or len(test) < 100:
        raise ValueError('Too few validation/holdout games')
    slope,intercept=fit_platt(val)
    v=measure(val,slope,intercept)
    t=measure(test,slope,intercept)
    # Check model can be reproduced from the immutable 2025 holdout BWF cache.
    if t['n'] != report['test_2025']['n'] or abs(t['raw_brier']-report['test_2025']['brier'])>0.000002:
        raise ValueError('Underlying 2025 holdout changed; audit before publishing')
    ratings,seen=snapshot_ratings(matches,k,now.date().isoformat())
    rows=fixture_rows(data_dir,ratings,seen,slope,intercept,k,now,days)
    model_dir.mkdir(parents=True,exist_ok=True)
    obj={
        'model':MODEL_NAME,'generated_at_utc':now.isoformat(),
        'baseline_model':'bwf_elo_ms_v0','k':k,
        'fit_year':2024,'holdout_year':2025,
        'method':'regularized Platt calibration of Elo logit using 2024 only',
        'platt_slope':round(slope,8),'platt_intercept':round(intercept,8),
        'ridge_strength':5.0,'validation_2024':v,'holdout_2025':t,
        'upcoming_count':len(rows),
        'snapshot_policy':'Rosters and start times from BWF cache; do not infer T0 for completed or already-started matches. Ratings exclude ALL same-UTC-day results.',
        'cold_start_policy':'Flag players without earlier completed matches; never treat unknown as 0 strength.',
        'trading_policy':'NO BET until a separately timestamped operator quote is verified before match, and stronger model-vs-market validation performed.',
        'data_quality':quality,
    }
    (model_dir/'elo_calibration_report.json').write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    write_csv(model_dir/'upcoming_latest.csv',rows)
    snapshot_path=model_dir/'snapshots'/f'{now.date().isoformat()}.csv'
    # Freeze the FIRST feed on a date, never rewrite past snapshots.
    if not snapshot_path.exists():
        write_csv(snapshot_path,rows)
    print(json.dumps({'model':MODEL_NAME,'fit_2024':v['n'],'holdout_2025':t['n'],
       'raw_brier_2025':t['raw_brier'],'calibrated_brier_2025':t['calibrated_brier'],
       'platt_slope':round(slope,5),'platt_intercept':round(intercept,5),
       'upcoming':len(rows), 'frozen_snapshot':str(snapshot_path)},ensure_ascii=False))


def self_test():
    assert abs(sigmoid(0)-.5)<1e-12
    assert abs(calibrate(.3,1,0)-.3)<1e-10
    assert abs(calibrate(.8,1,0)-.8)<1e-10
    rng = [{'p_a':(i+1)/302,'winner':1 if ((i*37)%101)< (i+1)/3 else 2} for i in range(301)]
    a,b=fit_platt(rng)
    assert math.isfinite(a) and math.isfinite(b)
    metrics=measure(rng,a,b)
    assert metrics['n']==301 and metrics['calibrated_brier']<.4
    from elo_model import Match
    sample=[Match('1','2024-04-01','T','R','a','A','b','B',1,'21-19 21-15'),
            Match('2','2024-04-01','T','R','a','A','b','B',2,'19-21 21-19 19-21'),
            Match('3','2024-04-02','T','R','a','A','b','B',1,'21-10 21-15')]
    rated,seen=snapshot_ratings(sample,48,'2024-04-02')
    assert rated['a']==1500 and rated['b']==1500 and seen['a']==2
    rated,seen=snapshot_ratings(sample,48,'2024-04-03')
    assert rated['a']>1500 and seen['a']==3
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp);(p/'matches').mkdir()
        (p/'index.json').write_text('[{"id": 11, "name": "Test Open"}]')
        def match(t,id,w):
            return {'id':id,'draw_name':'MS','winner':w,'start_time':int(t.timestamp()),
                    't1p1_detail':{'id':10,'name_display':'A'},
                    't2p1_detail':{'id':20,'name_display':'B'},
                    'round_name':'R16'}
        now=datetime(2026,10,8,5,tzinfo=timezone.utc)
        future=match(now+timedelta(hours=3),3,None)
        past=match(now-timedelta(hours=3),4,None)
        finished=match(now+timedelta(hours=3),5,1)
        with gzip.open(p/'matches'/'11.json.gz','wt') as f:
            json.dump({'results':{'by_time':{'time_group':[future,past,finished]}}},f)
        rows=fixture_rows(p,defaultdict(lambda:1500.0),Counter(),1,0,48,now,7)
        assert len(rows)==1 and rows[0]['bwf_match_id']=='11:3'
        assert rows[0]['cold_start']==1
        # A BWF midnight timestamp may be a calendar-only placeholder. It must
        # survive the morning import on the same date, but never be called a
        # verified prematch starting time.
        morning=datetime(2026,10,9,7,tzinfo=timezone.utc)
        placeholder=match(datetime(2026,10,9,0,tzinfo=timezone.utc),6,None)
        with gzip.open(p/'matches'/'11.json.gz','wt') as f:
            json.dump({'results':{'by_time':{'time_group':[placeholder]}}},f)
        morning_rows=fixture_rows(p,defaultdict(lambda:1500.0),Counter(),1,0,48,morning,7)
        assert len(morning_rows)==1 and morning_rows[0]['bwf_match_id']=='11:6'
        assert morning_rows[0]['status']=='DATE_ONLY_START_UNVERIFIED_NO_BET'
        next_morning=datetime(2026,10,10,7,tzinfo=timezone.utc)
        assert not fixture_rows(p,defaultdict(lambda:1500.0),Counter(),1,0,48,next_morning,7)
    print('ALL TESTS PASS: calibration, Elo, future fixtures, midnight placeholder, and no-T0 flag')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,default=Path('data/bwf'))
    p.add_argument('--model-dir',type=Path,default=Path('data/model'))
    p.add_argument('--days',type=int,default=7)
    p.add_argument('--self-test',action='store_true')
    args=p.parse_args()
    if args.self_test:
        self_test()
    else:
        run(args.data_dir,args.model_dir,datetime.now(timezone.utc),args.days)

if __name__=='__main__':
    main()
