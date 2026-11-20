"""Append-only explicit actual/simulation records; never infer that a bet was placed."""
import argparse
import json
import math
from pathlib import Path
from badminton_singles_value import utc
from ws_model import write


def validate(r):
    if r['discipline'] not in ('MS','WS') or r['kind'] not in ('actual','simulation'): raise ValueError('Invalid ledger type')
    if not r['id'] or not r['match_id']: raise ValueError('IDs required')
    if not utc(r['prediction_at'])<=utc(r['placed_at'])<utc(r['start_utc']): raise ValueError('Not prematch')
    for k in ('stake','odds','p_model'):
        if not math.isfinite(float(r[k])): raise ValueError('Non-finite value')
    if r['stake']<=0 or r['odds']<=1 or not 0<r['p_model']<1: raise ValueError('Invalid values')
    if r['result'] not in ('pending','won','lost','void'): raise ValueError('Invalid result')
    if not r.get('prediction_evidence'): raise ValueError('Original prediction evidence required')
    if r['kind']=='actual' and not r.get('bet_receipt'): raise ValueError('Actual bet evidence required')
    return dict(r,ev_at_bet=r['p_model']*r['odds']-1)


def summary(rows):
    out={}
    for kind in ('actual','simulation'):
        for d in ('MS','WS','ALL'):
            rs=[r for r in rows if r['kind']==kind and (d=='ALL' or r['discipline']==d)]
            settled=[r for r in rs if r['result']!='pending']
            stake=sum(r['stake'] for r in settled)
            returns=sum(r['stake']*r['odds'] if r['result']=='won' else r['stake'] if r['result']=='void' else 0 for r in settled)
            out[f'{kind}_{d}']=dict(bets=len(rs),won=sum(r['result']=='won' for r in rs),lost=sum(r['result']=='lost' for r in rs),
                pending=sum(r['result']=='pending' for r in rs),total_staked=sum(r['stake'] for r in rs),settled_stake=stake,
                returns=returns,net=returns-stake,roi=(returns/stake-1) if stake else None)
    return out


def main():
    p=argparse.ArgumentParser(); p.add_argument('--root',type=Path,default=Path('data/singles_ledgers')); p.add_argument('--append',type=Path); p.add_argument('--settle',type=Path)
    args=p.parse_args()
    if args.append:
        r=validate(json.loads(args.append.read_text())); path=args.root/r['discipline']/r['kind']/(r['id']+'.json')
        if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in r['id']): raise ValueError('Unsafe ID')
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x') as f: json.dump(r,f,indent=2)
    rows=[validate(json.loads(f.read_text())) for f in args.root.glob('*/*/*.json')]
    if args.settle:
        event=json.loads(args.settle.read_text())
        found=[r for r in rows if all(r[k]==event[k] for k in ('id','discipline','kind'))]
        if len(found)!=1 or found[0]['result']!='pending': raise ValueError('Unknown or already settled bet')
        if event['result'] not in ('won','lost','void') or not event.get('evidence'): raise ValueError('Settlement evidence required')
        if utc(event['settled_at']) < utc(found[0]['start_utc']): raise ValueError('Settlement before start')
        path=args.root/'settlements'/event['discipline']/event['kind']/(event['id']+'.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x') as f: json.dump(event,f,indent=2)
    for r in rows:
        path=args.root/'settlements'/r['discipline']/r['kind']/(r['id']+'.json')
        if path.exists(): r['result']=json.loads(path.read_text())['result']
    write(args.root/'summary.json',dict(scope='Only explicit imported records; legacy history not automatically imported',summary=summary(rows)))

if __name__=='__main__': main()
