#!/usr/bin/env python3
"""Join V3.2 provisional pair forecasts to BWF archive fixtures by both player IDs.

Never infer a date from a forecast report date or from a bookmaker listing.
"""
import argparse
import csv
import json
from pathlib import Path

def enrich(payload, rows):
    by_pair = {}
    for row in rows:
        ids = (str(row.get("player_a_id","")),str(row.get("player_b_id","")))
        day = str(row.get("start_utc",""))[:10]
        if not all(x.isdigit() for x in ids) or len(day)!=10:
            continue
        if row.get("status") not in ("DATE_ONLY_START_UNVERIFIED_NO_BET","MODEL_ONLY_NO_VERIFIED_BOOKMAKER_ODDS"):
            continue
        key=frozenset(ids)
        by_pair.setdefault(key,set()).add(day)
    out=dict(payload)
    predictions=[]
    for p in payload.get("predictions",[]):
        q=dict(p)
        ids=(str(p.get("player_a_id","")),str(p.get("player_b_id","")))
        dates=by_pair.get(frozenset(ids),set()) if all(x.isdigit() for x in ids) and ids[0]!=ids[1] else set()
        if len(dates)==1:
            q["match_date"]=next(iter(dates))
            q["fixture_source"]="BWF_ARCHIVE_PAIR_DATE"
        predictions.append(q)
    out["predictions"]=predictions
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--predictions",type=Path,required=True)
    ap.add_argument("--bwf-upcoming",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    a=ap.parse_args()
    data=json.loads(a.predictions.read_text(encoding="utf-8"))
    with a.bwf_upcoming.open(encoding="utf-8-sig",newline="") as f:
        result=enrich(data,list(csv.DictReader(f)))
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({"dated":sum(bool(x.get("match_date")) for x in result["predictions"]),"total":len(result["predictions"])}))
if __name__=="__main__": main()
