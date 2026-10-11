#!/usr/bin/env python3
"""Probe explicit men's singles draw URLs on official BWF sites.

Research-only: no player pairing extraction and no betting decisions.
"""
import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path
from official_draw_sources import UA, allowed_official_url

LIMIT = 2_000_000


def ms_draw_url(tournament_url):
    if not allowed_official_url(tournament_url):
        return None
    parsed = urllib.parse.urlsplit(tournament_url)
    path = parsed.path.rstrip("/") + "/draws/draw/ms"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def probe(url, timeout=12):
    if not url:
        return {"status": "INVALID_URL"}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            if not allowed_official_url(final_url):
                return {"status": "UNEXPECTED_REDIRECT"}
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                return {"status": "NON_HTML", "final_url": final_url}
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                return {"status": "TOO_LARGE", "final_url": final_url}
            html = data.decode("utf-8", errors="replace")
            # Do not confuse a generic HTML page with an actual published MS draw.
            return {"status": "PAGE_ACCESSIBLE", "final_url": final_url,
                    "html_bytes": len(data),
                    "explicit_ms_url": urllib.parse.urlsplit(final_url).path.rstrip("/").endswith("/draw/ms"),
                    "contains_draw_table": "drawTable" in html,
                    "contains_match_draw_link": "matchDrawLink" in html,
                    "contains_single_draw_link": "singleDrawLink" in html}
    except Exception as exc:
        return {"status": "FETCH_ERROR", "error_type": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    parser.add_argument("--out", type=Path, default=Path("official_ms_draw_probe.json"))
    args = parser.parse_args()
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    rows = []
    for t in coverage.get("tournaments", []):
        if not t.get("needs_secondary_source"):
            continue
        url = ms_draw_url(t.get("official_tournament_url"))
        rows.append({"id": t.get("tournament_id"), "name": t.get("name"),
                     "requested_url": url, "probe": probe(url)})
    args.out.write_text(json.dumps({"policy": "RESEARCH_ONLY_NO_BET",
                                    "tournaments": rows}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
