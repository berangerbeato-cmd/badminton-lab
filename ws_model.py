"""Independent WS V3.2 training / daily inference using stateless shared math."""
import argparse
import csv
import hashlib
import gzip
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from elo_model import Match, K_CANDIDATES, load_matches
from badminton_features_v3 import fit_variant, predict, metrics, coefficient_map
from badminton_v3_2_walkforward import build_rows_raw, select_k, VARIANTS, calibration_bins
from badminton_v3_2_daily import snapshot_state, SELECTED_FEATURES
from ws_data import load_ws


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    text=json.dumps(obj, ensure_ascii=False, indent=None if path.suffix=='.gz' else 2, allow_nan=False)+'\n'
    if path.suffix=='.gz':
        path.write_bytes(gzip.compress(text.encode(),mtime=0))
    else: path.write_text(text)


def evaluate(rows, model=None):
    probs = [predict(r, model) if model else r['raw_p_a'] for r in rows]
    return {**metrics(rows, probs), 'calibration': calibration_bins(rows, probs)}


def train(matches, quality, out):
    # All selection decisions frozen before evaluating 2026. No holdout feedback.
    cache = {k: build_rows_raw(matches, k) for k in K_CANDIDATES}
    folds, pooled = {}, {n: ([], []) for n in VARIANTS}
    for year in (2022, 2023, 2024, 2025):
        k, trials = select_k(cache, year-1)
        rows = cache[k]
        fit = [r for r in rows if 2019 <= r['year'] < year]
        valid = [r for r in rows if r['year'] == year]
        if len(fit) < 100 or len(valid) < 100:
            raise ValueError(f'Insufficient fold {year}')
        fold = {'k': k, 'k_validation': trials, 'raw': evaluate(valid), 'variants': {}}
        for name, features in VARIANTS.items():
            model = fit_variant(fit, features)
            ps = [predict(r, model) for r in valid]
            fold['variants'][name] = metrics(valid, ps)
            pooled[name][0].extend(valid); pooled[name][1].extend(ps)
        folds[str(year)] = fold
    aggregate = {n: metrics(*pair) for n,pair in pooled.items()}
    baseline = aggregate['elo_baseline']['brier']
    # Conservative predeclared gate: >0.001 aggregate Brier gain, 3/4 better folds.
    eligible = ['elo_baseline'] + [n for n in VARIANTS if n != 'elo_baseline'
        and aggregate[n]['brier'] < baseline-0.001
        and sum(f['variants'][n]['brier'] < f['variants']['elo_baseline']['brier'] for f in folds.values()) >= 3]
    selected = min(eligible, key=lambda n:(aggregate[n]['brier'],len(VARIANTS[n])))
    k, trials = select_k(cache, 2025)
    rows = cache[k]
    fit = [r for r in rows if 2019 <= r['year'] <= 2025]
    models = {n: fit_variant(fit, features) for n,features in VARIANTS.items()}
    artifact = dict(discipline='WS', version='V3.2', k=k, selected=selected, fitted=models[selected],
        fit_start='2019-01-01', fit_end='2025-12-31', training_n=len(fit),
        training_sha256=hashlib.sha256(json.dumps([asdict(m) for m in matches if m.date < '2026'],sort_keys=True).encode()).hexdigest())
    artifact['model_id'] = hashlib.sha256(json.dumps(artifact,sort_keys=True).encode()).hexdigest()[:16]
    write(out/'model.json',artifact) # Frozen artifact persisted before holdout evaluation.
    test = [r for r in rows if r['year'] == 2026]
    test_probs = [predict(r,models[selected]) for r in test]
    groups = {}
    for label, pred in [('balanced',lambda r,p: .4<=p<=.6),('favorite60_80',lambda r,p:.6<=max(p,1-p)<.8),
                        ('favorite80plus',lambda r,p:max(p,1-p)>=.8)]:
        subset=[(r,p) for r,p in zip(test,test_probs) if pred(r,p)]
        groups[label]=metrics([r for r,p in subset],[p for r,p in subset])
    seen=Counter(); experience={}
    for day in sorted({m.date for m in matches}):
        batch=[m for m in matches if m.date==day]
        for m in batch: experience[m.key]=min(seen[m.p1_id],seen[m.p2_id])
        for m in batch: seen.update([m.p1_id,m.p2_id])
    for label, low, high in [('history0_4',0,5),('history5_19',5,20),('history20plus',20,100000)]:
        subset=[(r,p) for r,p in zip(test,test_probs) if low<=experience[r['match_id']]<high]
        groups[label]=metrics([r for r,p in subset],[p for r,p in subset])
    ms,_=load_matches(Path('data/bwf'))
    msrows=build_rows_raw([m for m in ms if m.date < quality['asof_exclusive']],64)
    msfit=fit_variant([r for r in msrows if 2019<=r['year']<=2025],SELECTED_FEATURES)
    mstest=[r for r in msrows if r['year']==2026]
    report=dict(policy='RETROSPECTIVE_BACKTEST_NOT_LIVE_NOT_BETS',quality=quality,folds=folds,
        aggregate_validation=aggregate,selected=selected,selection_rule='Brier gain >0.001 and better in >=3/4 folds; otherwise calibrated Elo',
        final_k=k,k_validation_2025=trials,coefficients=coefficient_map(models[selected]),
        test_2026={'raw_elo':evaluate(test),'calibrated_elo':evaluate(test,models['elo_baseline']),
                   'selected_ws':evaluate(test,models[selected])},
        test_variants_2026={n:evaluate(test,m) for n,m in models.items()},
        segments=groups,ms_same_period=evaluate(mstest,msfit),
        comparison_note='Same 2026 time window, different players and coverage; no claim WS inherently more predictable')
    # Tournament categories supplied separately in enrichment below.
    write(out/'backtest.json',report)
    write(out/'backtest_predictions_2026.json.gz',dict(policy='RETROSPECTIVE',predictions=[dict(r,p_a=p) for r,p in zip(test,test_probs)]))
    return artifact, report


def forecast(matches, artifact, fixture, asof):
    if artifact.get('discipline') != 'WS': raise ValueError('Wrong model discipline')
    start=datetime.fromisoformat(fixture['start_utc'].replace('Z','+00:00'))
    if start.tzinfo is None or asof.tzinfo is None: raise ValueError('Timezone required')
    if start <= asof: raise ValueError('Not a future fixture')
    if artifact.get('fit_end', '9999') >= asof.date().isoformat(): raise ValueError('Model training overlaps forecast')
    cutoff=asof.astimezone(timezone.utc).date().isoformat()
    prior=[m for m in matches if m.date < cutoff]
    a,b=str(fixture['player_a_id']),str(fixture['player_b_id'])
    counts=Counter(p for m in prior for p in (m.p1_id,m.p2_id))
    result={**fixture,'asof_utc':asof.isoformat(),'model_id':artifact['model_id'],'discipline':'WS',
            'history_a':counts[a],'history_b':counts[b],'latest_result':prior[-1].date if prior else None}
    if a==b or not counts[a] or not counts[b]:
        return dict(result,p_a=None,status='UNKNOWN_PLAYER_NO_BET')
    # Synthetic query row: its outcome is never used before its features/prediction.
    query=Match('__query__',start.astimezone(timezone.utc).date().isoformat(),fixture.get('tournament',''),'',a,'',b,'',1,'21-0 21-0')
    row=build_rows_raw(prior+[query],artifact['k'])[-1]
    probability=predict(row,artifact['fitted'])
    age=(asof.date()-datetime.fromisoformat(prior[-1].date).date()).days
    inactive=max((asof.date()-datetime.fromisoformat(max(m.date for m in prior if p in (m.p1_id,m.p2_id))).date()).days for p in (a,b))
    status='MODEL_READY'
    if age>7: status='STALE_NO_BET'
    elif min(counts[a],counts[b])<10: status='LOW_HISTORY_NO_BET'
    elif inactive>90: status='LONG_INACTIVITY_NO_BET'
    elif start.hour==start.minute==start.second==0: status='DATE_ONLY_NO_BET'
    return dict(result,p_a=probability,status=status,source_age_days=age,inactivity_days=inactive,
                features={n:row[n] for n in artifact['fitted']['features']},raw_p_a=row['raw_p_a'])


def daily(matches, quality, fixtures, artifact, out, now):
    if artifact['fit_end'] >= now.date().isoformat(): raise ValueError('Model training overlaps forecast')
    upcoming=[]
    for f in fixtures:
        start=datetime.fromisoformat(f['start_utc'])
        if now<start<=now+timedelta(days=14): upcoming.append(forecast(matches,artifact,f,now))
    ratings,seen,_=snapshot_state(matches,artifact['k'],now.date().isoformat())
    names={p:n for m in matches for p,n in [(m.p1_id,m.p1_name),(m.p2_id,m.p2_name)]}
    write(out/'ratings.json',[dict(player_id=p,name=names.get(p,p),elo=r,matches=seen[p]) for p,r in sorted(ratings.items(),key=lambda x:-x[1])])
    payload=dict(asof_utc=now.isoformat(),discipline='WS',model_id=artifact['model_id'],quality=quality,predictions=upcoming)
    write(out/'latest.json',payload)
    # Immutable real generation timestamp: never fabricate historical T0 snapshots.
    snapshot=out/'predictions'/f'{now.strftime("%Y%m%dT%H%M%S%fZ")}.json'
    if snapshot.exists(): raise ValueError('Snapshot already exists')
    write(snapshot,payload)
    return payload


def main():
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['train','daily']); p.add_argument('--out',type=Path,default=Path('data/ws_v3_2'))
    args=p.parse_args(); now=datetime.now(timezone.utc)
    matches,quality,fixtures,index=load_ws()
    write(args.out/'inventory.json',quality)
    write(args.out/'matches.json.gz',[asdict(m) for m in matches])
    if args.command=='train':
        artifact,report=train(matches,quality,args.out)
        rows=json.loads(gzip.decompress((args.out/'backtest_predictions_2026.json.gz').read_bytes()))['predictions']
        cats=defaultdict(list)
        for r in rows: cats[index.get(r['match_id'].split(':')[0],{}).get('category','unknown')].append(r)
        report['tournament_categories']={c:metrics(rs,[r['p_a'] for r in rs]) for c,rs in cats.items()}
        write(args.out/'backtest.json',report)
        print(json.dumps({'inventory':{k:v for k,v in quality.items() if k not in ['months','missing_indexed_elite']},'selected':artifact['selected'],'test':{n:{k:v for k,v in m.items() if k!='calibration'} for n,m in report['test_2026'].items()}}))
    else: artifact=json.loads((args.out/'model.json').read_text())
    daily(matches,quality,fixtures,artifact,args.out,now)

if __name__=='__main__': main()
