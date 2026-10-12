#!/usr/bin/env python3
"""Conservative BWF fixture bridge for V3.2 research. Never fabricates kickoff times."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def parse_utc(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else None
    except ValueError:
        return None


def build(payload, now=None):
    now = now or datetime.now(timezone.utc)
    eligible, blocked, schedule_pending = [], [], []
    seen = set()
    for row in payload.get("candidates", []):
        if not isinstance(row, dict):
            continue
        players = row.get("players")
        if not isinstance(players, list) or len(players) != 2:
            continue
        ids = [str(p.get("id") or "") for p in players if isinstance(p, dict)]
        if len(ids) != 2 or not all(x.isdigit() for x in ids) or ids[0] == ids[1]:
            continue
        mid = str(row.get("match_id") or "")
        if not mid.isdigit() or mid in seen:
            continue
        seen.add(mid)
        timestamp = parse_utc(row.get("match_time_utc"))
        verified = row.get("date_verified") is True and row.get("time_verified") is True
        source = row.get("schedule_source")
        valid = bool(timestamp and timestamp.astimezone(timezone.utc) > now and verified and
                     isinstance(source, str) and source.startswith("BWF_OFFICIAL_"))
        record = {
            "match_id": mid, "tournament_id": str(row.get("tournament_id") or ""),
            "draw_id": str(row.get("draw_id") or ""), "round": row.get("round"),
            "players": players, "match_time_utc": timestamp.astimezone(timezone.utc).isoformat() if valid else None,
            "source": row.get("source"), "schedule_source": source if valid else None,
            "status": "READY_FOR_SHADOW_PREDICTION" if valid else "BLOCKED_UNVERIFIED_SCHEDULE",
        }
        if valid:
            eligible.append(record)
        else:
            record["status"] = "SCHEDULE_PENDING"
            record["prediction_eligibility"] = "READY_FOR_SHADOW_PREDICTION"
            record["betting_eligibility"] = "NO_BET"
            schedule_pending.append(record)
    return {"policy": "RESEARCH_ONLY_NO_BET", "eligible_count": len(eligible),
            "prediction_ready_count": len(eligible) + len(schedule_pending),
            "schedule_pending_count": len(schedule_pending), "schedule_pending": schedule_pending,
            "blocked_count": len(blocked), "eligible": eligible, "blocked": blocked,
            "note": "Fixture-only bridge: schedule-pending pairs are prediction-ready for research; no model execution or betting."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("official_ms_fixture_candidates.json"))
    parser.add_argument("--out", type=Path, default=Path("v32_official_fixture_bridge.json"))
    args = parser.parse_args()
    result = build(json.loads(args.input.read_text(encoding="utf-8")))
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("eligible", "blocked")}, indent=2))


if __name__ == "__main__":
    main()
