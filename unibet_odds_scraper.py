#!/usr/bin/env python3
"""Cautious read-only observation of public Unibet.fr badminton H2H markets.

Not a bookmaker API; not proof that the quote is executable. No login, no
captcha/geo bypass, no betting. Preserve evidence and reject stale/ambiguous
records. This is an experimental collector until the live DOM has been audited.

    python unibet_odds_scraper.py --self-test
    python unibet_odds_scraper.py --live --out data/odds/unibet_fr
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import hashlib
import json
import re
import sys
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.robotparser import RobotFileParser

URL = "https://www.unibet.fr/paris-badminton"
PARIS = ZoneInfo("Europe/Paris")
USER_AGENT = "BadmintonResearchOdds/0.1 (public research, read-only)"
ODD = re.compile(r"(?<!\d)(\d{1,2}[,.]\d{2})(?!\d)")
HEADER = re.compile(
    r"(?:^|\s)monde\s+(?P<tournament>.+?)\s+(?P<discipline>H|F|DM|DF|DH)\s+"
    r"[ÀA]\s*(?P<clock>\d{1,2}h\d{2})\s+(?P<body>.*?)(?=\s+monde\s+|\s+Fermer\s+Mon\s+panier|$)",
    flags=re.I | re.S,
)


def canonical(s):
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def is_allowed():
    """Fail closed on robots fetch problems (apart from explicit 404)."""
    url = "https://www.unibet.fr/robots.txt"
    try:
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=18) as response:
            rules = response.read(250_000).decode("utf-8", "replace")
            rp = RobotFileParser()
            rp.parse(rules.splitlines())
            return rp.can_fetch(USER_AGENT, URL), "robots parsed"
    except HTTPError as e:
        if e.code == 404:
            return True, "robots 404"
        return False, f"robots HTTP {e.code}"
    except (URLError, TimeoutError, OSError) as e:
        return False, f"robots unavailable: {type(e).__name__}"


def section(text, tag):
    # The date heading must precede the sporting listings.
    match = re.search(r"\b" + tag + r"\b", text, flags=re.I)
    if not match:
        return ""
    end = re.search(r"\b(?:Aujourd'hui|Demain|Fermer Mon panier)\b", text[match.end():], flags=re.I)
    return text[match.end(): match.end() + end.start()] if end else text[match.end():]


def parse_listings(text, local_now):
    """Conservative parser for PUBLIC text of current-day H2H listings.

    Records only where both labels and prices can be bound to the same match;
    never reuse old quotes or assume the listing is still executable.
    """
    # Public Unibet listings insert decorative popularity widgets between a
    # player's price and the opposing player: "Étape 6%" / "Étape 94%".
    # These are not market prices or contestant names and must be ignored.
    text = re.sub(r"\bÉtape\s*\d{1,3}\s*%", " ", text.replace("\xa0", " "), flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    # Scope quote extraction to current-day matches only.
    today = section(text, "Aujourd'hui")
    if not today:
        return [], "missing Aujourd'hui section"
    matches = []
    for hit in HEADER.finditer(today):
        body = hit.group("body").strip(" |")
        odds = list(ODD.finditer(body))
        if len(odds) < 2:
            continue
        first, second = odds[:2]
        if len(odds) > 2 and odds[2].start() - second.end() < 25:
            continue  # Ambiguous extra market odds, refuse.
        before = body[:first.start()].strip()
        after = body[first.end():second.start()].strip(" |")
        if "-" not in before:
            continue
        left, right = before.split("-", 1)
        p1 = left.strip(" |")
        if not p1 or not right.rstrip().endswith(p1):
            continue
        p2 = right.rsplit(p1, 1)[0].strip(" |")
        if not p2 or canonical(after) != canonical(p2):
            continue
        o1, o2 = [float(m.group().replace(",", ".")) for m in (first, second)]
        if not (1.01 <= o1 <= 99 and 1.01 <= o2 <= 99):
            continue
        hour, minute = (int(x) for x in hit.group("clock").split("h"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        start = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        status = "OBSERVED_NOT_BOOKMAKER_TIMESTAMPED"
        if start <= local_now:
            status = "SKIP_LISTED_START_ALREADY_PASSED"
        matches.append({
            "tournament": hit.group("tournament").strip(),
            "discipline_display": hit.group("discipline").upper(),
            "player_1_display": p1,
            "player_2_display": p2,
            "odds_1": o1,
            "odds_2": o2,
            "listed_start_paris": start.isoformat(timespec="minutes"),
            "status": status,
            "bookmaker_event_id": None,
            "market": "Face à Face (listing; verify market per detail)",
            "raw_event_excerpt": body[:180],
        })
    if not matches:
        return [], "no confidently parsed H2H pairs"
    return matches, "parsed"


def live_read():
    # No accounts or bypass. Prefer the rendered PUBLIC browser page to a
    # search-engine cache, which is not a fresh quote.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="fr-FR", timezone_id="Europe/Paris")
        response = page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        info = {
            "http_status": response.status if response else None,
            "cache_age": (response.headers.get("age") if response else None),
            "response_date": (response.headers.get("date") if response else None),
            "page_url": page.url,
        }
        txt = page.locator("body").inner_text(timeout=15000)
        info["body_sha256"] = hashlib.sha256(txt.encode()).hexdigest()
        # Diagnostic only: public event links and limited non-sensitive text.
        info["event_urls"] = list(dict.fromkeys(page.locator(
            "a[href*='/paris-badminton/']"
        ).evaluate_all("els => els.map(a => a.href).filter(Boolean)")))[:120]
        info["text_excerpt"] = txt[:3000]
        browser.close()
        return txt, info


def collect(now, out):
    permitted, reason = is_allowed()
    obj = {
        "observed_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "observed_at_paris": now.isoformat(timespec="seconds"),
        "source": URL,
        "operator": "Unibet.fr",
        "provenance": "direct HTTP browser observation, NOT bookmaker-issued price timestamp",
        "robots_check": reason,
        "quotes": [],
        "status": "NOT_CHECKED",
        "policy": "research/simulation only; no bet; do not write Odds History without additional verification",
    }
    if not permitted:
        obj["status"] = "ROBOTS_DENIED_OR_UNAVAILABLE"
    else:
        try:
            content, extra = live_read()
            obj.update(extra)
            obj["quotes"], msg = parse_listings(content, now)
            if extra.get("http_status") != 200 or "www.unibet.fr" not in extra["page_url"]:
                obj["status"] = "HTTP_OR_REDIRECT_REJECTED"
                obj["quotes"] = []
            elif obj["quotes"]:
                age = extra.get("cache_age")
                try:
                    cache_old = age is not None and int(age) > 120
                except (ValueError, TypeError):
                    cache_old = True
                if cache_old:
                    obj["status"] = "CACHE_TOO_OLD"
                    for e in obj["quotes"]:
                        e["status"] = "CACHE_TOO_OLD_NOT_ADMISSIBLE"
                else:
                    obj["status"] = "PUBLIC_QUOTES_OBSERVED_NEED_CONFIRMATION"
            else:
                obj["status"] = "NO_VALID_QUOTES: " + msg
        except Exception as exc:
            obj["status"] = "SCRAPE_FAILED: " + type(exc).__name__
            obj["error"] = str(exc)[:350]
    out.mkdir(parents=True, exist_ok=True)
    fname = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ") + ".json"
    filepath = out / fname
    filepath.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(filepath), "status": obj["status"],
                      "pairs": len(obj["quotes"]), "robots": reason}, ensure_ascii=False))
    return obj


def self_test():
    f = ("Compétition badminton Badminton Face à Face Aujourd'hui "
         "monde Open de Finlande H À 12h25 ZJ.Lee-C.Popov ZJ.Lee 2,95 | C.Popov 1,25 "
         "monde Open de Finlande H À 13h20 TC.Chou-K.Watanabe TC.Chou 1,80 | K.Watanabe 1,71 "
         "monde Open de Finlande H À 13h50 ZA.Hu-A.Antonsen ZA.Hu 2,65 | A.Antonsen 1,31 "
         "Demain monde Masters Chine H À 08h30 J.Gunawan-CY.Lee J.Gunawan 2,05 | CY.Lee 1,52")
    today = datetime(2026,10,9,8,58,tzinfo=PARIS)
    items,msg = parse_listings(f,today)
    assert len(items)==3,(msg,items)
    assert items[0]["player_1_display"]=="ZJ.Lee" and items[0]["player_2_display"]=="C.Popov"
    assert items[0]["odds_2"] ==1.25 and items[1]["odds_1"]==1.8
    assert items[2]["listed_start_paris"].startswith("2026-10-09T13:50")
    real_page=("Badminton\nFace à Face\nRace to $n points\nAujourd'hui\n"
      "monde\nOpen de Finlande DM\nÀ 12h00\nJiang/Wei\n-\nYe/Chan\n"
      "Jiang/Wei\n1,06\nÉtape\n86%\nYe/Chan\n5,00\nÉtape\n14%\n"
      "monde\nOpen de Finlande H\nÀ 12h25\nZJ.Lee\n-\nC.Popov\n"
      "ZJ.Lee\n2,95\nÉtape\n6%\nC.Popov\n1,25\nÉtape\n94%\n"
      "monde\nOpen de Finlande H\nÀ 13h20\nTC.Chou\n-\nK.Watanabe\n"
      "TC.Chou\n1,80\nÉtape\n32%\nK.Watanabe\n1,71\nÉtape\n68%\nDemain\n")
    live_items,why=parse_listings(real_page,today)
    assert len(live_items)==3,(why,live_items)
    assert any(x['player_2_display']=='C.Popov' and x['odds_2']==1.25 for x in live_items)
    bad = f.replace("C.Popov 1,25", "WrongName 1,25")
    items,_=parse_listings(bad,today)
    assert len(items)==2  # ambiguous labels rejected
    _,msg=parse_listings("Pas de badminton",today)
    assert "missing" in msg
    passed = parse_listings(f,datetime(2026,10,9,18,0,tzinfo=PARIS))[0]
    assert all(x["status"] == "SKIP_LISTED_START_ALREADY_PASSED" for x in passed)
    print("SELF TEST PASS: parse 3 quotes, reject mismatches, reject absent, elapsed starts")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--live",action="store_true")
    p.add_argument("--out",type=Path,default=Path("data/odds/unibet_fr"))
    a=p.parse_args()
    if a.self_test: self_test()
    elif a.live: collect(datetime.now(PARIS),a.out)
    else: p.error("choose --self-test or --live")

if __name__=="__main__": main()
