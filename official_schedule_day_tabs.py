#!/usr/bin/env python3
"""Extract explicit official BWF schedule-day tabs; never infer match start times."""
import argparse
import json
import re
from pathlib import Path
from official_draw_ajax_evidence import read_html
from official_ms_draw_probe import ms_draw_url

DAY_TAB = re.compile(r"activeTmtTab\s*===\s*['\"](\d{4}-\d{2}-\d{2})['\"]")

def extract_days(html):
    from datetime import date
    days = set()
    for value in DAY_TAB.findall(html):
        try:
            date.fromisoformat(value)
            days.add(value)
        except ValueError:
            pass
    return sorted(days)

def audit(coverage):
    rows = []
    for tournament in coverage.get("tournaments", [])[:7]:
        url = ms_draw_url(tournament.get("official_tournament_url"))
        if not url:
            continue
        row = {"tournament_id": str(tournament.get("tournament_id")), "url": url,
               "schedule_days": [], "match_times_verified": 0}
        try:
            row["schedule_days"] = extract_days(read_html(url, 2_000_000))
        except Exception as exc:
            row["error"] = type(exc).__name__
        rows.append(row)
    return {"policy": "RESEARCH_ONLY_NO_BET", "source": "BWF_OFFICIAL_PAGE",
            "warning": "Schedule day tabs indicate possible OOP days, not confirmed dates for any individual match.",
            "tournaments": rows}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    p.add_argument("--out", type=Path, default=Path("official_schedule_day_tabs.json"))
    a=p.parse_args()
    result=audit(json.loads(a.coverage.read_text(encoding="utf-8")))
    a.out.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,indent=2))
if __name__=="__main__":
    main()
