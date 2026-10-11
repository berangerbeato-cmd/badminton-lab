#!/usr/bin/env python3
"""One-click research shortlist. No bet execution, no invented odds."""
import argparse
import csv
import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def normalize(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())


def build(predictions, quotes, now):
    if predictions.get("policy") != "RESEARCH_ONLY_NO_BET":
        raise ValueError("Unexpected model policy")
    rows = []
    used = set()
    for p in predictions.get("predictions", []):
        if p.get("status") != "SCHEDULE_PENDING_NO_BET":
            continue
        mid = str(p.get("match_id", ""))
        if not mid.isdigit() or mid in used:
            continue
        used.add(mid)
        pa = float(p["v3_2_p_a"])
        if not 0 < pa < 1:
            continue
        row = {"match_id": mid, "player_a": p["player_a"], "player_b": p["player_b"],
               "p_a": round(pa, 6), "p_b": round(1-pa, 6),
               "quality": "PROVISIONAL_AS_OF_DATE", "status": "NO_VERIFIED_ODDS",
               "operator": "", "observed_at_utc": "", "odds_a": "", "odds_b": "",
               "ev_a_pct": "", "ev_b_pct": "", "best_ev_pct": "",
               "decision": "RESEARCH_ONLY_NO_BET"}
        candidates = []
        for q in quotes:
            if str(q.get("match_id", "")) != mid:
                continue
            if q.get("market") != "H2H_FULL_MATCH" or q.get("verified_pair") is not True:
                continue
            if normalize(q.get("player_a")) != normalize(p["player_a"]) or normalize(q.get("player_b")) != normalize(p["player_b"]):
                continue
            try:
                observed = datetime.fromisoformat(q["observed_at_utc"].replace("Z", "+00:00"))
                if observed.tzinfo is None or not 0 <= (now-observed.astimezone(timezone.utc)).total_seconds() <= 1200:
                    continue
                a, bb = float(q["odds_a"]), float(q["odds_b"])
                if not (1.01 <= a <= 99 and 1.01 <= bb <= 99):
                    continue
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            candidates.append((max(100*(pa*a-1), 100*((1-pa)*bb-1)), q, a, bb))
        if candidates:
            best, q, a, bb = max(candidates, key=lambda x: x[0])
            row.update({"status": "INDICATIVE_EV_REVIEW_ONLY", "operator": q.get("operator", ""),
                        "observed_at_utc": q["observed_at_utc"], "odds_a": a, "odds_b": bb,
                        "ev_a_pct": round(100*(pa*a-1), 2),
                        "ev_b_pct": round(100*((1-pa)*bb-1), 2),
                        "best_ev_pct": round(best, 2)})
        rows.append(row)
    rows.sort(key=lambda x: (x["status"] != "INDICATIVE_EV_REVIEW_ONLY", -(x["best_ev_pct"] if isinstance(x["best_ev_pct"], float) else -999), x["match_id"]))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--quotes", type=Path)
    parser.add_argument("--out", type=Path, default=Path("research_shortlist"))
    args = parser.parse_args()
    model = json.loads(args.predictions.read_text(encoding="utf-8"))
    quotes = json.loads(args.quotes.read_text(encoding="utf-8")) if args.quotes and args.quotes.exists() else []
    if not isinstance(quotes, list):
        raise ValueError("Quotes must be a list")
    rows = build(model, quotes, datetime.now(timezone.utc))
    args.out.mkdir(parents=True, exist_ok=True)
    fields = ["match_id","player_a","player_b","p_a","p_b","quality","status","operator","observed_at_utc","odds_a","odds_b","ev_a_pct","ev_b_pct","best_ev_pct","decision"]
    with (args.out / "shortlist.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (args.out / "shortlist.json").write_text(json.dumps({"policy":"RESEARCH_ONLY_NO_BET","rows":rows}, indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"matches":len(rows),"with_matched_recent_odds":sum(x["status"]=="INDICATIVE_EV_REVIEW_ONLY" for x in rows),"without_odds":sum(x["status"]=="NO_VERIFIED_ODDS" for x in rows),"policy":"RESEARCH_ONLY_NO_BET"}))


if __name__ == "__main__":
    main()
