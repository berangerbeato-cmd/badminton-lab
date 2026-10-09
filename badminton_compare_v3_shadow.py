#!/usr/bin/env python3
"""Compare public Unibet/NetBet observations against Badminton V3.2 probabilities.

Research/shadow only. This script deliberately ignores the V1 probabilities
embedded in bookmaker collection reports and reuses only:
- BWF match/player identity
- observed decimal odds
- observation timestamp / source metadata

A V3.2 model snapshot is accepted only if its asof_utc timestamp is strictly
before the bookmaker observation. Old quotes can therefore never be backfilled
with a model generated later.

No bets, no bookmaker actions, no Google Sheets writes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

MODEL_NAME = "bwf_v3_2_score_rest_live"
POLICY = "SHADOW_ONLY_NO_BET_NO_T0_NO_SHEETS_WRITE"
NO_BET = "NO_BET_SHADOW"
OPERATORS = ("Unibet.fr", "NetBet.fr")


def utc(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("missing timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def norm(value: object) -> str:
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().split())


def read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    return obj if isinstance(obj, dict) else None


def valid_price(value: object) -> float:
    x = float(value)
    if not math.isfinite(x) or not (1.01 <= x <= 100):
        raise ValueError("invalid odds")
    return x


def snapshot_for_observation(root: Path, observed: datetime) -> tuple[dict | None, str | None]:
    path = root / "data/model_v3_2_live/snapshots" / f"{observed.date().isoformat()}.csv"
    if not path.is_file():
        return None, "MISSING_SAME_DAY_V3_SNAPSHOT"
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error):
        return None, "V3_SNAPSHOT_INVALID_CSV"
    if not rows:
        return None, "V3_SNAPSHOT_EMPTY"
    ids = [r.get("bwf_match_id", "") for r in rows]
    if any(not x for x in ids) or len(ids) != len(set(ids)):
        return None, "V3_SNAPSHOT_IDS_INVALID"
    by_id = {r["bwf_match_id"]: r for r in rows}
    return {"path": str(path.relative_to(root)), "hash": sha, "rows": by_id}, None


def extract_unibet(report: dict | None) -> tuple[datetime | None, list[dict], list[str]]:
    errors = []
    if not report:
        return None, [], ["UNIBET_SOURCE_MISSING"]
    if report.get("operator") != "Unibet.fr" or report.get("status") != "ANALYZED_SHADOW":
        return None, [], ["UNIBET_SOURCE_STATUS_INVALID"]
    try:
        observed = utc(report.get("source_observed_at_utc") or report.get("generated_at_utc"))
    except ValueError:
        return None, [], ["UNIBET_SOURCE_TIME_INVALID"]
    out = []
    for rec in report.get("matches") or []:
        try:
            if rec.get("market") != "H2H_FULL_MATCH":
                continue
            mid = str(rec["bwf_match_id"])
            sides = []
            for key in ("side_1", "side_2"):
                s = rec[key]
                sides.append({
                    "name": str(s["bwf_name"]),
                    "player_id": str(s.get("bwf_player_id") or ""),
                    "odds": valid_price(s["decimal_odds"]),
                })
            if len({norm(x["name"]) for x in sides}) != 2:
                raise ValueError("duplicate side names")
            out.append({
                "bwf_match_id": mid,
                "operator": "Unibet.fr",
                "observed_at_utc": iso(utc(rec.get("observed_at_utc") or iso(observed))),
                "sides": sides,
                "source_url": "https://www.unibet.fr/paris-badminton",
                "quality_flags": rec.get("quality_flags", []),
            })
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"UNIBET_ROW_REJECTED:{str(exc)[:100]}")
    return observed, out, errors


def extract_netbet(report: dict | None) -> tuple[datetime | None, list[dict], list[str]]:
    errors = []
    if not report:
        return None, [], ["NETBET_SOURCE_MISSING"]
    if report.get("operator") != "NetBet.fr" or report.get("status") != "OBSERVED_SHADOW_NEEDS_CONFIRMATION":
        return None, [], ["NETBET_SOURCE_STATUS_INVALID"]
    try:
        observed = utc(report["observed_at_utc"])
    except (KeyError, ValueError):
        return None, [], ["NETBET_SOURCE_TIME_INVALID"]
    out = []
    # Use only identity + odds from the already-audited fixture matching.
    for rec in report.get("elo_comparisons") or []:
        try:
            if rec.get("market") != "H2H_FULL_MATCH":
                continue
            mid = str(rec["bwf_match_id"])
            sides = []
            for key in ("side_1", "side_2"):
                s = rec[key]
                sides.append({
                    "name": str(s["bwf_name"]),
                    "player_id": "",
                    "odds": valid_price(s["decimal_odds"]),
                })
            out.append({
                "bwf_match_id": mid,
                "operator": "NetBet.fr",
                "observed_at_utc": iso(observed),
                "sides": sides,
                "source_url": rec.get("event_url") or report.get("index_url") or "",
                "quality_flags": rec.get("quality_flags", []),
            })
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"NETBET_ROW_REJECTED:{str(exc)[:100]}")
    return observed, out, errors


def bind_to_v3(root: Path, rows: list[dict], observed: datetime,
               max_age_seconds: int, evaluated: datetime) -> tuple[dict, dict]:
    snap, reason = snapshot_for_observation(root, observed)
    audit = {
        "observation_utc": iso(observed),
        "snapshot_status": reason or "OK",
        "snapshot_path": snap["path"] if snap else None,
        "snapshot_sha256": snap["hash"] if snap else None,
        "rows_read": len(rows),
        "rows_validated": 0,
        "rows_current": 0,
        "rejected": [],
    }
    if not snap:
        return {}, audit

    found = {}
    for rec in rows:
        mid = rec["bwf_match_id"]
        try:
            m = snap["rows"][mid]
            if m.get("model") != MODEL_NAME:
                raise ValueError("V3_MODEL_NAME_MISMATCH")
            if m.get("status") not in (
                "DATE_ONLY_START_UNVERIFIED_NO_BET",
                "MODEL_ONLY_NO_VERIFIED_BOOKMAKER_ODDS",
            ):
                raise ValueError("V3_MODEL_STATUS_INVALID")
            model_time = utc(m["asof_utc"])
            obs_time = utc(rec["observed_at_utc"])
            if model_time >= obs_time:
                raise ValueError("MODEL_NOT_PRIOR_TO_QUOTES")

            names = [m["player_a"], m["player_b"]]
            ids = [m["player_a_id"], m["player_b_id"]]
            refs = [norm(x) for x in names]
            given = [norm(s["name"]) for s in rec["sides"]]
            if set(refs) != set(given):
                raise ValueError("PLAYER_NAMES_DONT_MATCH_V3")

            pa = float(m["v3_2_p_a"])
            if not 0 < pa < 1:
                raise ValueError("INVALID_V3_PROBABILITY")
            probs = {refs[0]: pa, refs[1]: 1-pa}
            id_by_name = {refs[0]: ids[0], refs[1]: ids[1]}
            canonical_by_name = {refs[0]: names[0], refs[1]: names[1]}

            odds_by_id = {}
            for side in rec["sides"]:
                n = norm(side["name"])
                odds_by_id[id_by_name[n]] = side["odds"]

            age = (evaluated - obs_time).total_seconds()
            found[mid] = {
                "bwf_match_id": mid,
                "operator": rec["operator"],
                "observed_at_utc": iso(obs_time),
                "age_seconds": round(age),
                "current": 0 <= age <= max_age_seconds,
                "model_asof_utc": iso(model_time),
                "model_snapshot_sha256": snap["hash"],
                "model_snapshot_path": snap["path"],
                "players": [
                    {"id": ids[0], "name": names[0], "p": pa},
                    {"id": ids[1], "name": names[1], "p": 1-pa},
                ],
                "decimal_odds_by_id": odds_by_id,
                "source_url": rec["source_url"],
                "quality_flags": rec.get("quality_flags", []),
            }
        except (KeyError, TypeError, ValueError) as exc:
            audit["rejected"].append(f"{mid}:{str(exc)[:120]}")
    audit["rows_validated"] = len(found)
    audit["rows_current"] = sum(int(v["current"]) for v in found.values())
    return found, audit


def compare(root: Path, unibet: dict | None, netbet: dict | None,
            evaluated: datetime, max_age: int, max_gap: int) -> dict:
    u_time, u_rows, u_errors = extract_unibet(unibet)
    n_time, n_rows, n_errors = extract_netbet(netbet)

    left, l_audit = ({}, {"rejected": []})
    right, r_audit = ({}, {"rejected": []})
    if u_time:
        left, l_audit = bind_to_v3(root, u_rows, u_time, max_age, evaluated)
    if n_time:
        right, r_audit = bind_to_v3(root, n_rows, n_time, max_age, evaluated)
    l_audit.setdefault("rejected", []).extend(u_errors)
    r_audit.setdefault("rejected", []).extend(n_errors)

    matches = []
    for mid in sorted(set(left) | set(right)):
        sources = {
            name: item
            for name, item in (("Unibet.fr", left.get(mid)), ("NetBet.fr", right.get(mid)))
            if item
        }
        first = next(iter(sources.values()))
        both = len(sources) == 2
        same_model = len({
            (x["model_asof_utc"], x["model_snapshot_sha256"]) for x in sources.values()
        }) == 1
        timestamps = [utc(x["observed_at_utc"]) for x in sources.values()]
        gap = int((max(timestamps)-min(timestamps)).total_seconds()) if both else None

        if not same_model:
            state = "INCOMPATIBLE_V3_SNAPSHOTS"
        elif not both:
            state = "ONE_BOOKMAKER_ONLY"
        elif not all(x["current"] for x in sources.values()):
            state = "STALE_AT_COMPARISON_TIME"
        elif gap is not None and gap > max_gap:
            state = "ASYNCHRONOUS_OUTSIDE_WINDOW"
        else:
            state = "WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW"

        players = []
        for p in first["players"]:
            pid, prob = p["id"], p["p"]
            observed = {
                op: s["decimal_odds_by_id"][pid]
                for op, s in sources.items()
                if pid in s["decimal_odds_by_id"]
            }
            best_op = max(observed, key=observed.get) if observed else None
            best_odds = observed[best_op] if best_op else None
            players.append({
                "bwf_player_id": pid,
                "name": p["name"],
                "v3_2_probability": round(prob, 8),
                "fair_odds": round(1/prob, 4),
                "observed_odds": observed,
                "best_historical_operator": best_op,
                "best_historical_odds": best_odds,
                "best_historical_ev_pct": (
                    round((prob*best_odds-1)*100, 2) if best_odds else None
                ),
                "best_within_window_ev_pct": (
                    round((prob*best_odds-1)*100, 2)
                    if best_odds and state == "WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW"
                    else None
                ),
            })

        matches.append({
            "bwf_match_id": mid,
            "model": MODEL_NAME,
            "model_asof_utc": first["model_asof_utc"],
            "model_snapshot_sha256": first["model_snapshot_sha256"],
            "comparison_status": state,
            "observation_gap_seconds": gap,
            "decision": NO_BET,
            "valid_pre_match_T0": False,
            "kickoff_verified": False,
            "executable_price_verified": False,
            "source_details": {
                op: {
                    "observed_at_utc": s["observed_at_utc"],
                    "age_seconds": s["age_seconds"],
                    "current": s["current"],
                    "source_url": s["source_url"],
                    "quality_flags": s["quality_flags"],
                }
                for op, s in sources.items()
            },
            "players": players,
        })

    return {
        "schema_version": "badminton_v3_2_shadow_compare_v1",
        "generated_at_utc": iso(evaluated),
        "policy": POLICY,
        "model": MODEL_NAME,
        "max_age_seconds": max_age,
        "max_gap_seconds": max_gap,
        "notes": [
            "V3.2 probability snapshot must predate each bookmaker observation.",
            "Public bookmaker observation time is not proof of quote update time.",
            "Kickoff and executable price remain unverified; NO BET.",
        ],
        "sources": {"Unibet.fr": l_audit, "NetBet.fr": r_audit},
        "match_count": len(matches),
        "within_window_match_count": sum(
            m["comparison_status"] == "WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW"
            for m in matches
        ),
        "matches": matches,
    }


def write_report(out: Path, report: dict):
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    stamp = utc(report["generated_at_utc"]).strftime("%Y-%m-%dT%H%M%SZ")
    archive = out / f"{stamp}.json"
    if not archive.exists():
        archive.write_text(payload, encoding="utf-8")
    (out / "latest.json").write_text(payload, encoding="utf-8")


def self_test():
    assert norm("CHOU Tien Chén") == "chou tien chen"
    assert valid_price("1.83") == 1.83
    try:
        valid_price("1.00")
        raise AssertionError("1.00 odds should fail")
    except ValueError:
        pass
    print("V3.2 shadow comparator self-test OK")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("."))
    p.add_argument("--unibet", type=Path,
                   default=Path("data/odds_shadow/latest_value_shadow.json"))
    p.add_argument("--netbet", type=Path,
                   default=Path("data/odds_shadow/netbet_quotes/latest.json"))
    p.add_argument("--output", type=Path,
                   default=Path("data/odds_shadow/comparison_v3"))
    p.add_argument("--max-age", type=int, default=2700)
    p.add_argument("--max-gap", type=int, default=1200)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        self_test()
        return

    root = args.root.resolve()
    report = compare(
        root,
        read_json(root / args.unibet),
        read_json(root / args.netbet),
        datetime.now(timezone.utc),
        args.max_age,
        args.max_gap,
    )
    write_report(root / args.output, report)
    print(json.dumps({
        "status": "SHADOW_ONLY",
        "model": MODEL_NAME,
        "matches": report["match_count"],
        "within_window": report["within_window_match_count"],
        "unibet_valid": report["sources"]["Unibet.fr"].get("rows_validated", 0),
        "netbet_valid": report["sources"]["NetBet.fr"].get("rows_validated", 0),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
