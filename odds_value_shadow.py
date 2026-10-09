#!/usr/bin/env python3
"""Read-only match of observed badminton H2H quotes with BWF Elo V1 forecasts.

SHADOW RESEARCH ONLY. This does not validate an operator quote as executable,
confirm the exact BWF match start, freeze a T0, or recommend/place a bet.
Only reads immutable source observations; writes separate audit JSON/CSV.

Usage:
    python odds_value_shadow.py --self-test
    python odds_value_shadow.py --live --odds-dir data/odds_shadow --model-dir data/model

Python 3.12 standard library, no credentials or network requests.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import tempfile
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REQUIRED_SOURCE_STATUS = 'OBSERVED_SHADOW_NEEDS_MARKET_CONFIRMATION'
MODEL_STATUS = 'DATE_ONLY_START_UNVERIFIED_NO_BET'
TOLERANCE_SECONDS = 300
MAX_AGE_SECONDS = 20 * 60
# Use explicit equivalences only when tournament naming differs between
# a bookmaker's translated title and BWF's event name.
TOURNAMENT_EQUIVALENTS = {
    'open de finlande': ('arctic open',),
}


def dt(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None or d.utcoffset() is None:
        raise ValueError('Timestamp without timezone')
    return d.astimezone(timezone.utc)


def norm(text: str) -> str:
    value = unicodedata.normalize('NFKD', text)
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(ch for ch in value if not unicodedata.combining(ch)).lower()).strip()


def player_matches(abbrev: str, official: str) -> bool:
    """Unambiguous Unibet style initials.surname, e.g. ZJ.Lee -> LEE Zii Jia.

    Refuse malformed names, surnames appearing more than once, and incomplete
    initial sets rather than guessing which competitor the abbreviation means.
    """
    hit = re.fullmatch(r'([A-Za-z]{1,5})\.([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-]{1,45})', abbrev.strip())
    if not hit:
        return False
    initials, surname = hit.groups()
    words = norm(official).split()
    target = norm(surname).replace(' ', '')
    possible = [i for i, w in enumerate(words) if w == target]
    if len(possible) != 1:
        return False
    other = words[:possible[0]] + words[possible[0]+1:]
    return bool(other) and ''.join(word[0] for word in other).lower() == initials.lower()


def tournament_matches(book: str, bwf: str) -> bool:
    b, f = norm(book), norm(bwf)
    if not b or not f:
        return False
    if b == f or b in f or f in b:
        return True
    return any(alias in f for alias in TOURNAMENT_EQUIVALENTS.get(b, ()))


def parse_model(text: str) -> list[dict]:
    entries = list(csv.DictReader(io.StringIO(text)))
    needed = {'bwf_match_id','player_a_id','player_b_id','player_a','player_b',
              'calibrated_p_a','start_utc','asof_utc','status','tournament','model'}
    if not entries or not needed.issubset(entries[0]):
        raise ValueError('Elo snapshot missing required fields or rows')
    return entries


def match_quote(quote: dict, models: list[dict], observed: datetime) -> tuple[dict|None, str]:
    try:
        if quote.get('discipline') != 'MS' or quote.get('market') != 'H2H_FULL_MATCH':
            return None, 'NOT_MS_H2H'
        if quote.get('market_verification') != 'LISTING_ONLY_NEEDS_DETAIL':
            return None, 'UNEXPECTED_MARKET_STATUS'
        offered = dt(quote['listed_start_paris'])
        if not (observed < offered <= observed + timedelta(days=2)):
            return None, 'START_NOT_AFTER_OBSERVATION'
        prices = [float(quote['odds_1']), float(quote['odds_2'])]
        if any(not (1.01 <= k <= 99.0) for k in prices):
            return None, 'INVALID_ODDS'
        candidates=[]
        for m in models:
            # Date-only BWF midnight is NOT a kickoff clock; compare its UTC
            # *date* with the quoted event date, never the midnight hour.
            if m['start_utc'][:10] != offered.astimezone(ZoneInfo("Europe/Paris")).date().isoformat():
                continue
            if not tournament_matches(quote['tournament'], m['tournament']):
                continue
            direct=(player_matches(quote['player_1_display'], m['player_a']) and
                    player_matches(quote['player_2_display'], m['player_b']))
            reverse=(player_matches(quote['player_1_display'], m['player_b']) and
                     player_matches(quote['player_2_display'], m['player_a']))
            if direct or reverse:
                candidates.append((m, bool(reverse)))
        if len(candidates)!=1:
            return None, 'NO_UNIQUE_FIXTURE' if not candidates else 'AMBIGUOUS_FIXTURE'
        m,rev=candidates[0]
        asof=dt(m['asof_utc'])
        if asof >= observed:
            return None,'MODEL_CREATED_AFTER_ODDS'
        if m['status']!=MODEL_STATUS:
            return None,'UNEXPECTED_MODEL_STATUS'
        pa=float(m['calibrated_p_a'])
        if not (0 < pa < 1):
            return None,'INVALID_MODEL_PROBABILITY'
        p1=(1-pa) if rev else pa
        p2=1-p1
        side1={'bookmaker_name':quote['player_1_display'], 'bwf_name':m['player_b'] if rev else m['player_a'],
               'bwf_player_id':m['player_b_id'] if rev else m['player_a_id'],
               'decimal_odds':prices[0],'model_probability':round(p1,8),
               'model_fair_odds':round(1/p1,4),'illustrative_ev_pct':round(100*(p1*prices[0]-1),2)}
        side2={'bookmaker_name':quote['player_2_display'], 'bwf_name':m['player_a'] if rev else m['player_b'],
               'bwf_player_id':m['player_a_id'] if rev else m['player_b_id'],
               'decimal_odds':prices[1],'model_probability':round(p2,8),
               'model_fair_odds':round(1/p2,4),'illustrative_ev_pct':round(100*(p2*prices[1]-1),2)}
        return {'bwf_match_id':m['bwf_match_id'], 'tournament_bwf':m['tournament'],
                'round':m.get('round',''), 'observed_at_utc':observed.isoformat(timespec='seconds'),
                'listed_start_paris':quote['listed_start_paris'],
                'model_asof_utc':m['asof_utc'], 'model':m['model'],
                'operator':'Unibet.fr', 'market':'H2H_FULL_MATCH',
                'bookmaker_price_updated_at':None,
                'valid_pre_match_T0':False, 'decision':'NO_BET_SHADOW',
                'quality_flags':['BOOKMAKER_UPDATE_TIME_UNKNOWN','EVENT_DETAIL_NOT_VERIFIED',
                                 'BWF_KICKOFF_DATE_ONLY','MODEL_PROVISIONAL','NO_BET'],
                'side_1':side1,'side_2':side2}, 'MATCHED_SHADOW_ONLY'
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        return None, 'INVALID_SOURCE_'+type(exc).__name__.upper()


def produce(summary: dict, obs: dict, models: list[dict], *, now: datetime) -> dict:
    if summary.get('policy')!='SHADOW / NO BET / NO AUTOMATIC SHEETS T0 OR ODDS HISTORY':
        raise ValueError('Unexpected scrape policy')
    source_time=dt(summary['observed_at_utc'])
    obs_time=dt(obs['observed_at_utc'])
    if abs((obs_time-source_time).total_seconds())>TOLERANCE_SECONDS:
        raise ValueError('Summary and odds file do not belong to the same collection')
    if now < obs_time or (now-obs_time).total_seconds()>MAX_AGE_SECONDS:
        raise ValueError('Collection too old for a current-run analysis')
    result={'generated_at_utc':now.isoformat(timespec='seconds'),
            'source_observed_at_utc':obs_time.isoformat(timespec='seconds'),
            'operator':'Unibet.fr', 'policy':'NO_BET_SHADOW_ONLY_NO_SHEETS_WRITE',
            'status':'ANALYZED_SHADOW', 'matches':[], 'rejections':dict(),
            'source_quotes_count':len(obs.get('quotes',[])),
            'matched_fixtures_count':0,
            'notes':'Illustrative EV only. Source reading timestamp is NOT bookmaker quote update time. BWF start date at 00:00 is NOT a verified match start. Do not write Predictions, T0, Odds History or place wagers.'}
    if obs.get('status') != REQUIRED_SOURCE_STATUS:
        result['status']='SOURCE_NOT_READY'
        result['notes']+=' Source status: '+str(obs.get('status'))
        return result
    rejects=Counter()
    seen=set()
    for quote in obs.get('quotes',[]):
        match,why=match_quote(quote,models,obs_time)
        if not match:
            rejects[why]+=1
            continue
        if match['bwf_match_id'] in seen:
            rejects['DUPLICATE_FIXTURE']+=1
            continue
        seen.add(match['bwf_match_id'])
        result['matches'].append(match)
    result['matches'].sort(key=lambda m:m['bwf_match_id'])
    result['matched_fixtures_count']=len(result['matches'])
    result['rejections']=dict(rejects)
    return result


def collect(odds_dir: Path, model_dir: Path, now: datetime) -> dict:
    summary_file=odds_dir/'latest_summary.json'
    summary=json.loads(summary_file.read_text(encoding='utf-8'))
    source_stamp=dt(summary['observed_at_utc']).strftime('%Y-%m-%dT%H%M%SZ')
    quote_file=odds_dir/'unibet_fr'/(source_stamp+'.json')
    if not quote_file.is_file():
        raise FileNotFoundError('Missing same-run quote file: '+str(quote_file))
    obs=json.loads(quote_file.read_text(encoding='utf-8'))
    snapshot=model_dir/'snapshots'/(dt(summary['observed_at_utc']).date().isoformat()+'.csv')
    if not snapshot.is_file():
        raise FileNotFoundError('Missing immutable model snapshot: '+str(snapshot))
    models=parse_model(snapshot.read_text(encoding='utf-8'))
    report=produce(summary,obs,models,now=now)
    report['source_archive']=str(quote_file)
    report['source_sha256']=hashlib.sha256(quote_file.read_bytes()).hexdigest()
    report['model_snapshot']=str(snapshot)
    report['model_snapshot_sha256']=hashlib.sha256(snapshot.read_bytes()).hexdigest()
    archive_dir=odds_dir/'value_reports'
    archive_dir.mkdir(parents=True,exist_ok=True)
    artifact=archive_dir/(source_stamp+'.json')
    payload=json.dumps(report,indent=2,ensure_ascii=False,sort_keys=True)+'\n'
    if artifact.exists():
        # A repeated workflow run must not rewrite the previous observation.
        payload=artifact.read_text(encoding='utf-8')
        report=json.loads(payload)
        if (report.get('source_sha256') != hashlib.sha256(quote_file.read_bytes()).hexdigest() or
            report.get('model_snapshot_sha256') != hashlib.sha256(snapshot.read_bytes()).hexdigest()):
            raise FileExistsError('Archived report exists but its source bytes changed')
    else:
        artifact.write_text(payload,encoding='utf-8')
    (odds_dir/'latest_value_shadow.json').write_text(payload,encoding='utf-8')
    return report


def self_test() -> None:
    obs_time=dt('2026-10-09T08:45:22+00:00')
    model_csv='''bwf_match_id,start_utc,tournament,round,player_a_id,player_a,player_b_id,player_b,calibrated_p_a,asof_utc,status,model
5594:1552538,2026-10-09T00:00:00+00:00,CLASH OF CLANS Arctic Open 2026 powered by YONEX,QF,81561,LEE Zii Jia,72885,Christo POPOV,0.11105876,2026-10-09T05:26:01+00:00,DATE_ONLY_START_UNVERIFIED_NO_BET,bwf_elo_ms_platt_v1
5594:1552534,2026-10-09T00:00:00+00:00,CLASH OF CLANS Arctic Open 2026 powered by YONEX,QF,34810,CHOU Tien Chen,97174,Koki WATANABE,0.57103844,2026-10-09T05:26:01+00:00,DATE_ONLY_START_UNVERIFIED_NO_BET,bwf_elo_ms_platt_v1
5594:1552533,2026-10-09T00:00:00+00:00,CLASH OF CLANS Arctic Open 2026 powered by YONEX,QF,73591,HU Zhe An,91554,Anders ANTONSEN,0.32272385,2026-10-09T05:26:01+00:00,DATE_ONLY_START_UNVERIFIED_NO_BET,bwf_elo_ms_platt_v1
5594:1552530,2026-10-09T00:00:00+00:00,CLASH OF CLANS Arctic Open 2026 powered by YONEX,QF,64032,Kunlavut VITIDSARN,86672,Yushi TANAKA,0.73146539,2026-10-09T05:26:01+00:00,DATE_ONLY_START_UNVERIFIED_NO_BET,bwf_elo_ms_platt_v1
'''
    pairs=[('ZJ.Lee','C.Popov',2.95,1.25,'12:25'),('TC.Chou','K.Watanabe',1.80,1.71,'13:20'),
           ('ZA.Hu','A.Antonsen',2.65,1.31,'13:50'),('K.Vitidsarn','Y.Tanaka',1.28,2.75,'14:00')]
    summary={'observed_at_utc':'2026-10-09T08:45:21+00:00','policy':'SHADOW / NO BET / NO AUTOMATIC SHEETS T0 OR ODDS HISTORY'}
    obs={'observed_at_utc':obs_time.isoformat(),'status':REQUIRED_SOURCE_STATUS,
         'quotes':[{'discipline':'MS','market':'H2H_FULL_MATCH','market_verification':'LISTING_ONLY_NEEDS_DETAIL',
                    'tournament':'Open de Finlande','listed_start_paris':f'2026-10-09T{clock}+02:00',
                    'player_1_display':a,'player_2_display':b,'odds_1':o1,'odds_2':o2} for a,b,o1,o2,clock in pairs]}
    model=parse_model(model_csv)
    out=produce(summary,obs,model,now=obs_time+timedelta(seconds=40))
    assert out['matched_fixtures_count']==4,out
    assert out['rejections']=={},out
    matches={m['bwf_match_id']:m for m in out['matches']}
    assert matches['5594:1552538']['side_2']['illustrative_ev_pct']==11.12
    assert matches['5594:1552534']['side_1']['illustrative_ev_pct']==2.79
    assert all(m['decision']=='NO_BET_SHADOW' and not m['valid_pre_match_T0'] for m in out['matches'])
    # Names, dates, timing, ambiguous match and inaccurate source versions must fail closed.
    bad=dict(obs['quotes'][0]);bad['player_2_display']='Other.Player'
    assert match_quote(bad,model,obs_time)[0] is None
    assert match_quote(obs['quotes'][0],model+model,obs_time)[1]=='AMBIGUOUS_FIXTURE'
    expired=dict(obs['quotes'][0]);expired['listed_start_paris']='2026-10-09T08:20+02:00'
    assert match_quote(expired,model,obs_time)[1]=='START_NOT_AFTER_OBSERVATION'
    future=dict(model[0]);future['asof_utc']='2026-10-09T09:30:00+00:00'
    assert match_quote(obs['quotes'][0],[future],obs_time)[1]=='MODEL_CREATED_AFTER_ODDS'
    reverse=dict(obs['quotes'][0]);reverse['player_1_display']='C.Popov';reverse['player_2_display']='ZJ.Lee';reverse['odds_1']=1.25;reverse['odds_2']=2.95
    flipped,why=match_quote(reverse,model,obs_time)
    assert why=='MATCHED_SHADOW_ONLY' and flipped['side_1']['bwf_player_id']=='72885'
    # Complete offline run: sources and model saved in the exact repository paths,
    # JSON report written, then repeated without changing the archived result.
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        odds_dir=root/'data'/'odds_shadow'
        model_dir=root/'data'/'model'
        (odds_dir/'unibet_fr').mkdir(parents=True)
        (model_dir/'snapshots').mkdir(parents=True)
        (odds_dir/'latest_summary.json').write_text(json.dumps(summary),encoding='utf-8')
        stamp='2026-10-09T084521Z.json'
        (odds_dir/'unibet_fr'/stamp).write_text(json.dumps(obs),encoding='utf-8')
        (model_dir/'snapshots'/'2026-10-09.csv').write_text(model_csv,encoding='utf-8')
        a=collect(odds_dir,model_dir,now=obs_time+timedelta(seconds=40))
        b=collect(odds_dir,model_dir,now=obs_time+timedelta(seconds=50))
        assert a==b and a['matched_fixtures_count']==4
        assert (odds_dir/'value_reports'/stamp).is_file()
        assert (odds_dir/'latest_value_shadow.json').is_file()
        assert a['source_sha256'] and a['model_snapshot_sha256']
    print('PASS: 4 real-format MS fixtures cross-matched to Elo V1; illustrative EV and identity checks')
    print('PASS: rejects ambiguous fixtures, mismatched names, started matches, future model snapshots')
    print('PASS: immutable T0/Sheets/no bet safeguards; tests are offline samples')


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--odds-dir',type=Path,default=Path('data/odds_shadow'))
    parser.add_argument('--model-dir',type=Path,default=Path('data/model'))
    args=parser.parse_args()
    if args.self_test:
        self_test()
    elif args.live:
        report=collect(args.odds_dir,args.model_dir,now=datetime.now(timezone.utc))
        print(json.dumps({'status':report['status'],'observed':report['source_observed_at_utc'],
                          'quotes':report['source_quotes_count'],'matches':report['matched_fixtures_count'],
                          'rejections':report['rejections']},ensure_ascii=False))
        for m in report['matches']:
            s=m['side_1'];t=m['side_2']
            print(f"{s['bwf_name']} {s['decimal_odds']} EV {s['illustrative_ev_pct']:+.2f}% | "
                  f"{t['bwf_name']} {t['decimal_odds']} EV {t['illustrative_ev_pct']:+.2f}% "
                  '[SHADOW/NO BET]')
    else:
        parser.error('Choose --live or --self-test')

if __name__=='__main__':
    main()
