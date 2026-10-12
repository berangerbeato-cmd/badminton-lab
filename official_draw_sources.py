#!/usr/bin/env python3
"""Read-only source discovery for every selected BWF tournament.

Probe only the official BWF tournament URL provided by its index. No scraping
of third-party sites, no challenge bypass, no inferred match times.
"""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "BadmintonLabResearch/0.1 (official fixture source availability audit)"
MAX_BYTES = 1000000


def allowed_official_url(url):
    if not isinstance(url, str):
        return False
    parsed = urllib.parse.urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in
            {"bwfbadminton.com", "www.bwfbadminton.com",
             "bwfworldtour.bwfbadminton.com"} and
            (parsed.path.startswith("/tournament/") or parsed.path.startswith("/tournaments/"))
            and not parsed.username and not parsed.password)


def probe(url, timeout=10):
    if not allowed_official_url(url):
        return {"status": "INVALID_OFFICIAL_URL"}
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            final = response.geturl()
            if not allowed_official_url(final):
                return {"status": "UNEXPECTED_REDIRECT"}
            # Only verify the response and its content type; do not download
            # or parse the full page in this availability probe.
            response.read(1024)
            content_type = response.headers.get("Content-Type", "")
            return {"status": "PAGE_ACCESSIBLE" if "text/html" in content_type.lower()
                    else "NON_HTML_RESPONSE", "http_status": response.status}
    except urllib.error.HTTPError as exc:
        return {"status": "ACCESS_DENIED" if exc.code in (401, 403, 429)
                else "HTTP_ERROR", "http_status": exc.code}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "NETWORK_ERROR", "error_type": type(exc).__name__}


def build(report, max_probes=10):
    rows = []
    for t in report.get("tournaments", []):
        if not t.get("needs_secondary_source"):
            continue
        url = t.get("official_tournament_url")
        rows.append({"tournament_id": t.get("tournament_id"), "name": t.get("name"),
                     "official_url": url, "result": probe(url) if len(rows) < max_probes
                     else {"status": "NOT_PROBED_LIMIT"}})
    return {"policy": "RESEARCH_ONLY_NO_BET", "generated_at_utc":
            datetime.now(timezone.utc).isoformat(),
            "note": "Availability probe only. PAGE_ACCESSIBLE does not imply a published draw or a parsable match list.",
            "sources": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    ap.add_argument("--out", type=Path, default=Path("official_draw_source_probe.json"))
    ap.add_argument("--max-probes", type=int, default=10)
    args = ap.parse_args()
    if not 0 <= args.max_probes <= 20:
        ap.error("--max-probes must be 0..20")
    report = build(json.loads(args.coverage.read_text(encoding="utf-8")), args.max_probes)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sources": len(report["sources"]), "statuses":
        [x["result"]["status"] for x in report["sources"]]}, indent=2))


if __name__ == "__main__":
    main()
