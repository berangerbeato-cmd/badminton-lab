#!/usr/bin/env python3
"""Worldwide BWF tournament fixture coverage inventory, offline and read-only.

Uses the existing BWF index and archived match records. Does not assume that an
empty match endpoint means a draw is unpublished. No network or model changes.
"""
import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bwf_sync import extract_matches, is_elite, read_json


def parse_date(value):
    """Accept explicit ISO dates or timestamps; never infer dates from names."""
    if isinstance(value, (int, float)):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def tournament_window(t):
    """Normalize common BWF index date fields. Unknown or contradictory -> exclude."""
    starts = [parse_date(t.get(k)) for k in ("start_date", "startDate", "date_from", "start")]
    ends = [parse_date(t.get(k)) for k in ("end_date", "endDate", "date_to", "end")]
    starts = {d for d in starts if d is not None}
    ends = {d for d in ends if d is not None}
    if len(starts) != 1 or len(ends) != 1:
        return None
    start, end = next(iter(starts)), next(iter(ends))
    return (start, end) if start <= end else None


def build(data_dir, now, days):
    horizon = now + timedelta(days=days)
    tournaments = read_json(data_dir / "index.json") or []
    output = []
    excluded_unknown_dates = 0
    for t in tournaments:
        if not isinstance(t, dict) or not is_elite(t) or t.get("id") is None:
            continue
        window = tournament_window(t)
        if window is None:
            excluded_unknown_dates += 1
            continue
        if window[1] < now.date() or window[0] > horizon.date():
            continue
        tid = str(t["id"])
        # BWF index date fields vary across responses. Never guess missing dates.
        dates = {k: t.get(k) for k in ("start_date", "end_date", "startDate", "endDate", "date_from", "date_to")}
        archive = data_dir / "matches" / (tid + ".json.gz")
        matches = extract_matches(read_json(archive)) if archive.exists() else {}
        pending, paired, unpaired, completed = 0, 0, 0, 0
        for m in matches.values():
            if (str(m.get("draw_name") or "").split(" ")[0].upper() != "MS"):
                continue
            if m.get("winner") in (1, 2):
                completed += 1
                continue
            try:
                start = datetime.fromtimestamp(int(m["start_time"]), timezone.utc)
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            if not (now.date() <= start.date() <= horizon.date()):
                continue
            pending += 1
            p1, p2 = m.get("t1p1_detail") or {}, m.get("t2p1_detail") or {}
            if p1.get("id") and p2.get("id") and not (m.get("t1p2_detail") or {}).get("id") and not (m.get("t2p2_detail") or {}).get("id"):
                paired += 1
            else:
                unpaired += 1
        output.append({"tournament_id": tid, "name": t.get("name"), "category": t.get("category"),
                       "index_dates_raw": dates, "start_date": window[0].isoformat(), "end_date": window[1].isoformat(), "archive_exists": archive.exists(),
                       "ms_future_paired": paired, "ms_future_unpaired": unpaired,
                       "ms_future_total": pending, "ms_completed_in_archive": completed,
                       "draw_status": "UNKNOWN" if not paired else "BWF_PAIRINGS_PRESENT",
                       "needs_secondary_source": paired == 0})
    output.sort(key=lambda t: (t["start_date"], str(t["name"])))
    counts = Counter({"excluded_unknown_dates": excluded_unknown_dates})
    for t in output:
        counts["elite_tournaments_in_index"] += 1
        counts["tournaments_needing_secondary_source"] += t["needs_secondary_source"]
        counts["future_ms_paired"] += t["ms_future_paired"]
    return {"policy": "RESEARCH_ONLY_NO_BET", "generated_at_utc": now.isoformat(),
            "horizon_days": days, "scope": "ELITE_TOURNAMENTS_WITH_CONFIRMED_INDEX_DATES_OVERLAPPING_HORIZON",
            "warning": "Tournaments with unknown or conflicting dates are excluded, not assumed absent. Draw availability is not independently verified.",
            "counts": dict(counts), "tournaments": output}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, default=Path("data/bwf"))
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--out", type=Path, default=Path("data/fixture_sources/global_coverage.json"))
    args = p.parse_args()
    if not 1 <= args.days <= 31:
        p.error("--days must be 1..31")
    report = build(args.data_dir, datetime.now(timezone.utc), args.days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], indent=2))


if __name__ == "__main__":
    main()
