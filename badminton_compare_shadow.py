#!/usr/bin/env python3
"""Badminton Lab V2: compare archived Unibet/NetBet odds, research-only.

Read-only with respect to all inputs. Writes ONLY JSON/CSV under the requested
comparison output directory. No wager, T0, prediction, or Sheets operation.
Requires only Python 3.12's standard library.
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

VERSION = "badminton_shadow_comparator_v2.1"
POLICY = "SHADOW_ONLY_NO_BET_NO_T0_NO_SHEETS_WRITE"
NO_BET = "NO_BET_SHADOW"
OPERATORS = ("Unibet.fr", "NetBet.fr")
CSV_HEADERS = [
    "generated_at_utc", "bwf_match_id", "bwf_player_id", "player", "model",
    "model_asof_utc", "model_snapshot_sha256", "model_probability",
    "unibet_odds", "unibet_observed_at_utc", "netbet_odds",
    "netbet_observed_at_utc", "best_historical_odds",
    "best_historical_operator", "best_historical_ev_pct",
    "best_within_window_odds", "best_within_window_operator",
    "best_within_window_ev_pct", "observation_gap_seconds",
    "comparison_status", "decision",
]


def utc(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing ISO timestamp")
    out = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if out.tzinfo is None or out.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return out.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalized(value: object) -> str:
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(value.casefold().split())


def price(value: object) -> float:
    v = float(value)
    if not math.isfinite(v) or not (1.01 <= v <= 100):
        raise ValueError("Invalid decimal odds")
    return v


def read_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"Not a JSON object: {path}")
    return raw


def validate_snapshot(report: dict, root: Path) -> tuple[dict | None, str | None]:
    """Verify the referenced historical CSV by content hash, not latest.csv."""
    name = report.get("model_snapshot")
    digest = report.get("model_snapshot_sha256")
    if not isinstance(name, str) or not isinstance(digest, str):
        return None, "MODEL_SNAPSHOT_METADATA_ABSENT"
    allowed = (root / "data/model/snapshots").resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(allowed) or path.suffix.lower() != ".csv":
        return None, "MODEL_SNAPSHOT_PATH_UNSAFE"
    if not path.is_file():
        return None, "MODEL_SNAPSHOT_NOT_FOUND"
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != digest:
        return None, "MODEL_SNAPSHOT_SHA256_MISMATCH"
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    try:
        rows = list(reader)
        ids = [r["bwf_match_id"] for r in rows]
        if not ids or len(ids) != len(set(ids)):
            return None, "MODEL_SNAPSHOT_DUPLICATE_OR_EMPTY"
        return {"hash": sha, "path": name, "rows": {r["bwf_match_id"]: r for r in rows}}, None
    except (csv.Error, KeyError, TypeError):
        return None, "MODEL_SNAPSHOT_CSV_INVALID"


def collect(operator: str, report: dict | None, root: Path, evaluated: datetime,
            age_limit: int) -> tuple[dict, dict]:
    audit = {"operator": operator, "status": "MISSING_SOURCE" if report is None else "REJECTED_SOURCE",
             "observation_utc": None, "rows_read": 0, "rows_validated": 0,
             "rows_current": 0, "rejected": []}
    if report is None:
        return {}, audit
    expected_status = ("ANALYZED_SHADOW" if operator == OPERATORS[0]
                       else "OBSERVED_SHADOW_NEEDS_CONFIRMATION")
    if report.get("operator") != operator or report.get("status") != expected_status:
        audit["rejected"].append("SOURCE_OPERATOR_OR_STATUS_UNEXPECTED")
        return {}, audit
    try:
        source_time = utc(report["source_observed_at_utc"] if operator == OPERATORS[0]
                          else report["observed_at_utc"])
    except (KeyError, ValueError):
        audit["rejected"].append("SOURCE_TIMESTAMP_INVALID")
        return {}, audit
    audit["observation_utc"] = iso(source_time)
    records = report.get("matches") if operator == OPERATORS[0] else report.get("elo_comparisons")
    if not isinstance(records, list):
        audit["rejected"].append("MATCHES_LIST_MISSING")
        return {}, audit
    snapshot, reason = validate_snapshot(report, root)
    if reason:
        audit["rejected"].append(reason)
        return {}, audit
    audit["status"] = "VALIDATED_SOURCE"
    audit["rows_read"] = len(records)
    found: dict[str, dict] = {}
    duplicate_ids: set[str] = set()
    for rec in records:
        mid = str(rec.get("bwf_match_id", "")) if isinstance(rec, dict) else ""
        if mid in found or mid in duplicate_ids:
            duplicate_ids.add(mid)
            found.pop(mid, None)
            audit["rejected"].append(f"{mid}:DUPLICATE_BWF_ID")
            continue
        try:
            if not mid or rec["market"] != "H2H_FULL_MATCH":
                raise ValueError("UNEXPECTED_MATCH_OR_MARKET")
            if rec.get("decision") != NO_BET or rec.get("valid_pre_match_T0") is not False:
                raise ValueError("NOT_SHADOW_NO_BET")
            match = snapshot["rows"][mid]
            if match.get("status") != "DATE_ONLY_START_UNVERIFIED_NO_BET":
                raise ValueError("MODEL_START_STATUS_UNEXPECTED")
            model_time = utc(rec["model_asof_utc"])
            if model_time != utc(match["asof_utc"]) or model_time >= source_time:
                raise ValueError("MODEL_TIME_INCONSISTENT")
            if rec.get("model") and rec["model"] != match["model"]:
                raise ValueError("MODEL_NAME_MISMATCH")
            names = (match["player_a"], match["player_b"])
            ids = (match["player_a_id"], match["player_b_id"])
            if not ids[0] or not ids[1] or ids[0] == ids[1]:
                raise ValueError("MODEL_PLAYER_IDS_INVALID")
            observed_sides = (rec["side_1"], rec["side_2"])
            given = [normalized(s["bwf_name"]) for s in observed_sides]
            reference = [normalized(name) for name in names]
            if len(set(given)) != 2 or set(given) != set(reference):
                raise ValueError("BWF_PLAYER_NAMES_DONT_MATCH")
            pa = float(match["calibrated_p_a"])
            if not math.isfinite(pa) or not 0 < pa < 1:
                raise ValueError("MODEL_PROBABILITY_INVALID")
            p_lookup = {reference[0]: pa, reference[1]: 1 - pa}
            odds = {}
            for side in observed_sides:
                n = normalized(side["bwf_name"])
                odds[n] = price(side["decimal_odds"])
                if abs(float(side["model_probability"]) - p_lookup[n]) > 0.000002:
                    raise ValueError("ODDS_PROBABILITY_DIFFERS_FROM_SNAPSHOT")
            stamp = utc(rec.get("observed_at_utc", iso(source_time)))
            if abs((stamp - source_time).total_seconds()) > 120:
                raise ValueError("SOURCE_AND_MATCH_OBSERVATIONS_DIVERGE")
            age = (evaluated - stamp).total_seconds()
            found[mid] = {
                "bwf_match_id": mid, "operator": operator, "observed_at_utc": iso(stamp),
                "age_seconds": round(age), "current": 0 <= age <= age_limit,
                "bwf_players": [{"id": ids[i], "name": names[i], "p": (pa if i == 0 else 1-pa)}
                                for i in (0, 1)],
                "decimal_odds_by_id": {ids[i]: odds[reference[i]] for i in (0, 1)},
                "model": match["model"], "model_asof_utc": iso(model_time),
                "model_snapshot_sha256": snapshot["hash"],
                "model_snapshot_path": snapshot["path"],
                "quality_flags": rec.get("quality_flags", []),
                "source_url": rec.get("event_url") or report.get("index_url")
                             or "https://www.unibet.fr/paris-badminton",
            }
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            audit["rejected"].append(f"{mid}:ROW_REJECTED:{str(exc)[:100]}")
    audit["rows_validated"] = len(found)
    audit["rows_current"] = sum(v["current"] for v in found.values())
    return found, audit


def compare(unibet: dict | None, netbet: dict | None, root: Path, evaluated_at: datetime,
            max_age_seconds: int = 2700, max_gap_seconds: int = 1200) -> tuple[dict, list[dict]]:
    if max_age_seconds < 0 or max_gap_seconds < 0:
        raise ValueError("Time windows cannot be negative")
    now = evaluated_at.astimezone(timezone.utc)
    left, l_audit = collect(OPERATORS[0], unibet, root, now, max_age_seconds)
    right, r_audit = collect(OPERATORS[1], netbet, root, now, max_age_seconds)
    matches = []
    csv_rows = []
    for mid in sorted(set(left) | set(right)):
        sources = {name: item for name, item in ((OPERATORS[0], left.get(mid)),
                                                 (OPERATORS[1], right.get(mid))) if item}
        first = next(iter(sources.values()))
        both = len(sources) == 2
        same_model = (len({(x["model"], x["model_asof_utc"], x["model_snapshot_sha256"])
                          for x in sources.values()}) == 1)
        same_players = (len({tuple((y["id"], y["name"]) for y in x["bwf_players"])
                             for x in sources.values()}) == 1)
        timestamps = [utc(x["observed_at_utc"]) for x in sources.values()]
        gap = int((max(timestamps) - min(timestamps)).total_seconds()) if both else None
        if not same_model or not same_players:
            state = "INCOMPATIBLE_SNAPSHOTS_NO_MERGE"
        elif not both:
            state = "ONE_BOOKMAKER_ONLY"
        elif not all(x["current"] for x in sources.values()):
            state = "STALE_AT_COMPARISON_TIME"
        elif gap > max_gap_seconds:
            state = "ASYNCHRONOUS_OUTSIDE_WINDOW"
        else:
            state = "WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW"
        summary = {
            "bwf_match_id": mid, "model": first["model"],
            "model_asof_utc": first["model_asof_utc"],
            "model_snapshot_sha256": first["model_snapshot_sha256"],
            "comparison_status": state, "observation_gap_seconds": gap,
            "decision": NO_BET, "valid_pre_match_T0": False,
            "kickoff_verified": False, "executable_price_verified": False,
            "source_details": {k: {"observed_at_utc": v["observed_at_utc"],
                                   "age_seconds": v["age_seconds"], "current": v["current"],
                                   "model_snapshot_sha256": v["model_snapshot_sha256"],
                                   "quality_flags": v["quality_flags"],
                                   "source_url": v["source_url"]} for k, v in sources.items()},
            "players": [],
        }
        for player in first["bwf_players"]:
            pid, name, p = player["id"], player["name"], player["p"]
            # If models or identities conflict, never combine the two prices.
            observed = ({source: item["decimal_odds_by_id"][pid]
                         for source, item in sources.items()}
                        if same_model and same_players else {})
            best = max(observed, key=observed.get) if observed else None
            best_live = best if state == "WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW" else None
            hist = ({"operator": best, "decimal_odds": observed[best],
                     "ev_pct": round(100 * (p * observed[best] - 1), 2),
                     "qualifier": "HISTORICAL_OBSERVED_NOT_PROVEN_SIMULTANEOUS"}
                    if best else None)
            live = ({"operator": best_live, "decimal_odds": observed[best_live],
                     "ev_pct": round(100 * (p * observed[best_live] - 1), 2),
                     "qualifier": "WITHIN_WINDOW_BUT_NOT_VERIFIED_EXECUTABLE"}
                    if best_live else None)
            summary["players"].append({"bwf_player_id": pid, "name": name,
                                        "model_probability": round(p, 8),
                                        "observed_odds": observed,
                                        "best_historical_observation": hist,
                                        "best_within_window": live})
            csv_rows.append({
                "generated_at_utc": iso(now), "bwf_match_id": mid,
                "bwf_player_id": pid, "player": name, "model": first["model"],
                "model_asof_utc": first["model_asof_utc"],
                "model_snapshot_sha256": first["model_snapshot_sha256"],
                "model_probability": round(p, 8),
                "unibet_odds": observed.get(OPERATORS[0], ""),
                "unibet_observed_at_utc": left[mid]["observed_at_utc"] if mid in left else "",
                "netbet_odds": observed.get(OPERATORS[1], ""),
                "netbet_observed_at_utc": right[mid]["observed_at_utc"] if mid in right else "",
                "best_historical_odds": observed[best] if best else "",
                "best_historical_operator": best or "",
                "best_historical_ev_pct": hist["ev_pct"] if hist else "",
                "best_within_window_odds": observed[best_live] if best_live else "",
                "best_within_window_operator": best_live or "",
                "best_within_window_ev_pct": live["ev_pct"] if live else "",
                "observation_gap_seconds": gap if gap is not None else "",
                "comparison_status": state, "decision": NO_BET,
            })
        matches.append(summary)
    report = {
        "schema_version": VERSION, "generated_at_utc": iso(now), "policy": POLICY,
        "notes": ["Cotes publiques observées; pas de preuve d'exécutabilité.",
                  "Temps d'observation différent du temps de mise à jour du bookmaker.",
                  "Un intervalle temporel compatible ne valide pas un T0 prématch.",
                  "Les prix historiques restent comparables à titre de recherche, jamais comme signaux de mise."],
        "max_age_seconds": max_age_seconds, "max_gap_seconds": max_gap_seconds,
        "sources": {OPERATORS[0]: l_audit, OPERATORS[1]: r_audit},
        "match_count": len(matches),
        "within_window_match_count": sum(m["comparison_status"] ==
                                         "WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW" for m in matches),
        "matches": matches,
    }
    return report, csv_rows


def write_outputs(report: dict, rows: list[dict], directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")
    payload = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    stream = io.StringIO(newline="")
    csv_writer = csv.DictWriter(stream, fieldnames=CSV_HEADERS)
    csv_writer.writeheader()
    csv_writer.writerows(rows)
    text = stream.getvalue()
    files = {f"{stamp}.json": payload, f"{stamp}.csv": text}
    for filename, value in files.items():
        dest = directory / filename
        with dest.open("x", encoding="utf-8", newline="") as fh:
            fh.write(value)
    (directory / "latest.json").write_text(payload, encoding="utf-8")
    (directory / "latest.csv").write_text(text, encoding="utf-8", newline="")
    return {"archive_json": str(directory / f"{stamp}.json"),
            "archive_csv": str(directory / f"{stamp}.csv")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--unibet", default="data/odds_shadow/latest_value_shadow.json")
    parser.add_argument("--netbet", default="data/odds_shadow/netbet_quotes/latest.json")
    parser.add_argument("--output", default="data/odds_shadow/comparison")
    parser.add_argument("--max-age", type=int, default=2700,
                        help="Max age of each public observation, in seconds")
    parser.add_argument("--max-gap", type=int, default=1200,
                        help="Max Unibet/NetBet observation gap, in seconds")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    report, rows = compare(read_file(root / args.unibet), read_file(root / args.netbet),
                           root, datetime.now(timezone.utc), args.max_age, args.max_gap)
    destinations = write_outputs(report, rows, root / args.output)
    print(json.dumps({"status": "SHADOW_ONLY", "matches": report["match_count"],
                      "within_window_matches": report["within_window_match_count"],
                      "sources": {k: v["rows_validated"] for k, v in report["sources"].items()},
                      **destinations}, ensure_ascii=False))


if __name__ == "__main__":
    main()
