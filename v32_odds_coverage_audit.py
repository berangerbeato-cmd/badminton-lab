#!/usr/bin/env python3
"""Audit bookmaker coverage without inventing prices or relaxing NO BET gates."""
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

SOURCES = ("unibet_fr", "netbet_quotes", "betclic_fr", "betsson_fr",
           "bwin_fr", "fdj_pos_fr", "winamax_fr", "pmu_fr",
           "oddschecker_badminton", "oddspedia_badminton", "oddsportal_badminton")


def audit(model_file, odds_dir):
    with model_file.open(encoding="utf-8-sig", newline="") as f:
        fixtures = list(csv.DictReader(f))
    sources = {}
    for source in SOURCES:
        p = odds_dir / source / "latest.json"
        if not p.exists():
            sources[source] = {"status": "MISSING_SNAPSHOT", "quote_count": 0}
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            quotes = data.get("quotes") or []
            if not isinstance(quotes, list):
                raise ValueError("quotes must be an array")
            sources[source] = {
                "status": data.get("status", "UNKNOWN"),
                "quote_count": len(quotes),
                "observed_at_utc": data.get("observed_at_utc"),
                "parser_notes": data.get("parser_notes"),
                "index_market_audit": data.get("index_market_audit"),
                "reason_counts": data.get("reason_counts"),
            }
        except (OSError, ValueError, TypeError) as exc:
            sources[source] = {"status": "INVALID_SNAPSHOT", "quote_count": 0, "error": str(exc)}
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy": "RESEARCH_ONLY_NO_BET",
        "status": "NO_CONFIRMED_PREMATCH_QUOTES" if not any(v["quote_count"] for v in sources.values()) else "QUOTES_REQUIRE_MATCH_AND_TIME_VALIDATION",
        "model_fixture_count": len(fixtures),
        "model_fixtures": [{"match_id": r.get("bwf_match_id"), "players": [r.get("player_a"), r.get("player_b")], "start_status": r.get("status")} for r in fixtures],
        "source_audit": sources,
        "note": "Quotes observed are not necessarily valid or executable. Zero quotes is not a scraper success claim.",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, default=Path("data/model_v3_2_live/upcoming_latest.csv"))
    ap.add_argument("--odds-dir", type=Path, default=Path("data/odds_shadow"))
    ap.add_argument("--out", type=Path, default=Path("data/odds_shadow/coverage_audit/latest.json"))
    args = ap.parse_args()
    result = audit(args.model, args.odds_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "model_fixture_count": result["model_fixture_count"],
                      "sources_with_quotes": [k for k,v in result["source_audit"].items() if v["quote_count"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
