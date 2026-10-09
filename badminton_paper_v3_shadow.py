#!/usr/bin/env python3
"""Badminton Lab V3.2 forward-only fictional paper ledger.

NO REAL MONEY / NO BETTING API / NO GOOGLE SHEETS / NO HISTORICAL BACKFILL.

Consumes only the canonical V3.2 shadow comparison:
    data/odds_shadow/comparison_v3/latest.json

Rules for a fictional candidate:
- comparison must be fresh and WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW
- both Unibet and NetBet observations must be present/current
- frozen V3.2 snapshot hash and timestamp must verify
- V3.2 snapshot must predate both bookmaker observations
- BWF fixture must still have no result
- calendar day must be today
- EV must be >= threshold
- kickoff and executable price remain unverified, so this is NEVER a real bet

Picks and settlements are immutable once written.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

POLICY = "EXPLORATORY_V3_PAPER_ONLY_NO_REAL_BET_NO_VALIDATED_T0_NO_SHEETS"
SCHEMA = "badminton_paper_v3_shadow_v1"
COMPARATOR_SCHEMA = "badminton_v3_2_shadow_compare_v1"
COMPARATOR_POLICY = "SHADOW_ONLY_NO_BET_NO_T0_NO_SHEETS_WRITE"
MODEL_NAME = "bwf_v3_2_score_rest_live"
PARIS = ZoneInfo("Europe/Paris")
MATCH_ID = re.compile(r"^[0-9]+:[0-9]+$")
SET_SCORE = re.compile(r"<span>\s*(\d+)\s*</span>", re.I)

CSV_FIELDS = [
    "pick_id", "selected_at_utc", "bwf_match_id", "bwf_day", "player",
    "bwf_player_id", "operator", "decimal_odds", "model_probability", "ev_pct",
    "stake_eur", "selection_status", "outcome", "settled_at_utc", "pnl_eur",
    "model", "model_asof_utc", "comparison_generated_at_utc",
]


def iso(t: datetime) -> str:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("Naive time forbidden")
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(s: str) -> datetime:
    if not isinstance(s, str) or not s:
        raise ValueError("Time must be ISO 8601")
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("Offset-aware time required")
    return t.astimezone(timezone.utc)


def amount(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def read_json(path: Path):
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, data: dict) -> None:
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, name = tempfile.mkstemp(prefix=".temp_", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(payload)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_immutable(path: Path, data: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as h:
            json.dump(data, h, ensure_ascii=False, indent=2, sort_keys=True)
            h.write("\n")
        return True
    except FileExistsError:
        return False


def fixture(root: Path, match_id: str) -> dict | None:
    if not MATCH_ID.fullmatch(match_id):
        return None
    tid, mid = match_id.split(":")
    archive = root / "data" / "bwf" / "matches" / f"{tid}.json.gz"
    if not archive.is_file():
        return None
    try:
        with gzip.open(archive, "rt", encoding="utf-8") as h:
            raw = json.load(h)
        results = raw.get("results") or {}
        candidates = list((results.get("by_time") or {}).get("time_group") or [])
        for court in (results.get("by_court") or {}).values():
            if isinstance(court, dict):
                candidates.extend(court.values())
        matches = [
            m for m in candidates
            if isinstance(m, dict) and str(m.get("id")) == mid
        ]
        if not matches:
            return None
        m = next((r for r in matches if r.get("winner") in (1, 2)), matches[0])
        if str(m.get("draw_name", "")).strip().split(" ")[0].upper() != "MS":
            return None
        a, b = m.get("t1p1_detail"), m.get("t2p1_detail")
        if not isinstance(a, dict) or not isinstance(b, dict):
            return None
        if (m.get("t1p2_detail") or {}).get("id") or (m.get("t2p2_detail") or {}).get("id"):
            return None
        a_id, b_id = str(a.get("id") or ""), str(b.get("id") or "")
        if not a_id or not b_id or a_id == b_id:
            return None
        return {
            "match_id": match_id,
            "winner": m.get("winner"),
            "p1_id": a_id,
            "p2_id": b_id,
            "raw": m,
            "archive": str(archive),
        }
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        return None


def complete_score(raw: dict) -> bool:
    a = [int(x) for x in SET_SCORE.findall(raw.get("team1Score") or "")]
    b = [int(x) for x in SET_SCORE.findall(raw.get("team2Score") or "")]
    if len(a) not in (2, 3) or len(a) != len(b):
        return False
    wins_a = sum(((x >= 21 and x-y >= 2) or (x == 30 and x > y)) for x, y in zip(a, b))
    wins_b = sum(((y >= 21 and y-x >= 2) or (y == 30 and y > x)) for x, y in zip(a, b))
    winner = raw.get("winner")
    return (
        winner == 1 and wins_a == 2 and wins_b < 2
        or winner == 2 and wins_b == 2 and wins_a < 2
    )


def model_row(root: Path, match: dict) -> dict | None:
    stamp = parse_time(match["model_asof_utc"]).date().isoformat()
    path = root / "data" / "model_v3_2_live" / "snapshots" / f"{stamp}.csv"
    if not path.is_file():
        return None
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != match.get("model_snapshot_sha256"):
        return None
    rows = [
        r for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if r.get("bwf_match_id") == match.get("bwf_match_id")
    ]
    return rows[0] if len(rows) == 1 else None


def proposal(root: Path, match: dict, now: datetime, threshold: Decimal,
             stake: Decimal) -> tuple[dict | None, str]:
    try:
        if match.get("comparison_status") != "WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW":
            return None, "COMPARISON_NOT_FRESH"
        if match.get("valid_pre_match_T0") is not False or match.get("decision") != "NO_BET_SHADOW":
            return None, "UNEXPECTED_SOURCE_DECISION"
        if match.get("kickoff_verified") is not False or match.get("executable_price_verified") is not False:
            return None, "UNEXPECTED_SOURCE_VALIDATION"

        mid = str(match["bwf_match_id"])
        if not MATCH_ID.fullmatch(mid):
            return None, "BAD_MATCH_ID"

        details = match["source_details"]
        if not all(op in details for op in ("Unibet.fr", "NetBet.fr")):
            return None, "TWO_SOURCES_REQUIRED"

        model_asof = parse_time(match["model_asof_utc"])
        for op in ("Unibet.fr", "NetBet.fr"):
            observed = parse_time(details[op]["observed_at_utc"])
            if observed > now or model_asof >= observed or not details[op]["current"]:
                return None, "OBSERVATION_NOT_ADMISSIBLE"

        m = model_row(root, match)
        if not m:
            return None, "MODEL_SNAPSHOT_NOT_VERIFIED"
        if m.get("model") != MODEL_NAME or match.get("model") != MODEL_NAME:
            return None, "MODEL_NAME_MISMATCH"
        if parse_time(m["asof_utc"]) != model_asof:
            return None, "MODEL_TIME_MISMATCH"
        if m.get("status") != "DATE_ONLY_START_UNVERIFIED_NO_BET":
            return None, "UNEXPECTED_MODEL_STATUS"

        start = parse_time(m["start_utc"])
        paris_day = now.astimezone(PARIS).date()
        if start.date() < paris_day:
            return None, "PAST_CALENDAR_DAY"
        if start.date() > paris_day:
            return None, "FUTURE_DAY_SKIPPED_CONSERVATIVELY"

        bwf = fixture(root, mid)
        if not bwf or bwf["p1_id"] != m["player_a_id"] or bwf["p2_id"] != m["player_b_id"]:
            return None, "BWF_FIXTURE_IDENTITY_NOT_VERIFIED"
        if bwf["winner"] in (1, 2):
            return None, "MATCH_ALREADY_HAS_RESULT"

        players = match.get("players") or []
        if len(players) != 2:
            return None, "PLAYER_PAIR_CONFLICT"
        by_id = {str(p["bwf_player_id"]): p for p in players}
        if set(by_id) != {m["player_a_id"], m["player_b_id"]}:
            return None, "PLAYER_PAIR_CONFLICT"

        p_a = Decimal(str(m["v3_2_p_a"]))
        if not Decimal(0) < p_a < Decimal(1):
            return None, "BAD_MODEL_PROBABILITY"

        candidates = []
        for pid, true_p in (
            (m["player_a_id"], p_a),
            (m["player_b_id"], Decimal(1) - p_a),
        ):
            side = by_id[pid]
            if abs(Decimal(str(side["v3_2_probability"])) - true_p) > Decimal("0.000002"):
                return None, "PROBABILITY_CHANGED"

            op = side.get("best_historical_operator")
            odds_raw = side.get("best_historical_odds")
            ev_live = side.get("best_within_window_ev_pct")
            if op not in ("Unibet.fr", "NetBet.fr") or odds_raw is None or ev_live is None:
                continue

            odds = Decimal(str(odds_raw))
            if not odds.is_finite() or not (Decimal("1.01") <= odds <= Decimal("100")):
                return None, "PRICE_INVALID"

            observed = side.get("observed_odds") or {}
            if op not in observed or Decimal(str(observed[op])) != odds:
                return None, "PRICE_NOT_IN_SOURCE"

            ev = (true_p * odds - Decimal(1)) * Decimal(100)
            if abs(ev - Decimal(str(ev_live))) > Decimal("0.055"):
                return None, "EV_RECOMPUTATION_MISMATCH"
            if ev >= threshold:
                candidates.append((ev, side, pid, op, odds, true_p))

        if not candidates:
            return None, "NO_EV_ABOVE_THRESHOLD"

        ev, side, pid, op, odds, prob = max(candidates, key=lambda x: (x[0], x[2]))
        return {
            "schema": SCHEMA,
            "policy": POLICY,
            "pick_id": mid.replace(":", "_"),
            "selected_at_utc": iso(now),
            "bwf_match_id": mid,
            "bwf_day": start.date().isoformat(),
            "bwf_player_id": pid,
            "player": side["name"],
            "operator": op,
            "decimal_odds": str(odds),
            "model_probability": str(prob),
            "ev_pct": amount(ev),
            "threshold_ev_pct": str(threshold),
            "stake_eur": amount(stake),
            "selection_status": "EXPLORATORY_V3_UNVERIFIED_PREMATCH",
            "valid_pre_match_T0": False,
            "kickoff_verified": False,
            "executable_price_verified": False,
            "model": MODEL_NAME,
            "model_asof_utc": match["model_asof_utc"],
            "model_snapshot_sha256": match["model_snapshot_sha256"],
            "comparison_generated_at_utc": None,
            "source_observed_at_utc": {
                op2: details[op2]["observed_at_utc"]
                for op2 in ("Unibet.fr", "NetBet.fr")
            },
            "observation_gap_seconds": match["observation_gap_seconds"],
            "notes": (
                "Virtual V3.2 research candidate only; real kickoff and "
                "executable price are NOT proven."
            ),
        }, "EXPLORATORY_PICK"
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
        return None, "SOURCE_VALIDATION_FAILED"


def select_new(root: Path, report: dict | None, now: datetime, out: Path,
               threshold: Decimal, stake: Decimal, max_report_age: int) -> dict:
    tally = {
        "candidates_created": 0,
        "already_frozen": 0,
        "rejections": {},
        "report_status": None,
    }
    if report is None:
        tally["report_status"] = "COMPARISON_REPORT_MISSING"
        return tally
    if (
        report.get("schema_version") != COMPARATOR_SCHEMA
        or report.get("policy") != COMPARATOR_POLICY
        or report.get("model") != MODEL_NAME
    ):
        tally["report_status"] = "COMPARISON_SCHEMA_POLICY_OR_MODEL_MISMATCH"
        return tally
    try:
        generated = parse_time(report["generated_at_utc"])
    except (ValueError, KeyError):
        tally["report_status"] = "COMPARISON_TIME_INVALID"
        return tally

    age = (now - generated).total_seconds()
    if not 0 <= age <= max_report_age:
        tally["report_status"] = "COMPARISON_REPORT_TOO_OLD_OR_FROM_FUTURE"
        return tally

    tally["report_status"] = "REPORT_FRESH_SHADOW"
    for match in report.get("matches", []):
        if not isinstance(match, dict):
            tally["rejections"]["MALFORMED_ROW"] = tally["rejections"].get("MALFORMED_ROW", 0) + 1
            continue
        candidate, reason = proposal(root, match, now, threshold, stake)
        if candidate:
            candidate["comparison_generated_at_utc"] = iso(generated)
            path = out / "picks" / (candidate["pick_id"] + ".json")
            if write_immutable(path, candidate):
                tally["candidates_created"] += 1
            else:
                tally["already_frozen"] += 1
        else:
            tally["rejections"][reason] = tally["rejections"].get(reason, 0) + 1
    return tally


def settle(root: Path, out: Path, now: datetime) -> dict:
    tally = {"new_settlements": 0, "pending": 0, "unverifiable": 0}
    for path in sorted((out / "picks").glob("*.json")):
        pick = read_json(path)
        if not isinstance(pick, dict) or pick.get("schema") != SCHEMA or pick.get("policy") != POLICY:
            tally["unverifiable"] += 1
            continue
        prior = out / "settlements" / path.name
        if prior.exists():
            continue

        f = fixture(root, str(pick.get("bwf_match_id", "")))
        if not f or str(pick.get("bwf_player_id")) not in (f["p1_id"], f["p2_id"]):
            tally["unverifiable"] += 1
            continue
        if f["winner"] not in (1, 2):
            tally["pending"] += 1
            continue
        if not complete_score(f["raw"]):
            tally["unverifiable"] += 1
            continue

        selected_won = (
            f["winner"] == 1 and str(pick["bwf_player_id"]) == f["p1_id"]
            or f["winner"] == 2 and str(pick["bwf_player_id"]) == f["p2_id"]
        )
        stake = Decimal(pick["stake_eur"])
        odds = Decimal(pick["decimal_odds"])
        pnl = stake * (odds - 1) if selected_won else -stake

        settlement = {
            "schema": SCHEMA,
            "policy": POLICY,
            "pick_id": pick["pick_id"],
            "bwf_match_id": pick["bwf_match_id"],
            "settled_at_utc": iso(now),
            "result_source_archive": f["archive"],
            "outcome": "WIN" if selected_won else "LOSS",
            "winning_bwf_player_id": f["p1_id"] if f["winner"] == 1 else f["p2_id"],
            "score_team1": SET_SCORE.findall(f["raw"].get("team1Score") or ""),
            "score_team2": SET_SCORE.findall(f["raw"].get("team2Score") or ""),
            "pnl_eur": amount(pnl),
            "stake_eur": str(stake),
            "selection_status": "EXPLORATORY_V3_UNVERIFIED_PREMATCH",
            "validated_roi_eligible": False,
        }
        if write_immutable(prior, settlement):
            tally["new_settlements"] += 1
    return tally


def summarize(out: Path, now: datetime) -> dict:
    rows = []
    pnl = Decimal("0")
    settled_stakes = Decimal("0")
    wins = losses = total = 0

    for p in sorted((out / "picks").glob("*.json")):
        pick = read_json(p)
        if not isinstance(pick, dict) or pick.get("policy") != POLICY:
            continue
        total += 1
        s = read_json(out / "settlements" / p.name)
        if s and s.get("policy") == POLICY and s.get("pick_id") == pick.get("pick_id"):
            pnl += Decimal(s["pnl_eur"])
            settled_stakes += Decimal(pick["stake_eur"])
            wins += int(s["outcome"] == "WIN")
            losses += int(s["outcome"] == "LOSS")

        row = {k: pick.get(k, "") for k in CSV_FIELDS}
        row.update({
            "outcome": s["outcome"] if s else "PENDING",
            "settled_at_utc": s["settled_at_utc"] if s else "",
            "pnl_eur": s["pnl_eur"] if s else "",
        })
        rows.append(row)

    roi = (pnl / settled_stakes * 100) if settled_stakes else None
    summary = {
        "schema": SCHEMA,
        "policy": POLICY,
        "model": MODEL_NAME,
        "generated_at_utc": iso(now),
        "exploratory_picks": total,
        "exploratory_settled": wins + losses,
        "exploratory_pending": total - wins - losses,
        "exploratory_wins": wins,
        "exploratory_losses": losses,
        "exploratory_pnl_eur": amount(pnl),
        "exploratory_roi_pct_not_validated": amount(roi) if roi is not None else None,
        "validated_picks": 0,
        "validated_roi_pct": None,
        "disclaimer": (
            "NO REAL BETS. Exploratory V3.2 P&L is not evidence of executable "
            "or profitable wagers."
        ),
    }

    atomic_write(out / "summary" / "latest.json", summary)
    (out / "summary").mkdir(parents=True, exist_ok=True)
    with (out / "summary" / "ledger_latest.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return summary


def self_test():
    assert amount(Decimal("1.005")) == "1.01"
    assert MATCH_ID.fullmatch("5594:1552530")
    assert not MATCH_ID.fullmatch("bad-id")
    print("V3.2 paper self-test OK")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("."))
    p.add_argument(
        "--comparison",
        default="data/odds_shadow/comparison_v3/latest.json",
    )
    p.add_argument(
        "--output",
        default="data/shadow_paper_v3",
    )
    p.add_argument("--stake", type=Decimal, default=Decimal("10"))
    p.add_argument("--min-ev", type=Decimal, default=Decimal("5"))
    p.add_argument("--max-report-age", type=int, default=1200)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        self_test()
        return

    if (
        not args.stake.is_finite()
        or args.stake <= 0
        or args.stake > 100
        or not args.min_ev.is_finite()
        or not 0 <= args.min_ev <= 100
    ):
        p.error("Invalid stake or EV threshold")
    if not 0 <= args.max_report_age <= 3600:
        p.error("Invalid report age")

    root = args.root.resolve()
    out = (root / args.output).resolve()
    allowed = (root / "data" / "shadow_paper_v3").resolve()
    if not out.is_relative_to(allowed):
        p.error("Output must remain under data/shadow_paper_v3")

    comp = (root / args.comparison).resolve()
    canonical = (root / "data/odds_shadow/comparison_v3/latest.json").resolve()
    if comp != canonical:
        p.error("Comparison path must be canonical V3.2 latest report")

    now = datetime.now(timezone.utc)
    selection = select_new(
        root,
        read_json(comp),
        now,
        out,
        args.min_ev,
        args.stake,
        args.max_report_age,
    )
    settlement = settle(root, out, now)
    summary = summarize(out, now)

    print(json.dumps({
        "selection": selection,
        "settlement": settlement,
        "summary": summary,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
