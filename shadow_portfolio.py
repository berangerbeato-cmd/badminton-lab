#!/usr/bin/env python3
"""Append-only paper-trading selections and deterministic settlement. Never places bets."""
import argparse
import csv
import json
from datetime import datetime, timezone, date
from pathlib import Path

STAKE_EUR = 10.0
MIN_EV_PCT = 5.0

def selections(rows, recorded_at, match_date, tournament):
    # Without verified kick-off time, only quotes recorded before match day
    # qualify for the main portfolio. Same-day quotes remain observations.
    day = date.fromisoformat(match_date)
    recorded = datetime.fromisoformat(recorded_at.replace('Z', '+00:00'))
    if recorded.tzinfo is None or recorded.astimezone(timezone.utc).date() >= day:
        return []
    output = []
    for row in rows:
        if row.get("status") != "VALUE":
            continue
        try:
            observed = datetime.fromisoformat(row["observed_at_utc"].replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.astimezone(timezone.utc).date() >= day:
                continue
        except (KeyError, ValueError, AttributeError):
            continue
        for side in ("a", "b"):
            ev = float(row["ev_" + side + "_pct"])
            if ev <= MIN_EV_PCT:
                continue
            odds = float(row["odds_" + side])
            player = row["player_" + side]
            opponent = row["player_" + ("b" if side == "a" else "a")]
            output.append({
                "selection_id": f"{tournament}:{match_date}:{row['match_id']}:{player}",
                "tournament": tournament, "match_date": match_date,
                "match_id": str(row["match_id"]), "player": player,
                "opponent": opponent, "probability": float(row["p_" + side]),
                "odds": odds, "ev_pct": ev, "stake_eur": STAKE_EUR,
                "operator": row.get("operator", ""),
                "odds_observed_at_utc": row.get("observed_at_utc", ""),
                "recorded_at_utc": recorded_at,
                "status": "OPEN", "net_eur": None,
                "source": "RESEARCH_SHADOW_ONLY"
            })
    return output

def append_once(ledger, additions):
    existing = {x["selection_id"] for x in ledger}
    for item in additions:
        if item["selection_id"] not in existing:
            ledger.append(item)
            existing.add(item["selection_id"])
    return ledger

def settle(ledger, results):
    # Results must be externally verified; missing/ambiguous results stay OPEN.
    for item in ledger:
        if item["status"] != "OPEN":
            continue
        result = results.get(item["selection_id"])
        if not isinstance(result, dict) or result.get("verified") is not True:
            continue
        winner = result.get("winner")
        if winner not in (item["player"], item["opponent"]):
            continue
        item["status"] = "WON" if winner == item["player"] else "LOST"
        item["net_eur"] = round(item["stake_eur"] * (item["odds"] - 1), 2) if item["status"] == "WON" else -item["stake_eur"]
        item["result_source"] = result.get("source", "")
    return ledger

def summary(ledger):
    done = [x for x in ledger if x["status"] in ("WON", "LOST")]
    invested = sum(x["stake_eur"] for x in done)
    net = round(sum(x["net_eur"] for x in done), 2)
    return {"selections": len(ledger), "open": sum(x["status"] == "OPEN" for x in ledger),
            "settled": len(done), "wins": sum(x["status"] == "WON" for x in done),
            "stake_settled_eur": invested, "net_eur": net,
            "roi_pct": round(100 * net / invested, 2) if invested else None}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shortlist", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--match-date", required=True)
    parser.add_argument("--tournament", required=True)
    parser.add_argument("--verified-results", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.shortlist.read_text(encoding="utf-8"))
    ledger = json.loads(args.ledger.read_text(encoding="utf-8")) if args.ledger.exists() else []
    new = selections(payload["rows"], datetime.now(timezone.utc).isoformat(),
                     args.match_date, args.tournament)
    append_once(ledger, new)
    if args.verified_results and args.verified_results.exists():
        settle(ledger, json.loads(args.verified_results.read_text(encoding="utf-8")))
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    args.ledger.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary(ledger)))

if __name__ == "__main__":
    main()
