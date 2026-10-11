#!/usr/bin/env python3
"""Audit actual BWF archive coverage over the forecast horizon; research only."""
import argparse
import gzip
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


def audit(data_dir, now, days):
    end = now + timedelta(days=days)
    counts = Counter()
    candidates = {}
    finished = set()
    for file in sorted((data_dir / "matches").glob("*.json.gz")):
        counts["archives_scanned"] += 1
        with gzip.open(file, "rt", encoding="utf-8") as stream:
            raw = json.load(stream)
        result = raw.get("results") or {}
        records = list((result.get("by_time") or {}).get("time_group") or [])
        for court in (result.get("by_court") or {}).values():
            if isinstance(court, dict):
                records.extend(court.values())
        for match in records:
            if not isinstance(match, dict) or match.get("id") is None:
                continue
            if (match.get("draw_name") or "").split(" ")[0].upper() != "MS":
                continue
            key = file.name.split(".")[0] + ":" + str(match["id"])
            if match.get("winner") in (1, 2):
                finished.add(key)
                candidates.pop(key, None)
                continue
            if key in finished:
                continue
            try:
                start = datetime.fromtimestamp(int(match["start_time"]), timezone.utc)
            except (KeyError, ValueError, TypeError, OverflowError):
                counts["missing_or_invalid_start"] += 1
                continue
            date_only = start.hour == start.minute == start.second == 0
            within = (now.date() <= start.date() <= end.date()) if date_only else (now < start <= end)
            if not within:
                continue
            p1, p2 = match.get("t1p1_detail") or {}, match.get("t2p1_detail") or {}
            if not isinstance(p1, dict) or not isinstance(p2, dict):
                counts["invalid_player_details"] += 1
                continue
            paired = bool(p1.get("id") and p2.get("id"))
            doubles = bool((match.get("t1p2_detail") or {}).get("id") or
                           (match.get("t2p2_detail") or {}).get("id"))
            candidates[key] = {"day": start.date().isoformat(), "paired": paired,
                               "doubles": doubles, "date_only": date_only}
    by_day = {}
    for key, item in sorted(candidates.items()):
        if key in finished:
            continue
        day = by_day.setdefault(item["day"], {"ms_unfinished": 0, "paired_singles": 0,
                                             "missing_pairing": 0, "date_only_paired": 0})
        day["ms_unfinished"] += 1
        if item["paired"] and not item["doubles"]:
            day["paired_singles"] += 1
            if item["date_only"]:
                day["date_only_paired"] += 1
        else:
            day["missing_pairing"] += 1
    return {"policy": "RESEARCH_ONLY_NO_BET", "generated_at_utc": now.isoformat(),
            "horizon_days": days, "archive_counts": dict(counts),
            "days": by_day,
            "total_ms_unfinished": sum(x["ms_unfinished"] for x in by_day.values()),
            "total_paired_singles": sum(x["paired_singles"] for x in by_day.values()),
            "note": "BWF archives only; date-only starts are not verified kickoffs."}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, default=Path("data/bwf"))
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--out", type=Path, default=Path("data/model_v3_2_live/bwf_future_coverage.json"))
    args = p.parse_args()
    if args.days < 1:
        p.error("--days must be positive")
    report = audit(args.data_dir, datetime.now(timezone.utc), args.days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
