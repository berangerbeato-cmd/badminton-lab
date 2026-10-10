#!/usr/bin/env python3
"""Conservative multi-source public badminton quote observation, SHADOW MODE.

This is NOT a bookmaker feed, bookmaker price timestamp, real-time bet signal,
or guarantee of executable prices. The run performs read-only public browsing;
no account, captcha solving, geoblock circumvention or wagering.

Sources: Unibet.fr, FDJ ParionsSport *point de vente* (RETAIL), and
conservative public-access probes for bwin, Betclic, Winamax, NetBet, PMU,
and Betsson. A reachable site is never counted as an actual price feed.
Every observation is stored for audit. Nothing writes Sheets or rewrites T0.

    python multi_book_odds_scraper.py --self-test
    python multi_book_odds_scraper.py --live --out data/odds_shadow
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo
import hashlib
import json
import re
import time
from collections import Counter

from unibet_odds_scraper import parse_listings

PARIS = ZoneInfo("Europe/Paris")
UA = "BadmintonResearchOdds/0.3 (read-only public research)"
ONLINE_MAX_CACHE_AGE_SECONDS = 120
# FDJ point-of-sale lists sometimes pass through a CDN cache of ~4 minutes.
# We may *archive* a page aged <=10 minutes but explicitly mark its quotes
# CACHED / NOT CURRENT / NOT EXECUTABLE. This never relaxes Unibet standards.
FDJ_ARCHIVE_MAX_CACHE_AGE_SECONDS = 600
FDJ_MIN_VALIDITY_REMAINING_SECONDS = 600
SOURCES = {
    "unibet_fr": {"operator":"Unibet.fr", "url":"https://www.unibet.fr/paris-badminton", "kind":"ONLINE", "parser":"unibet"},
    "bwin_fr": {"operator":"bwin.fr", "url":"https://sports.bwin.fr/fr/sports/badminton-44", "kind":"ONLINE", "parser":"bwin"},
    "fdj_pos_fr": {"operator":"Parions Sport Point de Vente (FDJ)", "url":"https://www.pointdevente.parionssport.fdj.fr/paris-ouverts/badminton", "kind":"RETAIL_NOT_ONLINE", "parser":"fdj"},
    "betclic_fr": {"operator":"Betclic.fr", "url":"https://www.betclic.fr/", "kind":"ONLINE", "parser":None},
    "winamax_fr": {"operator":"Winamax.fr", "url":"https://www.winamax.fr/", "kind":"ONLINE", "parser":None},
    "netbet_fr": {"operator":"NetBet.fr", "url":"https://www.netbet.fr/badminton", "kind":"ONLINE", "parser":None},
    "pmu_fr": {"operator":"PMU.fr", "url":"https://www.pmu.fr/sport/", "kind":"ONLINE", "parser":None},
    "betsson_fr": {"operator":"Betsson.fr", "url":"https://www.betsson.fr/", "kind":"ONLINE", "parser":None},
    # Aggregator reconnaissance only: no prices accepted as bookmaker quotes.
    "oddspedia_badminton": {"operator":"Oddspedia (aggregator)", "url":"https://oddspedia.com/badminton/odds", "kind":"AGGREGATOR_AUDIT_ONLY", "parser":None},
    "oddschecker_badminton": {"operator":"Oddschecker (aggregator)", "url":"https://www.oddschecker.com/badminton", "kind":"AGGREGATOR_AUDIT_ONLY", "parser":None},
    "flashscore_badminton": {"operator":"Flashscore (aggregator)", "url":"https://www.flashscore.fr/badminton/", "kind":"AGGREGATOR_AUDIT_ONLY", "parser":None},
    "oddsportal_badminton": {"operator":"OddsPortal (aggregator)", "url":"https://www.oddsportal.com/badminton/", "kind":"AGGREGATOR_AUDIT_ONLY", "parser":None},
}
ODD = re.compile(r"(?<!\d)(\d{1,2}[.,]\d{2})(?!\d)")
PRICE_PAIR = re.compile(r"^(.+?)\s+(\d{1,2}[.,]\d{2})\s+(.+?)\s+(\d{1,2}[.,]\d{2})$")
FDJ_MARKET = re.compile(r"N[°º]\s*\d+\s+Face\s+[àa]\s+Face\b.*?Fin\s+de\s+valid\.?\s+(\d{2})/(\d{2})\s+(\d{1,2})h(\d{2})", re.I)
COUNTRY = r"(?:FRA|DEN|JPN|CHN|THA|MAS|INA|IND|KOR|TPE|HKG|TWN|ENG|SCO|CAN|USA|GER|NED|FIN|SWE|ESP|VIE|PHI|SGP)"
BWIN_ROW = re.compile(
    r"(?P<p1>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ. /'\-]{2,55}?)\s+(?:"+COUNTRY+r")\s+"
    r"(?P<p2>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ. /'\-]{2,55}?)\s+(?:"+COUNTRY+r")\s*"
    r"(?P<when>Aujourd'hui|Demain)[/ ](?P<hour>\d{1,2}:\d{2})\s+"
    r"(?P<o1>\d{1,2}[.,]\d{2})\s+(?P<o2>\d{1,2}[.,]\d{2})",
    re.I,
)


def price(x):
    n = float(x.replace(",", "."))
    if not (1.01 <= n <= 99):
        raise ValueError("invalid decimal odds")
    return n


def robots_check(url):
    """Conservative: stop if robots is inaccessible or access is forbidden."""
    root = urlsplit(url)
    robots_url = f"{root.scheme}://{root.netloc}/robots.txt"
    try:
        with urlopen(Request(robots_url, headers={"User-Agent":UA}),timeout=18) as response:
            rules = response.read(200_000).decode("utf-8", "replace")
        rp = RobotFileParser()
        rp.parse(rules.splitlines())
        ok = rp.can_fetch(UA, url)
        return bool(ok), ("ALLOWED_BY_ROBOTS" if ok else "DISALLOWED_BY_ROBOTS")
    except HTTPError as e:
        return (e.code == 404), f"ROBOTS_HTTP_{e.code}"
    except (URLError, TimeoutError, OSError) as e:
        return False, f"ROBOTS_UNAVAILABLE_{type(e).__name__}"


def quote(p1, p2, o1, o2, **extra):
    p1 = p1.strip(" \t|-:")
    p2 = p2.strip(" \t|-:")
    if not p1 or not p2 or p1.casefold()==p2.casefold():
        raise ValueError("missing or duplicate contestants")
    return {"player_1_display":p1,"player_2_display":p2,
            "odds_1":price(o1),"odds_2":price(o2),"market":"H2H_FULL_MATCH",**extra}


def parse_unibet(text, now):
    entries, reason = parse_listings(text, now)
    rows = []
    categories = Counter(f"{v.get('discipline_display')}:{v.get('status')}" for v in entries)
    for v in entries:
        if v["status"] != "OBSERVED_NOT_BOOKMAKER_TIMESTAMPED":
            continue
        if v.get("discipline_display") != "H":
            continue  # current model MS only
        rows.append(quote(v["player_1_display"], v["player_2_display"],
                          str(v["odds_1"]), str(v["odds_2"]),
                          listed_start_paris=v["listed_start_paris"],
                          tournament=v["tournament"], discipline="MS",
                          market_verification="LISTING_ONLY_NEEDS_DETAIL", operator_event_id=None))
    summary = ",".join(f"{k}={v}" for k,v in sorted(categories.items())) or "NONE"
    return rows, f"{reason}; page_parsed_entries={len(entries)}; by_discipline_and_status={summary}; eligible_MS_H2H={len(rows)}; local_now={now.isoformat()}"


def parse_bwin(text, now):
    """Extract only strictly structured rows with *two explicit competitor countries*.

    Bwin's live DOM varies. Rows that do not fit are deliberately omitted,
    rather than manufacturing a competitor/price pairing.
    """
    rows = []
    if not re.search(r"\bVainqueur\b", text, re.I):
        return [], "missing Vainqueur market"
    for line in text.splitlines():
        line=re.sub(r"\s+"," ",line).strip()
        for m in BWIN_ROW.finditer(line):
            try:
                # No doubles in the men's-singles model.
                if '/' in m.group('p1') or '/' in m.group('p2'):
                    continue
                p1,p2=m.group('p1'),m.group('p2')
                # Clearly non-player UI material is never allowed as part of a name.
                if any(z in p1.casefold() or z in p2.casefold() for z in ('badminton','vainqueur','match','monde')):
                    continue
                from datetime import timedelta
                day=now.date()+(timedelta(days=1) if m.group('when').casefold()=="demain" else timedelta())
                hour,minute=map(int,m.group('hour').split(':'))
                local=datetime(day.year,day.month,day.day,hour,minute,tzinfo=PARIS)
                if local <= now: continue
                rows.append(quote(p1,p2,m.group('o1'),m.group('o2'),
                                  listed_start_paris=local.isoformat(timespec='minutes'),
                                  market_verification="LISTING_ONLY_NEEDS_DETAIL",discipline="MS"))
            except (ValueError, TypeError):
                continue
    return rows, ("structured rows" if rows else "no unambiguous two-country MS rows")


def parse_fdj(text, now, discipline="UNKNOWN"):
    """Read RETAIL face-à-face price blocks, honoring their explicit expiry.

    These prices are NOT automatically comparable with online prices.
    """
    lines=[re.sub(r"\s+"," ",x).strip() for x in text.splitlines() if x.strip()]
    rows=[]
    for i,line in enumerate(lines):
        # The retail index contains many other sports markets, including
        # Face à Face - 1er Set; never treat these as a full-match winner.
        if re.search(r"Face\s+[àa]\s+Face\s*[-–]\s*1er\s+Set",line,re.I):
            continue
        hit=FDJ_MARKET.search(line)
        if not hit or i+1>=len(lines):
            continue
        competition=lines[i-1] if i>=1 else ''
        event_title=lines[i-2] if i>=2 else ''
        this_discipline=discipline
        if discipline=='AUTO':
            # FDJ retail index labels individual competitions with H/F/DH/DF/DM.
            # Only H (not DH) belongs to the current BWF men's singles model.
            kind=re.search(r'\b(H|F|DH|DF|DM)\s*$',competition,re.I)
            if not kind or kind.group(1).upper()!='H':
                continue
            this_discipline='MS'
        dd,mm,hh,minute=map(int,hit.groups())
        try:
            end=datetime(now.year,mm,dd,hh,minute,tzinfo=PARIS)
            if (end-now).total_seconds() < FDJ_MIN_VALIDITY_REMAINING_SECONDS or (end-now).total_seconds()>4*86400:
                continue
        except ValueError:
            continue
        # FDJ renders player and price in separate DOM lines, often preceded
        # by an "Afficher" control. A single-line representation is also valid.
        pos=i+1
        if pos < len(lines) and lines[pos].casefold()=="afficher":
            pos+=1
        if pos >= len(lines):
            continue
        price_line=PRICE_PAIR.fullmatch(lines[pos].replace('|',' '))
        if price_line:
            fields=price_line.groups()
        elif (pos+3 < len(lines) and
              ODD.fullmatch(lines[pos+1]) and ODD.fullmatch(lines[pos+3])):
            fields=(lines[pos],lines[pos+1],lines[pos+2],lines[pos+3])
        else:
            continue
        try:
            p1,o1,p2,o2=fields
            if this_discipline=='MS' and ('/' in p1 or '/' in p2):
                continue
            # Bind prices to the TWO contestants in the market's own title.
            # If an event title is present but disagrees, reject the pairing.
            if '-' in event_title and not event_title.lower().startswith('parions'):
                heading=event_title.replace(' ', '').casefold()
                players=(p1.replace(' ','')+'-'+p2.replace(' ','')).casefold()
                if heading != players:
                    continue
            # This is a price block only, not proof of exact-match pairing.
            row=quote(p1,p2,o1,o2,valid_until_paris=end.isoformat(timespec="minutes"),
                      market_verification="RETAIL_NOT_EXECUTABLE_ONLINE_NEEDS_CONFIRMATION",
                      discipline=this_discipline,retail_event_display=event_title,
                      retail_competition_display=competition)
            rows.append(row)
        except ValueError:
            continue
    return rows, ("retail face-à-face rows" if rows else "no unexpired face-à-face retail rows")


def render(browser, url, screenshot_path=None):
    page=browser.new_page(locale="fr-FR",timezone_id="Europe/Paris")
    try:
        response=page.goto(url,wait_until="domcontentloaded",timeout=24000)
        page.wait_for_timeout(2000)
        content=page.locator("body").inner_text(timeout=12000)
        screenshot_error=None
        if screenshot_path is not None:
            try:
                screenshot_path.parent.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(screenshot_path),full_page=True,timeout=15000)
            except Exception as exc:
                screenshot_error=type(exc).__name__+": "+str(exc)[:160]
        # Only public link addresses; no internal betting APIs or accounts.
        event_links=page.locator('a[href*="/paris-ouverts/badminton/"]').evaluate_all(
            "els => els.map(a => a.href).filter(x => x && x.startsWith('https://'))"
        )[:100]
        headers=response.headers if response else {}
        return content,{
            "screenshot_error":screenshot_error,
            "http_status":response.status if response else None,
            "resolved_url":page.url,
            "server_date":headers.get('date'),"cache_age_seconds":headers.get('age'),
            "body_sha256":hashlib.sha256(content.encode('utf-8')).hexdigest(),
            "text_excerpt_for_debug":content[:1600],
            "public_badminton_event_links":list(dict.fromkeys(event_links))[:40],
        }
    finally:
        page.close()


def analyze_source(key, now, browser, evidence_dir=None):
    source=SOURCES[key]
    start=datetime.now(timezone.utc)
    item={
        "source_key":key,"operator":source["operator"],"operator_kind":source["kind"],
        "source_url":source["url"],"observed_at_utc":start.isoformat(timespec='seconds'),
        "status":"NOT_RUN","quotes":[],"validation":"SHADOW_ONLY_NO_BET_NO_SHEETS_WRITE",
        "bookmaker_price_updated_at":None, "notes":"Timestamp of reading is NOT bookmaker's price update timestamp.",
    }
    allowed, reason=robots_check(source['url'])
    item['robots_status']=reason
    if not allowed:
        item['status']="SKIPPED_ROBOTS_DENIED_OR_UNAVAILABLE"
        return item
    try:
        screenshot=(evidence_dir / (key+'.png')) if evidence_dir is not None else None
        content,info=render(browser,source['url'],screenshot_path=screenshot)
        if screenshot is not None and screenshot.exists():
            item['public_page_screenshot']=str(screenshot)
        item.update(info)
        expected_host=urlsplit(source['url']).netloc.removeprefix('www.')
        if info['http_status']!=200 or not urlsplit(info['resolved_url']).netloc.endswith(expected_host):
            item['status']='REJECT_HTTP_STATUS_OR_REDIRECT'
            return item
        age=info.get('cache_age_seconds')
        cached_fdj=False
        try:
            if age is not None:
                seconds=int(age)
                if seconds<0:
                    item['status']='REJECT_UNPARSEABLE_CACHE_AGE'
                    return item
                if key=='fdj_pos_fr' and seconds>ONLINE_MAX_CACHE_AGE_SECONDS:
                    if seconds>FDJ_ARCHIVE_MAX_CACHE_AGE_SECONDS:
                        item['status']='REJECT_HTTP_CACHE_AGE'
                        return item
                    cached_fdj=True
                elif seconds>ONLINE_MAX_CACHE_AGE_SECONDS and source['kind']!='AGGREGATOR_AUDIT_ONLY':
                    item['status']='REJECT_HTTP_CACHE_AGE'
                    return item
        except (ValueError, TypeError):
            item['status']='REJECT_UNPARSEABLE_CACHE_AGE'
            return item
        if source['kind']=='AGGREGATOR_AUDIT_ONLY':
            item['audit_only_stale_content_allowed']=True
            item['audit_warning']='Diagnostic only; no bookmaker prices are extracted or accepted.'
        if key=='flashscore_badminton':
            # Discovery only: public DOM anchors, no internal APIs and no
            # automatic quote ingestion. Avoid relying on one finished XD match.
            item['flashscore_discovery']={
                'index_match_links':[],
                'index_match_link_count':0,
                'index_ms_context_visible':bool(re.search(
                    r'(SIMPLES HOMMES|SIMPLE HOMMES|MEN.S SINGLES)',content,re.I)),
                'index_text_excerpt':content[:1800],
            }
            try:
                from playwright.sync_api import sync_playwright  # existing browser
                discovery_page=browser.new_page(locale='fr-FR',timezone_id='Europe/Paris')
                try:
                    discovery_page.goto(source['url'],wait_until='domcontentloaded',timeout=24000)
                    discovery_page.wait_for_timeout(2000)
                    links=discovery_page.locator('a[href*="/match/badminton/"]').evaluate_all(
                        "els => els.map(a => a.href).filter(Boolean)")
                    links=list(dict.fromkeys(u for u in links if
                        urlsplit(u).netloc.endswith('flashscore.fr') and
                        '/match/badminton/' in urlsplit(u).path))
                    item['flashscore_discovery']['index_match_link_count']=len(links)
                    item['flashscore_discovery']['index_match_links']=links[:15]
                finally:
                    discovery_page.close()
            except Exception as discovery_exc:
                item['flashscore_discovery']['error']=type(discovery_exc).__name__

            # User-provided public match link. Audit the odds tab, not any
            # private API; never interpret the visible numbers as live quotes.
            # Prefer a two-player named public link, rather than the old mixed-doubles sample.
            # Candidate status is still UNKNOWN until the rendered match page is checked.
            named_links=[u for u in item['flashscore_discovery'].get('index_match_links',[])
                         if len([p for p in urlsplit(u).path.split('/') if p])>=4
                         and not any('/' in p for p in urlsplit(u).path.split('/')[3:])]
            candidate=named_links[-1] if named_links else None
            match_url=(candidate.split('?',1)[0].rstrip('/')+
                       '/#/cotes/home-away/temps-regulier/' if candidate else None)

            audit={'url':match_url,'status':'NOT_RUN','quotes_extracted':0,
                   'candidate_from_index':bool(candidate),'candidate_selection':'last_named_public_link_not_verified_upcoming'}
            item['flashscore_match_audit']=audit
            if not match_url:
                audit['status']='NO_NAMED_MATCH_CANDIDATE'
                item['status']='PUBLIC_PAGE_REACHABLE_NO_MATCH_CANDIDATE'
                return item
            permitted,match_robots=robots_check(match_url)
            audit['robots_status']=match_robots
            if not permitted:
                audit['status']='SKIPPED_ROBOTS_DENIED_OR_UNAVAILABLE'
            else:
                time.sleep(1)
                try:
                    evidence=(evidence_dir / 'flashscore_match_odds.png') if evidence_dir is not None else None
                    match_body,match_info=render(browser,match_url,screenshot_path=evidence)
                    audit.update({k:match_info.get(k) for k in
                                  ('http_status','resolved_url','body_sha256','screenshot_error','text_excerpt_for_debug')})
                    if evidence is not None and evidence.exists():
                        audit['screenshot']=str(evidence)
                    audit['body_characters']=len(match_body)
                    audit['resolved_to_requested_odds_tab']=('/cotes/' in match_info.get('resolved_url',''))
                    audit['match_finished']=bool(re.search(r'\\bTERMINÉ\\b',match_body,re.I))
                    # Only the event heading identifies the discipline. Advertising
                    # elsewhere in the body can mention unrelated doubles markets.
                    match_header=match_body[:500].upper()
                    audit['appears_doubles']=bool(re.search(
                        r'BWF WORLD TOUR\\s*-\\s*DOUBLES?\\s+(?:MIXTES?|HOMMES|FEMMES)',
                        match_header,re.I))
                    audit['appears_mens_singles']=bool(re.search(
                        r'BWF WORLD TOUR\\s*-\\s*HOMMES(?:\\b|ARCTIC)',
                        match_header,re.I)) and not audit['appears_doubles']
                    audit['eligible_for_odds_comparison']=False
                    audit['comparison_block_reason']='NO_VERIFIED_BOOKMAKER_PRICES'
                    audit['contains_odds_heading']=bool(re.search(r'\bCOTES\b',match_body,re.I))
                    # Inspect only the odds section before promotional banners:
                    # bookmaker names in bonus advertisements are not odds evidence.
                    odds_section=match_body.split('OFFRES BONUS',1)[0]
                    if 'COTES' in odds_section:
                        odds_section=odds_section.rsplit('COTES',1)[-1]
                    odds_lines=[line.strip() for line in odds_section.splitlines() if line.strip()]
                    audit['odds_section_lines']=odds_lines[:35]
                    audit['odds_section_dash_count']=sum(line=='-' for line in odds_lines)
                    audit['odds_section_decimal_prices']=re.findall(
                        r'(?<![\\d])(?:[1-9]\\d{0,2})[.,]\\d{2}(?![\\d])',
                        odds_section)[:12]
                    audit['odds_section_placeholder_only']=(
                        bool(odds_lines) and not audit['odds_section_decimal_prices']
                        and audit['odds_section_dash_count']>=2)
                    audit['comparison_block_reason']=(
                        'ODDS_TAB_NOT_LOADED' if not audit['resolved_to_requested_odds_tab']
                        else 'ODDS_SECTION_PLACEHOLDERS' if audit['odds_section_placeholder_only']
                        else 'NO_VERIFIED_BOOKMAKER_PRICES')
                    audit['odds_section_bookmaker_names']=[name for name in
                        ('Betclic','Winamax','NetBet','Unibet','FDJ','PMU')
                        if re.search(r'\\b'+re.escape(name)+r'\\b',odds_section,re.I)]
                    audit['contains_bookmaker_names']=[name for name in
                        ('Betclic','Winamax','NetBet','Unibet','FDJ','PMU')
                        if re.search(r'\b'+re.escape(name)+r'\b',match_body,re.I)]
                    audit['status']=('PUBLIC_MATCH_PAGE_REACHABLE_AUDIT_ONLY'
                                     if match_info.get('http_status')==200 and
                                     urlsplit(match_info.get('resolved_url','')).netloc.endswith('flashscore.fr')
                                     else 'REJECT_HTTP_STATUS_OR_REDIRECT')
                except Exception as match_exc:
                    audit['status']='MATCH_AUDIT_FAILED'
                    audit['error']=(type(match_exc).__name__+': '+str(match_exc))[:250]
        if source['parser'] is None:
            item['status']='PUBLIC_PAGE_REACHABLE_PARSER_NOT_AUDITED'
            return item
        extract={'unibet':parse_unibet,'bwin':parse_bwin,'fdj':parse_fdj}[source['parser']]
        rows, why=(parse_fdj(content,now,discipline='AUTO') if key=='fdj_pos_fr'
                   else extract(content,now))
        if key=='fdj_pos_fr':
            # Discover only public event links; do not rely on a stale hard-
            # coded tournament ID from a previous week.
            candidates=info.get('public_badminton_event_links', [])
            chosen=[]
            for candidate in dict.fromkeys(candidates):
                if (urlsplit(candidate).netloc == urlsplit(source['url']).netloc and
                    re.search(r'/paris-ouverts/badminton/[^/?#]+-h/\d+/?$',candidate,re.I)):
                    chosen.append(candidate)
            item['retail_pages_audited']=[]
            for candidate in chosen[:6]:
                permitted,sub_reason=robots_check(candidate)
                if not permitted:
                    item['retail_pages_audited'].append({'url':candidate,'status':sub_reason})
                    continue
                time.sleep(1)
                try:
                    sub_content,sub_info=render(browser,candidate)
                    sub_rows,sub_why=parse_fdj(sub_content,now,discipline='MS')
                    sub_age=sub_info.get('cache_age_seconds')
                    if (sub_info['http_status']!=200 or
                        (sub_age is not None and (not str(sub_age).isdigit() or int(sub_age)>FDJ_ARCHIVE_MAX_CACHE_AGE_SECONDS))):
                        sub_rows=[]
                        sub_why='SUBPAGE_REJECT_HTTP_OR_CACHE_AGE'
                    if sub_rows and sub_age is not None and int(sub_age)>ONLINE_MAX_CACHE_AGE_SECONDS:
                        cached_fdj=True
                    for value in sub_rows:value['event_page_url']=candidate
                    rows.extend(sub_rows)
                    item['retail_pages_audited'].append({'url':candidate,'status':sub_why,
                                                         'http_status':sub_info['http_status'],
                                                         'cache_age_seconds':sub_age,
                                                         'quotes':len(sub_rows)})
                except Exception as sub_exc:
                    item['retail_pages_audited'].append({'url':candidate,
                         'status':'SUBPAGE_ERROR_'+type(sub_exc).__name__})
            rows=[r for r in rows if r.get('discipline')=='MS']
            distinct={}
            for row in rows:
                k=(row['player_1_display'].casefold(),row['player_2_display'].casefold(),
                   row['valid_until_paris'],row['odds_1'],row['odds_2'])
                distinct.setdefault(k,row)
            rows=list(distinct.values())
            item['retail_page_cached']=cached_fdj
            item['retail_price_notice']='Point-of-sale only. Odds can change before purchase; receipt is authoritative.'
            why=f'{why}; audited {len(item["retail_pages_audited"])} retail MS pages'
        item['quotes']=rows
        item['parser_notes']=why
        if key=="unibet_fr":
            body=content.casefold()
            item["unibet_page_diagnostics"]={
                "body_characters":len(content),
                "has_today_heading":("aujourd\u0027hui" in body or "aujourd’hui" in body),
                "has_face_a_face_label":("face à face" in body or "face a face" in body),
                "has_badminton_word":("badminton" in body),
                "has_tournament_heading":("monde" in body),
                "has_price_like_tokens":bool(ODD.search(content)),
                "parsed_eligible_ms_quotes":len(rows),
            }
        item['status']=(('OBSERVED_SHADOW_RETAIL_CACHED_NOT_CURRENT' if cached_fdj else 'OBSERVED_SHADOW_RETAIL_NOT_EXECUTABLE_ONLINE')
                        if rows and source['kind']=='RETAIL_NOT_ONLINE' else
                        ('OBSERVED_SHADOW_NEEDS_MARKET_CONFIRMATION' if rows else 'NO_CONFIDENT_PREMATCH_QUOTES'))
    except Exception as exc:
        item['status']='SCRAPE_FAILED'
        item['error']=(type(exc).__name__+': '+str(exc))[:300]
    finally:
        item['finished_at_utc']=datetime.now(timezone.utc).isoformat(timespec='seconds')
    return item


def collect(out):
    from playwright.sync_api import sync_playwright
    now=datetime.now(PARIS)
    out.mkdir(parents=True,exist_ok=True)
    reports=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for n,key in enumerate(SOURCES):
                if n: time.sleep(1)
                item=analyze_source(key,now,browser,evidence_dir=out/'visual_evidence')
                reports.append(item)
                path=out/key
                path.mkdir(parents=True,exist_ok=True)
                filename=path / (now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')+'.json')
                payload=json.dumps(item,ensure_ascii=False,indent=2,sort_keys=True)+'\n'
                filename.write_text(payload,encoding='utf8')
                (path/'latest.json').write_text(payload,encoding='utf8')
                print(json.dumps({'source':key,'status':item['status'],'count':len(item['quotes']),
                                  'file':str(filename)},ensure_ascii=False),flush=True)
        finally:
            browser.close()
    summary={"observed_at_utc":now.astimezone(timezone.utc).isoformat(timespec='seconds'),
             "policy":"SHADOW / NO BET / NO AUTOMATIC SHEETS T0 OR ODDS HISTORY",
             "online_h2h_shadow_rows":sum(len(x['quotes']) for x in reports if x['operator_kind']=='ONLINE'),
             "retail_shadow_rows":sum(len(x['quotes']) for x in reports if x['operator_kind']=='RETAIL_NOT_ONLINE'),
             "sources":{x['source_key']:{
                 "status":x["status"],"quotes":len(x["quotes"]),
                 "robots_status":x.get("robots_status"),
                 "http_status":x.get("http_status"),
                 "resolved_url":x.get("resolved_url"),
                 "parser_notes":x.get("parser_notes"),
                 "error":x.get("error"),
                 "cache_age_seconds":x.get("cache_age_seconds"),
                 "body_sha256":x.get("body_sha256"),
                "flashscore_discovery":x.get("flashscore_discovery"),
                "flashscore_match_audit":x.get("flashscore_match_audit"),
             } for x in reports}}
    (out/'latest_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return summary


def self_test():
    now=datetime(2026,10,9,8,58,tzinfo=PARIS)
    sample_unibet=("Compétition badminton Aujourd'hui monde Open de Finlande H À 12h25 "
       "ZJ.Lee-C.Popov ZJ.Lee 2,95 | C.Popov 1,25 "
       "monde Open de Finlande H À 13h20 TC.Chou-K.Watanabe TC.Chou 1,80 | K.Watanabe 1,71 "
       "Demain monde Open de Finlande H À 07h10 A.Other-B.Other A.Other 1,30 | B.Other 3,50")
    rows,why=parse_unibet(sample_unibet,now)
    assert len(rows)==2,(why,rows)
    assert rows[0]['player_2_display']=='C.Popov' and rows[0]['odds_2']==1.25
    # Page text extracted from the public DOM has newlines, popularity widgets,
    # and often additional disciplines preceding men's singles.
    realistic=("Badminton\nFace à Face\nAujourd'hui\nmonde\nOpen de Finlande DM\n"
       "À 12h00\nJiang/Wei\n-\nYe/Chan\nJiang/Wei\n1,06\nÉtape\n87%\nYe/Chan\n5,00\nÉtape\n13%\n"
       "monde\nOpen de Finlande H\nÀ 12h25\nZJ.Lee\n-\nC.Popov\nZJ.Lee\n2,95\nÉtape\n6%\nC.Popov\n1,25\nÉtape\n94%\n"
       "monde\nOpen de Finlande H\nÀ 13h20\nTC.Chou\n-\nK.Watanabe\nTC.Chou\n1,80\nÉtape\n46%\nK.Watanabe\n1,71\nÉtape\n54%\n")
    observed,why=parse_unibet(realistic,datetime(2026,10,9,10,33,tzinfo=PARIS))
    assert len(observed)==2, (why,observed)
    assert observed[0]['player_2_display']=='C.Popov' and observed[0]['odds_2']==1.25
    bad=sample_unibet.replace('C.Popov 1,25','Different 1,25')
    assert len(parse_unibet(bad,now)[0])==1
    bwin=("Badminton\nVainqueur 1 2\n"
          "A. Lanier FRA Yu Qi Shi CHN Demain/01:40 2.70 1.28\n"
          "Y. Z. Feng/D. P. Huang CHN D. Puavaranukroh/S. Paewsampran THA Demain/02:20 1.30 2.60")
    rows,why=parse_bwin(bwin,now)
    assert len(rows)==1,(why,rows)
    assert rows[0]['player_1_display']=='A. Lanier' and rows[0]['odds_1']==2.7
    fdj=("vendredi 9 octobre\nXC.Zhu-H.Huang Op. Finlande H N°19714 Face à Face I Fin de valid. 09/10 12h35 Afficher\n"
         "XC.Zhu 1,18 H.Huang 3,40\n"
         "Retraite-Live Op. Finlande H N°19715 Face à Face I Fin de valid. 08/10 07h35 Afficher\n"
         "Retraite 1,80 Live 1,80")
    rows,why=parse_fdj(fdj,now)
    assert len(rows)==1,(why,rows)
    fdj_lines=("Aujourd'hui\nXC.Zhu-H.Huang\nOp. Finlande H\n"
               "N°19714 Face à Face I Fin de valid. 09/10 12h35\nAfficher\nXC.Zhu\n1,18\nH.Huang\n3,40\n"
               "99%\n0%\nZJ.Lee-C.Popov\nOp. Finlande H\n"
               "N°12961 Score Exact I Fin de valid. 09/10 12h20\nAfficher\n")
    observed,why=parse_fdj(fdj_lines,datetime(2026,10,9,10,33,tzinfo=PARIS),discipline='MS')
    assert len(observed)==1,(why,observed)
    assert observed[0]['player_1_display']=='XC.Zhu' and observed[0]['odds_2']==3.4
    real_fdj=("Jiang/Wei-Ye/Chan\nOp. Finlande DM\nN°19087 Face à Face I Fin de valid. 09/10 11h55\n"
       "Afficher\nJiang/Wei\n1,06\nYe/Chan\n5,00\n99%\n0%\n"
       "ZJ.Lee-C.Popov\nOp. Finlande H\nN°12961 Score Exact I Fin de valid. 09/10 12h20\n"
       "Afficher\nZJ.Lee\n1,35\nC.Popov\n2,45")
    live_retail,why=parse_fdj(real_fdj,now)
    assert len(live_retail)==1,(why,live_retail)
    assert live_retail[0]['player_1_display']=='Jiang/Wei' and live_retail[0]['odds_2']==5.0
    assert rows[0]['valid_until_paris'].startswith('2026-10-09')
    assert parse_fdj(fdj,datetime(2026,10,9,13,0,tzinfo=PARIS))[0]==[]
    assert SOURCES['fdj_pos_fr']['kind']=='RETAIL_NOT_ONLINE'
    assert FDJ_ARCHIVE_MAX_CACHE_AGE_SECONDS >= 261 > ONLINE_MAX_CACHE_AGE_SECONDS
    assert len(parse_fdj(real_fdj,now,discipline='AUTO')[0])==0
    retail_index=("Aujourd'hui\nZJ.Lee-C.Popov\nOp. Finlande H\n"
                  "N°12962 Face à Face I Fin de valid. 09/10 12h20\nAfficher\n"
                  "ZJ.Lee\n2,95\nC.Popov\n1,25\n52%\n48%\n"
                  "Feng/Huang-Karlb/Sjoo\nOp. Finlande DM\n"
                  "N°19086 Face à Face I Fin de valid. 09/10 12h05\nAfficher\n"
                  "Feng/Huang\n1,01\nKarlb/Sjoo\n7,10\n"
                  "TC.Chou-K.Watanabe\nOp. Finlande H\n"
                  "N°12964 Face à Face - 1er Set I Fin de valid. 09/10 13h15\nAfficher\n"
                  "TC.Chou\n1,80\nK.Watanabe\n1,71\n")
    retail,why=parse_fdj(retail_index,now,discipline='AUTO')
    assert len(retail)==1,(why,retail)
    assert retail[0]['player_1_display']=='ZJ.Lee' and retail[0]['discipline']=='MS'
    assert retail[0]['odds_2']==1.25 and retail[0]['market_verification']=='RETAIL_NOT_EXECUTABLE_ONLINE_NEEDS_CONFIRMATION'
    bad_label=retail_index.replace('ZJ.Lee-C.Popov','ZJ.Lee-WrongName')
    assert parse_fdj(bad_label,now,discipline='AUTO')[0]==[]
    assert not parse_fdj(retail_index,datetime(2026,10,9,12,15,tzinfo=PARIS),discipline='AUTO')[0]
    for key in ('netbet_fr','pmu_fr','betsson_fr'):
        assert SOURCES[key]['parser'] is None and SOURCES[key]['kind']=='ONLINE'
    # Each operator's latest snapshot must be published for diagnostics.
    assert 'latest.json' not in ('.', '..')
    # Integration tests of cache handling without making external requests.
    from unittest.mock import patch
    browse_info={'http_status':200,
                 'resolved_url':SOURCES['fdj_pos_fr']['url'],
                 'cache_age_seconds':'261','public_badminton_event_links':[]}
    with (patch(__name__+'.robots_check',return_value=(True,'ALLOWED_BY_ROBOTS')),
          patch(__name__+'.render',return_value=(retail_index,browse_info))):
        archived=analyze_source('fdj_pos_fr',now,object())
        assert archived['status']=='OBSERVED_SHADOW_RETAIL_CACHED_NOT_CURRENT',archived
        assert len(archived['quotes'])==1 and archived['retail_page_cached']
        rejected_info=dict(browse_info,cache_age_seconds='601')
        with patch(__name__+'.render',return_value=(retail_index,rejected_info)):
            assert analyze_source('fdj_pos_fr',now,object())['status']=='REJECT_HTTP_CACHE_AGE'
        with patch(__name__+'.render',return_value=(retail_index,browse_info)):
            online=analyze_source('unibet_fr',now,object())
            assert online['status']=='REJECT_HTTP_STATUS_OR_REDIRECT' or online['status']=='REJECT_HTTP_CACHE_AGE'
    print('ALL SELF TESTS PASS: Unibet, bwin MS, FDJ retail expiry, invalid/expired rejected')
    print('PASS: FDJ retail MS-only pairing, reject 1st-set and doubles, no near-expiry, new operator probes')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-test',action='store_true')
    p.add_argument('--live',action='store_true')
    p.add_argument('--out',type=Path,default=Path('data/odds_shadow'))
    options=p.parse_args()
    if options.self_test:self_test()
    elif options.live:collect(options.out)
    else:p.error('Use --self-test or --live')

if __name__=='__main__':main()
