#!/usr/bin/env python3
"""Resolve manually observed MS pairings against historical BWF player IDs.
Research-only: observations are not official schedules or bookmaker validation.
"""
import argparse
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from elo_model import load_matches


def normalize(value):
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(sorted(re.findall(r"[a-z0-9]+", value.casefold())))


def build(matches, observations):
    aliases = defaultdict(set)
    labels = {}
    for match in matches:
        for pid, name in ((match.p1_id, match.p1_name), (match.p2_id, match.p2_name)):
            aliases[normalize(name)].add(str(pid))
            labels[str(pid)] = name
    rows, blocked = [], []
    for index, item in enumerate(observations["fixtures"], 1):
        players = []
        for name in item["players"]:
            ids = aliases.get(normalize(name), set())
            pinned = (item.get("player_ids") or {}).get(name)
            if pinned is not None:
                pinned = str(pinned)
                if pinned not in ids:
                    blocked.append({"players": item["players"], "name": name,
                                    "reason": "PINNED_ID_NOT_IN_HISTORY"})
                    break
                ids = {pinned}
            if len(ids) != 1:
                blocked.append({"players": item["players"], "name": name,
                                "reason": "PLAYER_ID_NOT_UNIQUE" if ids else "PLAYER_NOT_FOUND"})
                break
            pid = next(iter(ids))
            players.append({"id": pid, "name": labels[pid]})
        if len(players) != 2:
            continue
        if players[0]["id"] == players[1]["id"]:
            blocked.append({"players": item["players"], "reason": "SAME_PLAYER_ID"})
            continue
        rows.append({"match_id": str(index), "tournament_id": "5210",
                     "players": players, "source": "USER_CAPTURE_UNVERIFIED",
                     "match_time_utc": None, "status": "SCHEDULE_PENDING"})
    return {"policy": "RESEARCH_ONLY_NO_BET", "schedule_pending": rows,
            "blocked": blocked, "matched_count": len(rows), "blocked_count": len(blocked),
            "source_note": "Manual user-observed pairings; no official fixture/time verification"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, default=Path("data/bwf"))
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    matches, _ = load_matches(args.data_dir)
    observations = json.loads(args.fixtures.read_text(encoding="utf-8"))
    result = build(matches, observations)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"matched": result["matched_count"], "blocked": result["blocked"]}))


if __name__ == "__main__":
    main()
