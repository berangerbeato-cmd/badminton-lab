#!/usr/bin/env python3
"""Inspect official BWF draw page for order-of-play API evidence, read-only."""
import argparse
import json
import re
import urllib.parse
from pathlib import Path
from official_draw_ajax_evidence import read_html, candidate_scripts, endpoint_evidence
from official_ms_draw_probe import ms_draw_url

PATTERN = re.compile(r"order.?of.?play|orderOfPlay|matchTimeUtc|tmtTab|vue-tournament|oop|schedule", re.I)

def audit(coverage, max_tournaments=4):
    rows = []
    for t in coverage.get("tournaments", []):
        if len(rows) >= max_tournaments:
            break
        url = ms_draw_url(t.get("official_tournament_url"))
        if not url:
            continue
        row = {"tournament_id": str(t.get("tournament_id")), "url": url, "evidence": []}
        try:
            html = read_html(url, 2_000_000)
            scripts = candidate_scripts(html, url)
            for origin, data in [(url, html)]:
                row["evidence"].extend({"source": origin, "snippet": re.sub(r"\\s+", " ", data[max(0,m.start()-90):m.end()+160])[:250]}
                                       for m in list(PATTERN.finditer(data))[:25])
            for script_url in scripts[:8]:
                try:
                    script = read_html(script_url, 750_000)
                    matches = list(PATTERN.finditer(script))
                    row["evidence"].extend({"source": script_url,
                        "snippet": re.sub(r"\\s+", " ", script[max(0,m.start()-90):m.end()+160])[:250]}
                        for m in matches[:15])
                except Exception as exc:
                    row.setdefault("script_errors", []).append({"url": script_url, "error": type(exc).__name__})
        except Exception as exc:
            row["error"] = type(exc).__name__
        row["evidence"] = row["evidence"][:80]
        rows.append(row)
    return {"policy": "RESEARCH_ONLY_NO_BET", "note": "Endpoint evidence only, not verified match scheduling.", "tournaments": rows}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    p.add_argument("--out", type=Path, default=Path("official_oop_endpoint_evidence.json"))
    a=p.parse_args()
    result=audit(json.loads(a.coverage.read_text(encoding="utf-8")))
    a.out.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps([{"tournament_id":x["tournament_id"],"evidence_count":len(x["evidence"]),"error":x.get("error")} for x in result["tournaments"]],indent=2))
if __name__=="__main__":
    main()
