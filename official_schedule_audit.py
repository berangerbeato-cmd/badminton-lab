#!/usr/bin/env python3
"""Audit whether official BWF draw API exposes trustworthy UTC scheduling data.

Never infer kickoff time from rounds, tournament days or local display strings.
"""
import argparse
import json
from pathlib import Path
from official_draw_api_probe import probe


def audit(coverage):
    rows = []
    for tournament in coverage.get("tournaments", []):
        tid = str(tournament.get("tournament_id") or "")
        if not tid.isdigit():
            continue
        candidates = []
        response = probe(tid, candidates=candidates)
        rows.append({
            "tournament_id": tid,
            "http_status": response.get("http_status"),
            "error": response.get("error"),
            "draw_count": len(response.get("ms_draw_details") or []),
            "pending_paired_without_utc": len(candidates),
            "draws": [
                {"draw_id": d.get("draw_id"),
                 "with_match_time_utc": (d.get("match_audit") or {}).get("with_match_time_utc"),
                 "pending_without_time": (d.get("match_audit") or {}).get("two_identified_pending_without_time"),
                 "match_fields": d.get("match_field_names"),
                 "keys": d.get("keys")}
                for d in response.get("ms_draw_details") or []
            ],
        })
    return {"policy": "RESEARCH_ONLY_NO_BET", "schedule_verified": False,
            "source": "BWF_OFFICIAL_DRAW_API",
            "note": "Diagnostic only: UTC timestamps cannot be assumed from local matchTime or round labels.",
            "tournaments": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    ap.add_argument("--out", type=Path, default=Path("official_schedule_audit.json"))
    args = ap.parse_args()
    result = audit(json.loads(args.coverage.read_text(encoding="utf-8")))
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
