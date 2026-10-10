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


def classify(source, item):
    """Classify collection blockers without treating inaccessible sites as empty markets."""
    status = item.get("status", "")
    if status in ("MISSING_SNAPSHOT", "INVALID_SNAPSHOT"):
        return "DATA_MISSING_OR_INVALID"
    if status.startswith("SKIPPED_ROBOTS"):
        return "ACCESS_NOT_PERMITTED_OR_UNAVAILABLE"
    if status.startswith("REJECT_HTTP"):
        return "HTTP_OR_REDIRECT_REJECTED"
    if item.get("quote_count", 0):
        return "QUOTE_OBSERVED_REQUIRES_VALIDATION"
    if source == "netbet_quotes":
        rejected = (item.get("index_market_audit") or {}).get("rejections") or {}
        if rejected:
            return "MARKETS_SEEN_BUT_UNVERIFIED"
    if status in ("NO_CONFIDENT_PREMATCH_QUOTES", "NO_CONFIDENT_QUOTES"):
        return "NO_CONFIDENT_QUOTES_EXTRACTED"
    return "UNKNOWN_OR_UNCLASSIFIED"


def next_actions(sources):
    """Prioritize only public-access and fail-closed research actions."""
    actions = []
    netbet = sources.get("netbet_quotes", {})
    if netbet.get("blocker") == "MARKETS_SEEN_BUT_UNVERIFIED":
        actions.append({"source": "netbet_quotes", "priority": 1,
                        "action": "Review public competition heading and player-pair context; do not infer tournament from URL."})
    unibet = sources.get("unibet_fr", {})
    if unibet.get("blocker") == "NO_CONFIDENT_QUOTES_EXTRACTED":
        actions.append({"source": "unibet_fr", "priority": 2,
                        "action": "Inspect public page structure and discipline/date sections; do not interpret live prices as prematch."})
    for source, item in sources.items():
        if item.get("blocker") in ("ACCESS_NOT_PERMITTED_OR_UNAVAILABLE", "HTTP_OR_REDIRECT_REJECTED"):
            actions.append({"source": source, "priority": 3,
                            "action": "Record access limitation; do not bypass robots, authentication or geoblocks."})
    return actions


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
    for source, item in sources.items():
        item["blocker"] = classify(source, item)
    return {
        "next_actions": next_actions(sources),
        "blocker_counts": {kind: sum(x["blocker"] == kind for x in sources.values())
                           for kind in sorted({x["blocker"] for x in sources.values()})},
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
