#!/usr/bin/env python3
"""Read-only BWF fixture diagnostics for Arctic Open; no model or odds changes."""
import argparse
import gzip
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def audit(data_dir: Path, tournament_id: str, target_day: str) -> dict:
    path = data_dir / "matches" / (tournament_id + ".json.gz")
    if not path.exists():
        return {"status": "TOURNAMENT_ARCHIVE_MISSING", "tournament_id": tournament_id,
                "target_day": target_day, "archive_exists": False}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        raw = json.load(stream)
    results = raw.get("results") or {}
    group = (results.get("by_time") or {}).get("time_group") or []
    court = []
    for c in (results.get("by_court") or {}).values():
        if isinstance(c, dict):
            court.extend(c.values())
    counts = Counter()
    relevant = {}
    for m in list(group) + court:
        if not isinstance(m, dict) or m.get("id") is None:
            continue
        counts["all_records_including_duplicates"] += 1
        if (m.get("draw_name") or "").split(" ")[0].upper() != "MS":
            continue
        counts["ms_records_including_duplicates"] += 1
        p1, p2 = m.get("t1p1_detail") or {}, m.get("t2p1_detail") or {}
        if not isinstance(p1, dict) or not isinstance(p2, dict):
            continue
        names = [str(p1.get("name_display") or ""), str(p2.get("name_display") or "")]
        try:
            start = datetime.fromtimestamp(int(m["start_time"]), tz=timezone.utc)
            day = start.date().isoformat()
        except (KeyError, TypeError, ValueError, OverflowError):
            day = None
        is_target = day == target_day or (
            any("TANAKA" in name.upper() for name in names)
            and any("ANTONSEN" in name.upper() for name in names)
        )
        if not is_target:
            continue
        reasons = []
        if m.get("winner") in (1, 2):
            reasons.append("ALREADY_FINISHED")
        if not p1.get("id") or not p2.get("id"):
            reasons.append("MISSING_PLAYER_ID")
        if (m.get("t1p2_detail") or {}).get("id") or (m.get("t2p2_detail") or {}).get("id"):
            reasons.append("DOUBLES_NOT_MS")
        if day != target_day:
            reasons.append("DIFFERENT_UTC_DATE")
        key = str(m["id"])
        relevant[key] = {
            "bwf_match_id": tournament_id + ":" + key,
            "players": names, "day_utc": day,
            "round": m.get("round_name"), "winner": m.get("winner"),
            "reasons": reasons,
        }
    pair = [m for m in relevant.values() if
            any("TANAKA" in name.upper() for name in m["players"])
            and any("ANTONSEN" in name.upper() for name in m["players"])]
    return {"status": ("PAIR_PRESENT_IN_ARCHIVE" if pair else "PAIR_NOT_IN_ARCHIVE"),
            "tournament_id": tournament_id, "target_day": target_day,
            "archive_exists": True, "record_counts": dict(counts),
            "relevant_ms_fixtures": list(relevant.values()),
            "target_pair_count": len(pair),
            "note": "UTC calendar-day audit; does not establish prematch kickoff or bookmaker validity"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/bwf"))
    ap.add_argument("--tournament-id", default="5594")
    ap.add_argument("--target-day", default="2026-10-11")
    ap.add_argument("--out", type=Path, default=Path("data/model_v3_2_live/bwf_fixture_audit.json"))
    args = ap.parse_args()
    result = audit(args.data_dir, args.tournament_id, args.target_day)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
