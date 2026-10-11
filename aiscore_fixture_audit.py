#!/usr/bin/env python3
"""Read public AiScore fixtures and compare with BWF, without changing forecasts.

Run explicitly with --live. Uses stdlib, no credentials, browser, paid API or
anti-bot bypass. Stops on access restrictions. Coverage is bounded, not exhaustive.
Source timestamps are observations, NEVER verified kickoffs or executable bets.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from bwf_sync import atomic_write_json, extract_matches, is_elite, read_json

BASE = "https://www.aiscore.com"
HOME = BASE + "/badminton"
UA = "BadmintonLabFixtureAudit/1.0 (public read-only research)"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Node:
    def __init__(self, tag="root", attrs=(), section=""):
        self.tag, self.attrs, self.section = tag, dict(attrs), section
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children).strip()


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.section = ""
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.section)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                if tag == "h2":
                    self.section = " ".join(self.stack[i].text().split())
                del self.stack[i:]
                break

    def handle_data(self, text):
        self.stack[-1].children.append(text)


def public_url(value):
    url = urllib.parse.urljoin(BASE, value or "")
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.netloc != "www.aiscore.com" or p.query or p.fragment:
        raise ValueError("Only canonical public AiScore URLs are allowed")
    return url.rstrip("/")


def match_url(value):
    url = public_url(value)
    path = urllib.parse.urlsplit(url).path
    if not re.fullmatch(r"/badminton/match-[a-z0-9-]+/[a-z0-9]+(?:/h2h)?", path):
        raise ValueError("Not an AiScore badminton match URL")
    return url.removesuffix("/h2h")


def name_keys(name):
    s = unicodedata.normalize("NFKD", name).casefold()
    words = re.findall(r"[a-z0-9]+", "".join(c for c in s if not unicodedata.combining(c)))
    return {"compact:" + "".join(words), "tokens:" + " ".join(sorted(words))} if len(words) >= 2 else set()


def player_index(data_dir):
    index = defaultdict(set)
    records = []
    for path in sorted((data_dir / "matches").glob("*.json.gz")):
        tid = path.name.split(".")[0]
        for m in extract_matches(read_json(path)).values():
            if (m.get("draw_name") or "").split(" ")[0] != "MS":
                continue
            if any((m.get(k) or {}).get("id") for k in ("t1p2_detail", "t2p2_detail")):
                continue
            records.append((tid, m))
            for key in ("t1p1_detail", "t2p1_detail"):
                p = m.get(key) or {}
                if not isinstance(p, dict) or not p.get("id"):
                    continue
                names = [p.get("name_display") or ""]
                if p.get("first_name") and p.get("last_name"):
                    names += [p["first_name"] + " " + p["last_name"], p["last_name"] + " " + p["first_name"]]
                for name in names:
                    for signature in name_keys(name):
                        index[signature].add(str(p["id"]))
    if not records:
        raise ValueError("BWF MS archives are required for comparison")
    return index, records


def resolve_player(name, index):
    hits = set().union(*(index.get(k, set()) for k in name_keys(name)))
    return next(iter(hits)) if len(hits) == 1 else None


def properties(node, key):
    return [n for n in node.walk() if n.attrs.get("itemprop") == key]


def single_value(nodes, attribute=None):
    values = {" ".join((n.attrs.get(attribute, "") if attribute else n.text()).split()) for n in nodes}
    values.discard("")
    return next(iter(values)) if len(values) == 1 else ""


def discover_h2h(html):
    return list(dict.fromkeys(public_url(n.attrs["href"]) for n in Document(html).root.walk()
                             if n.tag == "a" and re.fullmatch(r"/head-to-head/badminton/[a-z0-9-]+", n.attrs.get("href", ""))))


def fixture_links(html, now, days):
    """Only visible future Fixture rows; never historical H2H result rows."""
    links = {}
    for row in Document(html).root.walk():
        if not row.attrs.get("itemtype", "").endswith("/SportsEvent") or not row.section.endswith(" Fixture"):
            continue
        visible = single_value([n for n in properties(row, "startDate") if n.tag != "meta"])
        try:
            day = datetime.strptime(visible, "%Y/%m/%d").date()
        except ValueError:
            continue
        if not now.date() <= day <= (now + timedelta(days=days)).date():
            continue
        tournament = single_value(properties(row, "location"), "content")
        if not singles_label(tournament):
            continue
        a = single_value(properties(row, "homeTeam"))
        b = single_value(properties(row, "awayTeam"))
        urls = {match_url(n.attrs["href"]) for n in properties(row, "url") if n.tag == "a" and n.attrs.get("href")}
        if len(urls) == 1 and a and b and a != b:
            url = next(iter(urls))
            links.setdefault(url, []).append({"source_date": day.isoformat(), "player_a": a, "player_b": b,
                                              "tournament": tournament,
                                              "metadata_start_raw": single_value(properties(row, "startDate"), "content")})
    return links


def singles_label(label):
    label = label.casefold()
    return bool(re.search(r"\b(singles|ms)\b", label)) and not re.search(r"women|female|doubles|mixed|junior|under|u19|para", label)


def structured_events(html):
    events = []
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("@type") == "SportsEvent":
                events.append(value)
            if "@graph" in value:
                visit(value["@graph"])
    for node in Document(html).root.walk():
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            try:
                visit(json.loads(node.text()))
            except (ValueError, TypeError):
                continue
    return events


def parse_match(html, url, now, days):
    url = match_url(url)
    found = []
    for e in structured_events(html):
        if str(e.get("sport", "")).casefold() != "badminton":
            continue
        if str(e.get("eventStatus", "")).rsplit("/", 1)[-1] != "EventScheduled":
            continue
        home, away = e.get("homeTeam") or {}, e.get("awayTeam") or {}
        if not isinstance(home, dict) or not isinstance(away, dict):
            continue
        if not isinstance(home.get("name"), str) or not isinstance(away.get("name"), str):
            continue
        a, b = home["name"].strip(), away["name"].strip()
        loc = e.get("location") or []
        if isinstance(loc, dict):
            loc = [loc]
        labels = {x.get("name", "") for x in loc if isinstance(x, dict)}
        if len(labels) != 1:
            continue
        tournament = next(iter(labels))
        if not singles_label(tournament) or not a or not b or a == b or any(c in a+b for c in "/&"):
            continue
        if any(re.search(r"\b(tbd|bye|qualifier|winner|unknown)\b", x, re.I) for x in (a, b)):
            continue
        raw_start = e.get("startDate", "")
        try:
            start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
            if start.utcoffset() is None:
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        # Calendar coverage only. A source time on today's date is not proof
        # that the match has not started (or that midnight is a real kickoff).
        day = start.date()
        if not now.date() <= day <= (now + timedelta(days=days)).date():
            continue
        if start.astimezone(timezone.utc) <= now:
            continue
        found.append({"source_match_id": url.rsplit("/", 1)[-1], "source_url": url,
                      "tournament": tournament, "player_a": a, "player_b": b,
                      "source_date": day.isoformat(), "source_start_raw": raw_start,
                      "source_time_precision": "REPORTED_DATETIME_UNVERIFIED",
                      "start_utc": None, "kickoff_verified": False, "decision": "NO_BET",
                      "observed_at_utc": now.isoformat(),
                      "evidence_sha256": hashlib.sha256(html.encode()).hexdigest()})
    unique = {json.dumps(x, sort_keys=True): x for x in found}
    if len(unique) != 1:
        raise ValueError("No unique future scheduled singles SportsEvent in match page")
    return next(iter(unique.values()))


def tournament_key(name):
    name = re.sub(r"\([^)]*\)", "", name.split(",")[0])
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def compare_fixture(row, index, bwf_records, tournaments, discovery):
    row = dict(row)
    row["player_a_bwf_id"] = resolve_player(row["player_a"], index)
    row["player_b_bwf_id"] = resolve_player(row["player_b"], index)
    reasons = []
    ids = (row["player_a_bwf_id"], row["player_b_bwf_id"])
    if not all(ids) or ids[0] == ids[1]:
        reasons.append("PLAYER_ID_UNRESOLVED_OR_AMBIGUOUS")
    key = tournament_key(row["tournament"])
    day = row["source_date"]
    candidates = [t for t in tournaments if is_elite(t) and len(key) >= 5
                  and key in tournament_key(t.get("name", ""))
                  and str(t.get("start_date", ""))[:10] <= day <= str(t.get("end_date", ""))[:10]]
    tid = str(candidates[0]["id"]) if len(candidates) == 1 else None
    row["bwf_tournament_id"] = tid
    if tid is None:
        reasons.append("TOURNAMENT_UNRESOLVED_OR_OUT_OF_SCOPE")
    observations = discovery.get(row["source_url"], [])
    row["discovery_observations"] = observations
    row["warnings"] = []
    if any(x.get("metadata_start_raw", "")[:10] not in ("", x["source_date"]) for x in observations):
        row["warnings"].append("DISCOVERY_METADATA_DATE_DIFFERS_FROM_VISIBLE_DATE")
    if any(x["source_date"] != day for x in observations):
        reasons.append("SOURCE_DATE_CONFLICT")
    for observation in observations:
        source_ids = {resolve_player(observation["player_a"], index), resolve_player(observation["player_b"], index)}
        if all(ids) and None not in source_ids and source_ids != set(ids):
            reasons.append("SOURCE_PAIRING_CONFLICT")
    matches = []
    if all(ids) and tid:
        for mtid, m in bwf_records:
            p1, p2 = m.get("t1p1_detail") or {}, m.get("t2p1_detail") or {}
            if mtid == tid and {str(p1.get("id")), str(p2.get("id"))} == set(ids):
                matches.append(m)
    row["bwf_match_ids"] = sorted({f"{tid}:{m['id']}" for m in matches})
    if any(m.get("winner") in (1, 2) for m in matches):
        reasons.append("ALREADY_COMPLETED_IN_BWF")
    for m in matches:
        try:
            bwf_day = datetime.fromtimestamp(int(m["start_time"]), timezone.utc).date().isoformat()
            if bwf_day != day:
                reasons.append("BWF_DATE_CONFLICT")
        except (KeyError, ValueError, TypeError, OverflowError):
            pass
    row["blockers"] = sorted(set(reasons))
    row["comparison"] = "QUARANTINED" if reasons else "ALSO_IN_BWF" if matches else "ADDITIONAL_TO_BWF"
    row["status"] = "RESEARCH_ONLY_NO_BET"
    return row


def quarantine_duplicate_pairings(rows):
    groups = defaultdict(list)
    for row in rows:
        ids = row["player_a_bwf_id"], row["player_b_bwf_id"]
        if all(ids) and row["bwf_tournament_id"]:
            groups[(row["bwf_tournament_id"], row["source_date"], tuple(sorted(ids)))].append(row)
    for group in groups.values():
        if len({r["source_match_id"] for r in group}) > 1:
            for row in group:
                row["blockers"] = sorted(set(row["blockers"] + ["MULTIPLE_SOURCE_IDS_FOR_SAME_PAIRING"]))
                row["comparison"] = "QUARANTINED"


class AccessStopped(RuntimeError):
    pass


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicClient:
    def __init__(self, timeout=15, delay=1.0, max_pages=40):
        self.timeout, self.delay, self.max_pages = timeout, delay, max_pages
        self.pages, self.last_request, self.request_count = [], 0.0, 0
        self.opener = urllib.request.build_opener(SameHostRedirect())
        self.robot = urllib.robotparser.RobotFileParser()
        self.robot.parse(self._request(BASE + "/robots.txt").splitlines())

    def _request(self, url):
        if self.request_count >= self.max_pages:
            raise AccessStopped("Request limit reached; coverage is partial")
        if self.pages:
            time.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.request_count += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                if urllib.parse.urlsplit(response.url).netloc != "www.aiscore.com":
                    raise AccessStopped("Unexpected redirect outside AiScore")
                body = response.read(3_000_001)
                if len(body) > 3_000_000:
                    raise ValueError("Page exceeds size limit")
                content = body.decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise AccessStopped(f"HTTP {exc.code}: source access stopped") from exc
            raise
        if any(x in content.casefold() for x in ("cf-chl-", "verify you are human", "captcha challenge")):
            raise AccessStopped("Challenge page: source access stopped")
        self.pages.append({"url": url, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                           "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        return content

    def get(self, url):
        url = public_url(url)
        if not self.robot.can_fetch(UA, url):
            raise AccessStopped("robots.txt disallows this URL")
        return self._request(url)


def collect(data_dir, now, days, client):
    index, records = player_index(data_dir)
    tournaments = read_json(data_dir / "index.json")
    errors, rows, discovery = [], [], {}
    h2h_links = []
    try:
        h2h_links = discover_h2h(client.get(HOME))
        if not h2h_links:
            raise ValueError("No H2H discovery links: empty source or changed schema")
        for url in h2h_links[:20]:
            print("Discovering fixtures:", url, flush=True)
            try:
                html = client.get(url)
                for event_url, observations in fixture_links(html, now, days).items():
                    discovery.setdefault(event_url, []).extend(dict(o, discovery_url=url) for o in observations)
            except (ValueError, urllib.error.URLError, OSError) as exc:
                errors.append({"url": url, "error": str(exc)[:180]})
        for url in sorted(discovery):
            print("Reading match:", url, flush=True)
            try:
                html = client.get(url)
                row = parse_match(html, url, datetime.now(timezone.utc), days)
                rows.append(compare_fixture(row, index, records, tournaments, discovery))
            except (ValueError, urllib.error.URLError, OSError) as exc:
                errors.append({"url": url, "error": str(exc)[:180]})
    except (AccessStopped, ValueError, urllib.error.URLError, OSError) as exc:
        errors.append({"error": str(exc)[:180]})
    quarantine_duplicate_pairings(rows)
    counts = Counter(x["comparison"] for x in rows)
    refresh = read_json(data_dir.parent / "model_v3_2_live" / "bwf_refresh_latest.json") or {}
    return {"schema_version": 1, "policy": "RESEARCH_ONLY_NO_BET", "source": "AiScore public HTML",
            "generated_at_utc": now.isoformat(), "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "horizon_days": days, "coverage": "BOUNDED_DISCOVERY_NOT_EXHAUSTIVE",
            "collection_status": "PARTIAL_OR_FAILED" if errors else "COMPLETED_BOUNDED_SCAN",
            "h2h_links_found": len(h2h_links), "h2h_links_limit": 20,
            "match_links_found": len(discovery), "source_pages": client.pages,
            "requests_attempted": client.request_count,
            "bwf_reference": {"archives_scanned": len(list((data_dir / "matches").glob("*.json.gz"))),
                              "refresh_observations": refresh.get("tournament_refreshes", []),
                              "note": "Comparison uses the local BWF checkout, not an independent live refresh."},
            "counts": dict(counts), "fixtures": rows, "errors": errors,
            "limitations": ["No BWF archives, model inputs, odds or existing schedules are modified.",
                            "Source times are unverified. Dates and identities may still change.",
                            "An empty BWF match feed does not prove the draw is unpublished."]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true", help="Explicitly enable public read-only HTTP collection")
    p.add_argument("--data-dir", type=Path, default=Path("data/bwf"))
    p.add_argument("--out", type=Path, default=Path("data/fixture_sources/aiscore"))
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--max-pages", type=int, default=40)
    args = p.parse_args()
    if not args.live:
        p.error("Use --live to explicitly enable collection")
    if not 1 <= args.days <= 31 or not 2 <= args.max_pages <= 60 or args.delay < 1 or not 1 <= args.timeout <= 30:
        p.error("days 1..31, max-pages 2..60, delay >=1 second, timeout 1..30 required")
    now = datetime.now(timezone.utc)
    try:
        client = PublicClient(args.timeout, args.delay, args.max_pages)
        report = collect(args.data_dir, now, args.days, client)
    except (AccessStopped, ValueError, urllib.error.URLError, OSError) as exc:
        report = {"schema_version": 1, "policy": "RESEARCH_ONLY_NO_BET", "generated_at_utc": now.isoformat(),
                  "collection_status": "FAILED", "fixtures": [], "counts": {}, "errors": [{"error": str(exc)[:180]}]}
    atomic_write_json(args.out / "latest.json", report)
    atomic_write_json(args.out / (now.strftime("%Y%m%dT%H%M%S%fZ") + ".json"), report)
    print(json.dumps({k: report.get(k) for k in ("collection_status", "h2h_links_found", "match_links_found", "counts", "errors")}, indent=2))
    return 2 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
