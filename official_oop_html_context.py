#!/usr/bin/env python3
"""Read-only official BWF OOP day-page probe. No guessed match schedules."""
import argparse
import json
import re
import urllib.parse
from pathlib import Path
from official_draw_ajax_evidence import read_html
from official_ms_draw_probe import ms_draw_url

def audit(coverage):
    rows=[]
    for t in coverage.get("tournaments", []):
        tid=str(t.get("tournament_id") or "")
        if not tid.isdigit() or len(rows)>=5:
            continue
        url=ms_draw_url(t.get("official_tournament_url"))
        if not url:
            continue
        entry={"tournament_id":tid,"url":url,"schedule_api_evidence":[]}
        try:
            html=read_html(url,2_000_000)
            patterns=(r"activeTmtTab",r"tmtTab",r"order.?of.?play",r"vue-tournament",r"matchTimeUtc",r"oopText",r"getMatches",r"matchSchedule")
            for pattern in patterns:
                for m in list(re.finditer(pattern,html,re.I))[:12]:
                    entry["schedule_api_evidence"].append({"pattern":pattern,
                        "snippet":re.sub(r"\\s+"," ",html[max(0,m.start()-180):m.end()+260])[:440]})
        except Exception as exc:
            entry["error"]=type(exc).__name__
        rows.append(entry)
    return {"policy":"RESEARCH_ONLY_NO_BET","note":"HTML source context for official schedule API; no match times inferred.","tournaments":rows}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--coverage",type=Path,default=Path("global_coverage.json"))
    p.add_argument("--out",type=Path,default=Path("official_oop_html_context.json"))
    a=p.parse_args()
    result=audit(json.loads(a.coverage.read_text(encoding="utf-8")))
    a.out.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps([{"tournament_id":x["tournament_id"],"evidence":len(x["schedule_api_evidence"])} for x in result["tournaments"]],indent=2))
if __name__=="__main__":
    main()
