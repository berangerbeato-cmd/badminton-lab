#!/usr/bin/env python3
"""Fail-closed validation of BWF refresh -> V3.2 model -> odds comparison order."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def validate(refresh, model, comparison=None):
    items = refresh.get("tournament_refreshes") or []
    if refresh.get("errors") or not items:
        raise ValueError("BWF refresh missing or has errors")
    if any(item.get("status") != "REFRESHED" for item in items):
        raise ValueError("BWF tournament refresh incomplete")
    times = [timestamp(item.get("checked_at_utc")) for item in items]
    model_at = timestamp(model.get("generated_at_utc"))
    if model_at <= max(times):
        raise ValueError("V3.2 model snapshot predates BWF refresh")
    if comparison is not None:
        comparison_at = timestamp(comparison.get("generated_at_utc"))
        if comparison_at <= model_at:
            raise ValueError("Odds comparison predates V3.2 model")
    return {
        "status": "ORDER_VALIDATED",
        "policy": "RESEARCH_ONLY_NO_BET",
        "bwf_tournaments": len(items),
        "latest_bwf_refresh_utc": max(times).isoformat(),
        "model_generated_at_utc": model_at.isoformat(),
        "comparison_generated_at_utc": comparison_at.isoformat() if comparison is not None else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", type=Path, default=Path("data/model_v3_2_live/bwf_refresh_latest.json"))
    parser.add_argument("--model", type=Path, default=Path("data/model_v3_2_live/latest_report.json"))
    parser.add_argument("--comparison", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        refresh = json.loads(args.refresh.read_text(encoding="utf-8"))
        model = json.loads(args.model.read_text(encoding="utf-8"))
        comparison = json.loads(args.comparison.read_text(encoding="utf-8")) if args.comparison else None
        result = validate(refresh, model, comparison)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {"status": "ORDER_INVALID", "policy": "RESEARCH_ONLY_NO_BET", "reason": str(exc)}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ORDER_VALIDATED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
