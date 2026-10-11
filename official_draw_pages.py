#!/usr/bin/env python3
"""Inspect official BWF draw pages for structural clues only.

No player extraction, no match-time inference, no betting actions.
"""
import argparse
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from official_draw_sources import UA, allowed_official_url

MAX_BYTES = 2_000_000


def draw_url(tournament_url):
    if not allowed_official_url(tournament_url):
        return None
    parsed = urllib.parse.urlsplit(tournament_url)
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path + "/draws/", "", ""))


def inspect(url, timeout=12):
    if not url:
        return {"status": "INVALID_URL"}
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not allowed_official_url(response.geturl()):
                return {"status": "UNEXPECTED_REDIRECT"}
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                return {"status": "NON_HTML"}
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return {"status": "TOO_LARGE"}
            html = data.decode("utf-8", errors="replace")
            links = re.findall(r'href=["\\\']([^"\\\']+)', html, re.I)
            interesting = sorted(set(
                urllib.parse.urljoin(response.geturl(), x.replace("&amp;", "&"))
                for x in links if any(w in x.lower() for w in ("draw", "match", "schedule"))
                and not x.lower().startswith(("javascript:", "data:"))
            ))
            own_host = urllib.parse.urlsplit(response.geturl()).hostname
            interesting = [x for x in interesting
                           if urllib.parse.urlsplit(x).hostname == own_host][:25]
            return {"status": "INSPECTED", "final_url": response.geturl(),
                    "html_bytes": len(data), "draw_links": interesting,
                    "has_draw_table": "drawTable" in html,
                    "has_match_draw_link": "matchDrawLink" in html,
                    "has_single_draw_link": "singleDrawLink" in html,
                    "mentions_ms": bool(re.search(r'\\b(MS|Men.s Singles)\\b', html, re.I))}
    except Exception as exc:
        return {"status": "FETCH_ERROR", "error_type": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    parser.add_argument("--out", type=Path, default=Path("official_draw_pages.json"))
    args = parser.parse_args()
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    rows = []
    for t in coverage.get("tournaments", []):
        if t.get("needs_secondary_source"):
            url = draw_url(t.get("official_tournament_url"))
            rows.append({"id": t.get("tournament_id"), "name": t.get("name"),
                         "draw_url": url, "inspection": inspect(url)})
    report = {"policy": "RESEARCH_ONLY_NO_BET", "tournaments": rows,
              "note": "Structural page inspection only. No validated fixture extraction."}
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{"id": t["id"], "draw_url": t["draw_url"],
                       "inspection": t["inspection"]} for t in rows], indent=2))


if __name__ == "__main__":
    main()
