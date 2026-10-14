#!/usr/bin/env python3
"""Select only explicitly dated next-day V3.2 fixtures for the evening shadow run."""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

def next_day_predictions(payload, today):
    target = (today + timedelta(days=1)).isoformat()
    accepted = []
    for p in payload.get("predictions", []):
        # Never infer match date from report date, tournament or filenames.
        if p.get("match_date") != target:
            continue
        if p.get("status") not in ("MODEL_READY", "SCHEDULE_PENDING_NO_BET"):
            continue
        if p.get("fixture_source") == "USER_CAPTURE_UNVERIFIED":
            continue
        accepted.append(p)
    return {"policy":"RESEARCH_ONLY_NO_BET","predictions":accepted,
            "target_date":target,"count":len(accepted)}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--predictions",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    now=datetime.now(ZoneInfo("Europe/Paris"))
    data=next_day_predictions(json.loads(args.predictions.read_text(encoding="utf-8")),now.date())
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(data,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"target_date":data["target_date"],"eligible_fixtures":data["count"]}))
if __name__=="__main__":
    main()
