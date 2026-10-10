#!/usr/bin/env python3
"""Read-only NetBet.fr badminton men's singles H2H observer and Elo V1 shadow audit.

No login, protected API, captcha/geoblock bypass, wager or Sheets writes.
Only accepts explicitly paired 'Qui va gagner le match ?' public event rows.
Prices, snapshots and matches remain UNVERIFIED for betting purposes.

    python netbet_quotes_shadow.py --self-test
    python netbet_quotes_shadow.py --live
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

PARIS = ZoneInfo('Europe/Paris')
INDEX = 'https://www.netbet.fr/badminton'
ODDS_DIR = Path('data/odds_shadow/netbet_quotes')
MODEL_DIR = Path('data/model')
ODD = re.compile(r'\d{1,2}[.,]\d{2}')
MARKET = re.compile(r'Qui va gagner le match\s*\?', re.I)
DATE_HINT = re.compile(r'\b(\d{1,2})\s+(janv|févr|mars|avr|mai|juin|juil|août|sept|oct|nov|déc)\.?\b', re.I)
MONTHS = {'janv':1,'févr':2,'mars':3,'avr':4,'mai':5,'juin':6,'juil':7,'août':8,'sept':9,'oct':10,'nov':11,'déc':12}
CACHE_LIMIT_SECONDS = 120


def norm(s: str) -> str:
    x=unicodedata.normalize('NFKD', s)
    return re.sub(r'[^a-z0-9]+',' ',''.join(c for c in x if not unicodedata.combining(c)).lower()).strip()


def same_player(book: str, bwf: str) -> bool:
    b=norm(book).split()
    a=norm(bwf).split()
    return bool(b) and len(b)>=2 and Counter(b)==Counter(a)


def parse_price(s: str) -> float:
    if not ODD.fullmatch(s.strip()):
        raise ValueError('not an explicit decimal price')
    x=float(s.strip().replace(',','.'))
    if not (1.01 <= x <= 99):
        raise ValueError('out-of-range price')
    return x


def competition_matches(competition: str, bwf_tournament: str) -> bool:
    # Strict equivalence of the actual translated typo 'Artic' on NetBet to BWF 'Arctic'.
    b=norm(competition).replace('artic open', 'arctic open')
    f=norm(bwf_tournament)
    return len(b)>=9 and b in f


def listed_day(context: list[str], now_local: datetime) -> tuple[str|None, str]:
    # Read explicit displayed French dates; do not treat BWF midnight as kickoff.
    for line in reversed(context):
        m=DATE_HINT.search(line)
        if m:
            day=int(m.group(1));month=MONTHS[m.group(2).lower()]
            candidates=[]
            for y in (now_local.year-1, now_local.year, now_local.year+1):
                try:
                    d=date(y,month,day)
                except ValueError:
                    continue
                if -1 <= (d-now_local.date()).days <= 2:
                    candidates.append(d)
            if len(candidates)==1:
                return candidates[0].isoformat(), 'PUBLISHED_CALENDAR_DATE'
            return None, 'DATE_AMBIGUOUS'
    for line in reversed(context):
        m=re.fullmatch(r'LIVE\s+dans\s+(\d+)\s+min',line,re.I)
        if m:
            mins=int(m.group(1))
            if 1<=mins<=1440:
                d=(now_local+timedelta(minutes=mins)).date()
                return d.isoformat(), 'FUTURE_COUNTDOWN_DAY_INFERRED_NOT_KICKOFF'
        if re.search(r'\bLIVE\b|En direct',line,re.I):
            return None, 'LIVE_OR_UNCERTAIN_START'
    return None,'MISSING_DATE_AND_COUNTDOWN'


def parse_event_text(text: str, observed: datetime, event_url: str) -> tuple[list[dict], dict]:
    """Require both names to repeat identically around the precise H2H market label."""
    now_local=observed.astimezone(PARIS)
    lines=[re.sub(r'\s+', ' ',v).strip() for v in text.splitlines()]
    lines=[l for l in lines if l]
    audits=Counter();out=[]
    # The public competition title must be visible above the events. Do not
    # infer the tournament from its URL slug or from an unrelated fixture.
    title_candidates=[lines[i+1] for i,x in enumerate(lines[:-1])
                      if norm(x)=='pariez sur' and lines[i+1]]
    competition=title_candidates[0] if len(set(title_candidates))==1 and title_candidates else None
    if not competition:
        return [], {'markets_seen':sum(1 for x in lines if MARKET.fullmatch(x)),
                    'rejections':{'MISSING_UNIQUE_COMPETITION_TITLE':1},'eligible_listing_rows':0}
    for i, line in enumerate(lines):
        if not MARKET.fullmatch(line):
            continue
        if i<4 or i+4>=len(lines):
            audits['TRUNCATED_MARKET']+=1;continue
        a,b=lines[i-2:i]
        pa,oa,pb,ob=lines[i+1:i+5]
        if norm(a)!=norm(pa) or norm(b)!=norm(pb) or norm(a)==norm(b):
            audits['PLAYER_PAIR_MISMATCH']+=1;continue
        if '/' in a or '/' in b:
            audits['DOUBLES_REJECTED']+=1;continue
        try:
            x,y=parse_price(oa),parse_price(ob)
        except ValueError:
            audits['PRICE_NOT_CONFIRMED']+=1;continue
        context=lines[max(0,i-8):i-2]
        if not any(norm(v)==norm(competition) for v in context):
            audits['COMPETITION_NOT_PROVEN']+=1;continue
        day,day_status=listed_day(context,now_local)
        if not day:
            audits[day_status]+=1;continue
        # Price is public listing observation, not an actual bookmaker quote update timestamp.
        out.append({'player_1_display':a,'player_2_display':b,
                    'odds_1':x,'odds_2':y,'market':'H2H_FULL_MATCH',
                    'discipline':'MS','competition_display':competition,
                    'listed_day_paris':day,'day_basis':day_status,
                    'event_page_url':event_url,
                    'verification':'LISTING_NEEDS_DETAIL_AND_PREMATCH_CONFIRMATION'})
    keys=set();unique=[]
    for q in out:
        key=(norm(q['player_1_display']),norm(q['player_2_display']),q['listed_day_paris'])
        if key in keys:
            audits['DUPLICATE_MARKET']+=1
            continue
        keys.add(key);unique.append(q)
    return unique,{'markets_seen':sum(1 for x in lines if MARKET.fullmatch(x)),
                   'rejections':dict(audits),'eligible_listing_rows':len(unique)}


def dt(s: str) -> datetime:
    t=datetime.fromisoformat(s.replace('Z','+00:00'))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('timezone required')
    return t.astimezone(timezone.utc)


def match_elo(quotes: list[dict], model_csv: str, observed: datetime) -> tuple[list[dict],dict]:
    rows=list(csv.DictReader(io.StringIO(model_csv)))
    reasons=Counter();matched=[];ids=set()
    for q in quotes:
        matches=[]
        for m in rows:
            if m.get('start_utc','')[:10]!=q['listed_day_paris']:
                continue
            if m.get('status')!='DATE_ONLY_START_UNVERIFIED_NO_BET':
                continue
            if not competition_matches(q['competition_display'],m.get('tournament','')):
                continue
            direct=same_player(q['player_1_display'],m['player_a']) and same_player(q['player_2_display'],m['player_b'])
            reverse=same_player(q['player_1_display'],m['player_b']) and same_player(q['player_2_display'],m['player_a'])
            if direct or reverse:
                matches.append((m,reverse))
        if len(matches)!=1:
            reasons['NO_UNIQUE_BWF_FIXTURE' if not matches else 'AMBIGUOUS_FIXTURE']+=1;continue
        m,rev=matches[0]
        if m['bwf_match_id'] in ids:
            reasons['DUPLICATE_BWF_FIXTURE']+=1;continue
        if dt(m['asof_utc'])>=observed:
            reasons['MODEL_NOT_PRIOR_TO_QUOTES']+=1;continue
        try:
            pa=float(m['calibrated_p_a'])
            if not 0<pa<1:raise ValueError('bad probability')
        except (KeyError,TypeError,ValueError):
            reasons['INVALID_MODEL_PROBABILITY']+=1;continue
        ids.add(m['bwf_match_id'])
        p1=1-pa if rev else pa
        sides=[]
        for k,p in ((1,p1),(2,1-p1)):
            # k=1 direct -> A; k=1 reverse -> B; k=2 direct -> B; k=2 reverse -> A
            if k==1:
                name=m['player_b'] if rev else m['player_a']
            else:
                name=m['player_a'] if rev else m['player_b']
            odds=q[f'odds_{k}']
            sides.append({'bookmaker_display':q[f'player_{k}_display'],
                          'bwf_name':name,'decimal_odds':odds,
                          'model_probability':round(p,8),
                          'model_fair_odds':round(1/p,4),
                          'illustrative_ev_pct':round((p*odds-1)*100,2)})
        matched.append({'bwf_match_id':m['bwf_match_id'],
                        'listed_day_paris':q['listed_day_paris'],
                        'date_basis':q['day_basis'],
                        'model_asof_utc':m['asof_utc'],
                        'market':'H2H_FULL_MATCH',
                        'operator':'NetBet.fr','event_url':q['event_page_url'],
                        'bookmaker_price_updated_at':None,
                        'valid_pre_match_T0':False,'decision':'NO_BET_SHADOW',
                        'quality_flags':['BOOKMAKER_UPDATE_TIME_UNKNOWN','EVENT_KICKOFF_UNVERIFIED',
                                         'BOOKMAKER_MARKET_NOT_CONFIRMED_AT_BET','MODEL_PROVISIONAL','NO_BET'],
                        'side_1':sides[0],'side_2':sides[1]})
    return matched,dict(reasons)


def archived_result(out: Path, result: dict):
    out.mkdir(parents=True,exist_ok=True)
    timecode=dt(result['observed_at_utc']).strftime('%Y-%m-%dT%H%M%SZ')
    named=out / f'{timecode}.json'
    payload=json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)+'\n'
    if named.exists() and named.read_text(encoding='utf-8')!=payload:
        raise FileExistsError('Refusing to replace immutable source audit')
    named.write_text(payload,encoding='utf-8')
    (out/'latest.json').write_text(payload,encoding='utf-8')
    print(json.dumps({'status':result['status'], 'quotes':len(result['quotes']),
                      'matched_elo_fixtures':len(result['elo_comparisons']),
                      'archive':str(named)},ensure_ascii=False))


def live(out: Path, model_dir: Path):
    from playwright.sync_api import sync_playwright
    from multi_book_odds_scraper import robots_check
    now=datetime.now(timezone.utc)
    result={'observed_at_utc':now.isoformat(timespec='seconds'),
            'operator':'NetBet.fr','index_url':INDEX,
            'policy':'SHADOW_ONLY_NO_BET_NO_SHEETS_NO_T0',
            'status':'NOT_RUN','quotes':[],'elo_comparisons':[],
            'bookmaker_price_updated_at':None,'page_audits':[],
            'reason_counts':{},'model_snapshot':None}
    allowed,reason=robots_check(INDEX)
    result['robots_status']=reason
    if not allowed:
        result['status']='SKIPPED_ROBOTS_DENIED_OR_UNAVAILABLE'
        archived_result(out,result);return
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                index=browser.new_page(locale='fr-FR',timezone_id='Europe/Paris')
                resp=index.goto(INDEX,wait_until='domcontentloaded',timeout=24000)
                index.wait_for_timeout(1800)
                result['index_http_status']=resp.status if resp else None
                result['index_resolved_url']=index.url
                result['index_body_sha256']=hashlib.sha256(index.locator('body').inner_text(timeout=12000).encode()).hexdigest()
                try:
                    shot=out.parent/'visual_evidence'/'netbet_index.png'
                    shot.parent.mkdir(parents=True,exist_ok=True)
                    index.screenshot(path=str(shot),full_page=True,timeout=15000)
                    result['index_screenshot']=str(shot)
                except Exception as exc:
                    result['index_screenshot_error']=type(exc).__name__+': '+str(exc)[:150]
                if not resp or resp.status!=200 or urlsplit(index.url).netloc not in ('www.netbet.fr','netbet.fr'):
                    result['status']='INDEX_HTTP_NOT_OK'
                else:
                    links=index.locator('a[href]').evaluate_all("els => els.map(x=>x.href).filter(Boolean)")
                    chosen=[]
                    for u in dict.fromkeys(links):
                        parts=urlsplit(u)
                        if (parts.scheme=='https' and parts.netloc in ('www.netbet.fr','netbet.fr')
                            and re.fullmatch(r'/badminton/international/[a-z0-9-]+',parts.path)
                            and not re.search(r'-(?:doubles?(?:-mixtes|-f)?|f)$',parts.path,re.I)):
                            chosen.append(u)
                    result['index_link_count']=len(links)
                    badminton_links=[u for u in dict.fromkeys(links) if 'badminton' in urlsplit(u).path.casefold()]
                    result['index_badminton_link_count']=len(badminton_links)
                    result['index_badminton_link_examples']=badminton_links[:25]
                    result['index_link_path_counts']=dict(Counter(urlsplit(u).path.split('/')[1] if len(urlsplit(u).path.split('/'))>1 else '' for u in links).most_common(12))
                    result['candidate_urls']=chosen[:8]
                    result['candidate_count']=len(chosen)
                    result['page_audit_limit']=8
                    for url in chosen[:8]:
                        permitted,rsn=robots_check(url)
                        audit={'url':url,'robots_status':rsn}
                        if not permitted:
                            audit['status']='ROBOTS_DENIED';result['page_audits'].append(audit);continue
                        pg=browser.new_page(locale='fr-FR',timezone_id='Europe/Paris')
                        try:
                            response=pg.goto(url,wait_until='domcontentloaded',timeout=24000)
                            pg.wait_for_timeout(2300)
                            body=pg.locator('body').inner_text(timeout=12000)
                            try:
                                shot=out.parent/'visual_evidence'/('netbet_event_'+str(len(result['page_audits']))+'.png')
                                pg.screenshot(path=str(shot),full_page=True,timeout=15000)
                                audit['screenshot']=str(shot)
                            except Exception as exc:
                                audit['screenshot_error']=type(exc).__name__+': '+str(exc)[:150]
                            audit['http_status']=response.status if response else None
                            audit['resolved_url']=pg.url
                            audit['body_sha256']=hashlib.sha256(body.encode()).hexdigest()
                            raw_age=response.headers.get('age') if response else None
                            audit['cache_age_seconds']=raw_age
                            if (not response or response.status!=200 or
                                urlsplit(pg.url).netloc not in ('www.netbet.fr','netbet.fr')):
                                audit['status']='REJECT_HTTP_OR_REDIRECT'
                            elif raw_age is not None and (not raw_age.isdigit() or int(raw_age)>CACHE_LIMIT_SECONDS):
                                audit['status']='REJECT_CACHE_AGE'
                            else:
                                rows,diag=parse_event_text(body,now,url)
                                audit.update(diag)
                                audit['status']='PUBLIC_LISTINGS_OBSERVED_NEEDS_CONFIRMATION' if rows else 'NO_CONFIDENT_MS_H2H'
                                result['quotes'].extend(rows)
                        except Exception as exc:
                            audit['status']='PAGE_FAILED'
                            audit['error']=(type(exc).__name__+': '+str(exc))[:220]
                        finally:
                            pg.close()
                        result['page_audits'].append(audit)
                    result['status']='OBSERVED_SHADOW_NEEDS_CONFIRMATION' if result['quotes'] else 'NO_CONFIDENT_QUOTES'
            finally:
                browser.close()
        if result['quotes']:
            day=now.astimezone(PARIS).date().isoformat()
            snapshot=model_dir/'snapshots'/(day+'.csv')
            if snapshot.is_file():
                source=snapshot.read_bytes()
                result['model_snapshot']=str(snapshot)
                result['model_snapshot_sha256']=hashlib.sha256(source).hexdigest()
                comp,reject=match_elo(result['quotes'],source.decode('utf-8-sig'),now)
                result['elo_comparisons']=comp
                result['reason_counts']=reject
            else:
                result['reason_counts']={'MISSING_SAME_DAY_ELO_SNAPSHOT':len(result['quotes'])}
    except Exception as exc:
        result['status']='LIVE_AUDIT_FAILED'
        result['error']=(type(exc).__name__+': '+str(exc))[:240]
    archived_result(out,result)


def self_test():
    now=datetime(2026,10,9,9,29,36,tzinfo=timezone.utc)
    sample='''Accueil\nBadminton\nInternational - Artic Open\nPariez sur\nArtic Open\nÀ Venir\nArtic Open\nLIVE dans 55 min\nLee Zii Jia\nChristo Popov\nQui va gagner le match ?\nLee Zii Jia\n2.62\nChristo Popov\n1.28\n+ 24\nArtic Open\nven. 9 oct.\n13:20\nTien Chen Chou\nKoki Watanabe\nQui va gagner le match ?\nTien Chen Chou\n1.83\nKoki Watanabe\n1.62\n+ 23\nArtic Open\nven. 9 oct.\n13:50\nZhe An Hu\nAnders Antonsen\nQui va gagner le match ?\nZhe An Hu\n2.95\nAnders Antonsen\n1.21\n+ 24\nArtic Open\nven. 9 oct.\n14:00\nVitidsarn Kunlavut\nYushi Tanaka\nQui va gagner le match ?\nVitidsarn Kunlavut\n1.22\nYushi Tanaka\n2.92\n+ 24\n'''
    url='https://www.netbet.fr/badminton/international/artic-open'
    rows,info=parse_event_text(sample,now,url)
    assert len(rows)==4,(rows,info)
    assert all(x['discipline']=='MS' for x in rows)
    assert rows[0]['odds_2']==1.28 and rows[0]['competition_display']=='Artic Open'
    assert rows[1]['player_1_display']=='Tien Chen Chou'
    assert rows[0]['day_basis']=='FUTURE_COUNTDOWN_DAY_INFERRED_NOT_KICKOFF'
    assert rows[1]['day_basis']=='PUBLISHED_CALENDAR_DATE'
    corrupt=sample.replace('Christo Popov\n1.28','Somebody Else\n1.28')
    bad,di=parse_event_text(corrupt,now,url)
    assert len(bad)==3 and di['rejections']['PLAYER_PAIR_MISMATCH']==1,(bad,di)
    doubles=sample.replace('Lee Zii Jia\nChristo Popov','Lee/Double\nChristo Popov')
    bad,_=parse_event_text(doubles,now,url)
    assert len(bad)==3
    missing_time=sample.replace('ven. 9 oct.\n13:20','unknown\nunknown')
    bad,_=parse_event_text(missing_time,now,url)
    assert len(bad)==3
    model='''bwf_match_id,start_utc,tournament,round,player_a,player_b,calibrated_p_a,asof_utc,status
5594:1552530,2026-10-09T00:00:00+00:00,CLASH OF CLANS Arctic Open 2026 powered by YONEX,QF,Kunlavut VITIDSARN,Yushi TANAKA,0.73146539,2026-10-09T05:26:01+00:00,DATE_ONLY_START_UNVERIFIED_NO_BET
5594:1552533,2026-10-09T00:00:00+00:00,CLASH OF CLANS Arctic Open 2026 powered by YONEX,QF,HU Zhe An,Anders ANTONSEN,0.32272385,2026-10-09T05:26:01+00:00,DATE_ONLY_START_UNVERIFIED_NO_BET
5594:1552534,2026-10-09T00:00:00+00:00,CLASH OF CLANS Arctic Open 2026 powered by YONEX,QF,CHOU Tien Chen,Koki WATANABE,0.57103844,2026-10-09T05:26:01+00:00,DATE_ONLY_START_UNVERIFIED_NO_BET
5594:1552538,2026-10-09T00:00:00+00:00,CLASH OF CLANS Arctic Open 2026 powered by YONEX,QF,LEE Zii Jia,Christo POPOV,0.11105876,2026-10-09T05:26:01+00:00,DATE_ONLY_START_UNVERIFIED_NO_BET
'''
    matches,reasons=match_elo(rows,model,now)
    assert len(matches)==4 and reasons=={},(matches,reasons)
    by_id={x['bwf_match_id']:x for x in matches}
    assert by_id['5594:1552538']['side_2']['illustrative_ev_pct']==13.78
    assert by_id['5594:1552534']['side_1']['illustrative_ev_pct']==4.50
    assert by_id['5594:1552533']['side_1']['illustrative_ev_pct']==-4.80
    assert by_id['5594:1552530']['side_2']['illustrative_ev_pct']==-21.59
    assert all(not x['valid_pre_match_T0'] and x['decision']=='NO_BET_SHADOW' for x in matches)
    future=sample.replace('Artic Open','Swiss Open')
    future_rows,_=parse_event_text(future,now,url)
    assert len(future_rows)==4 and future_rows[0]['competition_display']=='Swiss Open'
    future_model=model.replace('CLASH OF CLANS Arctic Open 2026 powered by YONEX','YONEX Swiss Open 2026')
    assert len(match_elo(future_rows,future_model,now)[0])==4
    different=rows.copy(); different[0]=dict(rows[0],player_2_display='Unknown Stranger')
    o,r=match_elo(different,model,now)
    assert len(o)==3 and r['NO_UNIQUE_BWF_FIXTURE']==1
    print('PASS NetBet: four observed real-format MS H2H pairs, no market-label confusion')
    print('PASS NetBet: four uniquely matched BWF snapshots, indicative EV calculations')
    print('PASS NetBet: rejected corrupt pairs, doubles and missing time; NO BET unchanged')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--out',type=Path,default=ODDS_DIR)
    parser.add_argument('--model-dir',type=Path,default=MODEL_DIR)
    args=parser.parse_args()
    if args.self_test:self_test()
    elif args.live:live(args.out,args.model_dir)
    else:parser.error('Choose --self-test or --live')


if __name__=='__main__':
    main()
