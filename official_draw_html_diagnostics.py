#!/usr/bin/env python3
"""Inspect official BWF tournament HTML for evidence of draw endpoints.

Research only. Does not infer players, fixtures, or match start times.
"""
import argparse
import json
import re
import urllib.request
from pathlib import Path
from official_draw_sources import UA, allowed_official_url

TERMS = ("draw", "match", "schedule", "fixture", "result")


def inspect(url, timeout=12):
    if not allowed_official_url(url):
        return {"status": "INVALID_URL"}
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not allowed_official_url(response.geturl()):
                return {"status": "REDIRECT_OUTSIDE_ALLOWLIST"}
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                return {"status": "NOT_HTML"}
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                return {"status": "HTML_TOO_LARGE"}
            html = data.decode("utf-8", errors="replace")
            links = re.findall(r'href=["\\\']([^"\\\']+)', html, re.I)
            matches = sorted(set(x for x in links if any(t in x.lower() for t in TERMS)))
            return {"status": "INSPECTED", "bytes": len(data),
                    "links_of_interest": matches[:30],
                    "has_next_data": "__NEXT_DATA__" in html,
                    "has_ld_json": "application/ld+json" in html,
                    "mentions_draw": bool(re.search(r"\\bdraws?\\b", html, re.I))}
    except Exception as exc:
        return {"status": "FETCH_ERROR", "error_type": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    parser.add_argument("--out", type=Path, default=Path("official_draw_html_diagnostics.json"))
    args = parser.parse_args()
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    rows = [{"id": t["tournament_id"], "name": t.get("name"),
             "inspection": inspect(t.get("official_tournament_url"))}
            for t in coverage["tournaments"] if t.get("needs_secondary_source")]
    args.out.write_text(json.dumps({"policy": "RESEARCH_ONLY_NO_BET",
                                    "tournaments": rows}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{"id": x["id"], "status": x["inspection"]["status"],
                       "links_of_interest": x["inspection"].get("links_of_interest", []),
                       "has_next_data": x["inspection"].get("has_next_data"),
                       "has_ld_json": x["inspection"].get("has_ld_json")}
                      for x in rows], indent=2))


if __name__ == "__main__":
    main()
