#!/usr/bin/env python3
"""Verify BWF's publicly embedded vue-tournament-matches endpoint, read-only."""
import argparse
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from official_draw_ajax_evidence import read_html
from official_ms_draw_probe import ms_draw_url
from official_draw_sources import UA

def inspect(coverage):
    results=[]
    for t in coverage.get("tournaments", []):
        tid=str(t.get("tournament_id") or "")
        url=ms_draw_url(t.get("official_tournament_url"))
        if not tid.isdigit() or not url or len(results)>=5: continue
        row={"tournament_id":tid,"endpoint":"https://extranet-lv.bwfbadminton.com/api/vue-tournament-matches","parameters_found":[]}
        try:
            html=read_html(url,2_000_000)
            m=re.search(r"getMatches\(\)\s*\{.{0,1800}?vue-tournament-matches.{0,1800}",html,re.S)
            if m:
                snippet=m.group(0)
                row["javascript_evidence"]=snippet[:2600]
                row["parameters_found"]=sorted(set(re.findall(r"\b(?:tmtId|tmtType|tmtTab|selectedDate|date|drawCode|drawId|isPara)\b",snippet)))
            if tid=="5228":
                # Only test the exact public endpoint with conservative parameters.
                params=urllib.parse.urlencode({"drawCount":1,"searchKey":"","tmtTab":"2026-10-13","tmtId":tid,"tmtType":0,"courtCode":"","eventCode":"MS","hideTeamMatches":"false","isPara":"false"})
                request=urllib.request.Request(row["endpoint"]+"?"+params,headers={"User-Agent":UA,"Accept":"application/json"})
                try:
                    with urllib.request.urlopen(request,timeout=15) as response:
                        body=response.read(400_000)
                        row["http_status"]=response.status
                        data=json.loads(body)
                        row["response_type"]=type(data).__name__
                        row["response_keys"]=list(data)[:20] if isinstance(data,dict) else []
                        row["response_preview"]=json.dumps(data,ensure_ascii=False)[:1200]
                        row["results_count"]=len(data.get("results")) if isinstance(data,dict) and isinstance(data.get("results"),list) else None
                except Exception as exc:
                    row["request_error"]=str(exc)[:300]
        except Exception as exc: row["error"]=str(exc)[:300]
        results.append(row)
    return {"policy":"RESEARCH_ONLY_NO_BET","note":"No fixture date/time is trusted without explicit per-match verification.","tournaments":results}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--coverage",type=Path,default=Path("global_coverage.json"))
    p.add_argument("--out",type=Path,default=Path("official_match_endpoint_probe.json"))
    a=p.parse_args()
    data=inspect(json.loads(a.coverage.read_text(encoding="utf-8")))
    a.out.write_text(json.dumps(data,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(data,indent=2)[:9000])
if __name__=="__main__": main()
