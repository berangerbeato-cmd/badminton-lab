"""Manual screenshot transcription -> independent singles models. No OCR claim."""
import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from elo_model import load_matches
from badminton_features_v3 import fit_variant, predict
from badminton_v3_2_walkforward import build_rows_raw
from badminton_v3_2_daily import snapshot_state, selected_features, FINAL_K, SELECTED_FEATURES
from elo_model import elo_p
from v32_odds_compare import match_name
from ws_data import load_ws
from ws_model import forecast, write


def utc(value):
    t=datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.tzinfo is None: raise ValueError('Timezone required')
    return t.astimezone(timezone.utc)


def resolve(name, pid, matches):
    names={p:n for m in matches for p,n in [(m.p1_id,m.p1_name),(m.p2_id,m.p2_name)]}
    if pid:
        return str(pid) if str(pid) in names and (not name or match_name(name,names[str(pid)])) else None
    found=[p for p,n in names.items() if match_name(name,n)]
    return found[0] if len(found)==1 else None


def decision(pred, quote, now):
    out=dict(pred,decision='NO BET',odds_a=None,odds_b=None,ev_a=None,ev_b=None)
    reasons=[]
    try:
        a,b=float(quote['odds_a']),float(quote['odds_b'])
        if not all(math.isfinite(x) and x>1 for x in (a,b)): raise ValueError('Invalid odds')
        observed=utc(quote['observed_at']); start=utc(quote['start_utc'])
        if not now-timedelta(hours=2)<=observed<=now<start: reasons.append('ODDS_OR_KICKOFF_STALE')
        if start>now+timedelta(days=14): reasons.append('FIXTURE_TOO_FAR')
        if not quote.get('bookmaker') or not quote.get('evidence'): reasons.append('MISSING_PRICE_EVIDENCE')
        if quote.get('start_verified') is not True or not quote.get('kickoff_source'): reasons.append('UNVERIFIED_KICKOFF')
        out.update(odds_a=a,odds_b=b,implicit_a=1/a,implicit_b=1/b,
                   market_no_margin_a=(1/a)/(1/a+1/b),observed_at=quote['observed_at'],
                   bookmaker=quote.get('bookmaker'),price_evidence=quote.get('evidence'))
        p=pred.get('p_a')
        if p is not None:
            out.update(ev_a=p*a-1,ev_b=(1-p)*b-1)
        if pred.get('status')!='MODEL_READY': reasons.append(pred.get('status','MODEL_NOT_READY'))
        if not reasons and p is not None:
            side='a' if out['ev_a']>=out['ev_b'] else 'b'
            prob=p if side=='a' else 1-p
            # Conservative independent WS gate; no changes to legacy MS policy.
            if out['ev_'+side]>=.10 and .50<=prob<=.85:
                out.update(decision='BET',selection=side)
            else: reasons.append('EV_OR_PROBABILITY_GATE')
    except (ValueError,TypeError,KeyError,OverflowError): reasons.append('INVALID_OR_MISSING_QUOTE')
    out['reasons']=reasons
    out['policy']='THEORETICAL_VALUE_MANUAL_DECISION_NO_AUTOMATIC_BET'
    return out


def analyze(quotes, now=None, ws_dir=Path('data/ws_v3_2')):
    now=now or datetime.now(timezone.utc)
    ws,_,_,_=load_ws(asof=now.date().isoformat()); ms,_=load_matches(Path('data/bwf'))
    ms=[m for m in ms if m.date<now.date().isoformat()]
    datasets={'MS':ms,'WS':ws}; model=json.loads((ws_dir/'model.json').read_text())
    ms_model=None; output=[]
    for q in quotes:
        try:
            discipline=q.get('discipline')
            if discipline in ('MD','WD','XD'):
                output.append(dict(q,decision='NO BET',status='DOUBLES_IGNORED')); continue
            candidates=[]
            for d, matches in datasets.items():
                a=resolve(q.get('player_a'),q.get('player_a_id'),matches)
                b=resolve(q.get('player_b'),q.get('player_b_id'),matches)
                if a and b and a!=b: candidates.append((d,a,b))
            if discipline:
                candidates=[c for c in candidates if c[0]==discipline]
            if len(candidates)!=1: raise ValueError('UNKNOWN_OR_AMBIGUOUS_PLAYERS')
            d,a,b=candidates[0]
            f=dict(q,discipline=d,player_a_id=a,player_b_id=b)
            start=utc(q['start_utc'])
            if start<=now or start>now+timedelta(days=14): raise ValueError('NOT_UPCOMING')
            if d=='WS': pred=forecast(ws,model,f,now)
            else:
                if ms_model is None:
                    rows=build_rows_raw(ms,FINAL_K)
                    ms_model=fit_variant([r for r in rows if 2019<=r['year']<=2025],SELECTED_FEATURES)
                    ratings,seen,hist=snapshot_state(ms,FINAL_K,now.date().isoformat())
                raw=elo_p(ratings[a],ratings[b])
                features=selected_features(hist[a],hist[b],start.date())
                age=(now.date()-datetime.fromisoformat(ms[-1].date).date()).days
                status='MODEL_READY' if age<=7 and min(seen[a],seen[b])>=10 else 'MS_QUALITY_NO_BET'
                if start.hour==start.minute==start.second==0: status='DATE_ONLY_NO_BET'
                pred=dict(f,p_a=predict(dict(raw_p_a=raw,**features),ms_model),asof_utc=now.isoformat(),
                          status=status,model_id='legacy_MS_V3.2_K64',source_age_days=age)
            output.append(decision(pred,q,now))
        except (KeyError,ValueError,TypeError) as exc:
            output.append(dict(q,decision='NO BET',status=str(exc),p_a=None))
    return output


def main():
    p=argparse.ArgumentParser(); p.add_argument('quotes',type=Path); p.add_argument('--out',type=Path,required=True)
    args=p.parse_args(); write(args.out,analyze(json.loads(args.quotes.read_text())))

if __name__=='__main__': main()
