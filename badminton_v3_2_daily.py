#!/usr/bin/env python3
"""Badminton Lab V3.2 daily prospective forecasts.

Selected model from the multi-year walk-forward:
    Elo + recent score dominance + rest/inactivity

The feature-selection decision is frozen from data/model_v3_2/latest.json.
For live 2026 forecasting:
- Elo K = 64, selected using 2025 only.
- Logistic coefficients are fit on 2019-2025 only.
- 2026 outcomes may update CURRENT Elo/player-history state before today,
  exactly as an online rating system should, but NEVER refit coefficients.
- Same-UTC-day completed results are excluded from today's forecast state.
- BWF midnight start_time values remain calendar-only placeholders and are
  explicitly marked non-actionable.

No bookmaker access, no bets, no Google Sheets writes.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from elo_model import INITIAL_ELO, elo_p, load_matches
from badminton_features_v3 import (
    PlayerObs,
    fit_variant,
    load_stats,
    metrics,
    parse_score,
    predict,
    score_dominance,
    coefficient_map,
)
from badminton_v3_2_walkforward import build_rows_raw

MODEL_NAME = "bwf_v3_2_score_rest_live"
FINAL_K = 64
FIT_START_YEAR = 2019
FIT_END_YEAR = 2025
SELECTED_VARIANT = "elo_plus_score_rest"
SELECTED_FEATURES = [
    "set_margin5_diff",
    "point_share5_diff",
    "rest_days_diff",
    "inactive30_diff",
]

FIELDS = [
    "bwf_match_id", "start_utc", "tournament", "round",
    "player_a_id", "player_a", "player_b_id", "player_b",
    "elo_a_asof", "elo_b_asof", "raw_p_a",
    "set_margin5_diff", "point_share5_diff",
    "rest_days_diff", "inactive30_diff",
    "v3_2_p_a", "fair_odds_a", "fair_odds_b",
    "elo_v1_probability_a",
    "k", "fit_start_year", "fit_end_year",
    "asof_utc", "model", "cold_start", "status", "source",
]


def snapshot_state(matches, k: int, cutoff_day: str):
    """Build rating and player-history state from dates strictly before cutoff."""
    ratings = defaultdict(lambda: INITIAL_ELO)
    seen = Counter()
    histories: dict[str, list[PlayerObs]] = defaultdict(list)

    for day_str, group_iter in groupby(matches, key=lambda m: m.date):
        if day_str >= cutoff_day:
            break
        today = date.fromisoformat(day_str)
        batch = list(group_iter)

        # Freeze all same-day pre-match ratings first.
        pending = []
        for m in batch:
            raw_p = elo_p(ratings[m.p1_id], ratings[m.p2_id])
            sa, sb, pa, pb, _ = parse_score(m.score)
            pending.append((m, raw_p, sa, sb, pa, pb))

        # Apply same-day outcomes only after every same-day forecast is frozen.
        delta = defaultdict(float)
        touched = set()
        for m, raw_p, sa, sb, pa, pb in pending:
            y = 1.0 if m.winner == 1 else 0.0
            shift = k * (y - raw_p)
            delta[m.p1_id] += shift
            delta[m.p2_id] -= shift
            touched.update((m.p1_id, m.p2_id))

            histories[m.p1_id].append(
                PlayerObs(today, y, raw_p, sa, sb, pa, pb)
            )
            histories[m.p2_id].append(
                PlayerObs(today, 1.0-y, 1.0-raw_p, sb, sa, pb, pa)
            )
            seen[m.p1_id] += 1
            seen[m.p2_id] += 1

        for pid in touched:
            ratings[pid] += delta[pid]

    return ratings, seen, histories


def selected_features(a_hist: list[PlayerObs], b_hist: list[PlayerObs],
                      today: date) -> dict[str, float]:
    a_load = load_stats(a_hist, today)
    b_load = load_stats(b_hist, today)
    a_score = score_dominance(a_hist)
    b_score = score_dominance(b_hist)
    return {
        "set_margin5_diff": a_score[0] - b_score[0],
        "point_share5_diff": a_score[1] - b_score[1],
        "rest_days_diff": a_load[3] - b_load[3],
        "inactive30_diff": a_load[4] - b_load[4],
    }


def load_v1_probabilities(model_dir: Path) -> dict[str, float]:
    """Optional witness probabilities from the existing V1 daily feed."""
    path = model_dir / "upcoming_latest.csv"
    if not path.is_file():
        return {}
    out = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                mid = row.get("bwf_match_id", "")
                p = float(row.get("calibrated_p_a", ""))
                if mid and 0 < p < 1:
                    out[mid] = p
    except (ValueError, TypeError, csv.Error):
        return {}
    return out


def fixture_rows(data_dir: Path, ratings, seen, histories, fitted_model: dict,
                 now: datetime, days: int, v1_probs: dict[str, float]) -> list[dict]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    indexes = json.loads((data_dir / "index.json").read_text(encoding="utf-8"))
    tournaments = {
        str(t["id"]): t
        for t in indexes
        if isinstance(t, dict) and t.get("id") is not None
    }

    future = {}
    finished_keys = set()

    for file in sorted((data_dir / "matches").glob("*.json.gz")):
        tid = file.name.split(".")[0]
        with gzip.open(file, "rt", encoding="utf-8") as f:
            raw = json.load(f)

        result = raw.get("results") or {}
        group = (result.get("by_time") or {}).get("time_group") or []
        more = []
        for court in (result.get("by_court") or {}).values():
            if isinstance(court, dict):
                more.extend(court.values())

        for m in list(group) + more:
            if not isinstance(m, dict) or m.get("id") is None:
                continue
            if (m.get("draw_name") or "").split(" ")[0].upper() != "MS":
                continue

            key = f'{tid}:{m["id"]}'
            if m.get("winner") in (1, 2):
                finished_keys.add(key)
                future.pop(key, None)
                continue
            if key in finished_keys:
                continue

            p1 = m.get("t1p1_detail") or {}
            p2 = m.get("t2p1_detail") or {}
            if (
                not isinstance(p1, dict) or not isinstance(p2, dict)
                or not p1.get("id") or not p2.get("id")
            ):
                continue
            if (m.get("t1p2_detail") or {}).get("id") or (m.get("t2p2_detail") or {}).get("id"):
                continue

            try:
                start = datetime.fromtimestamp(int(m["start_time"]), tz=timezone.utc)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue

            date_only = start.hour == 0 and start.minute == 0 and start.second == 0
            if date_only:
                if not (now.date() <= start.date() <= (now + timedelta(days=days)).date()):
                    continue
            elif not (now < start <= now + timedelta(days=days)):
                continue

            a_id, b_id = str(p1["id"]), str(p2["id"])
            if a_id == b_id:
                continue

            raw_p = elo_p(ratings[a_id], ratings[b_id])
            feats = selected_features(histories[a_id], histories[b_id], start.date())
            model_row = {"raw_p_a": raw_p, **feats}
            p_a = predict(model_row, fitted_model)

            future[key] = {
                "bwf_match_id": key,
                "start_utc": start.isoformat(timespec="seconds"),
                "tournament": (tournaments.get(tid) or {}).get("name")
                              or f"BWF tournament {tid}",
                "round": str(m.get("round_name") or ""),
                "player_a_id": a_id,
                "player_a": p1.get("name_display") or "",
                "player_b_id": b_id,
                "player_b": p2.get("name_display") or "",
                "elo_a_asof": round(ratings[a_id], 2),
                "elo_b_asof": round(ratings[b_id], 2),
                "raw_p_a": round(raw_p, 8),
                **{k: round(float(v), 8) for k, v in feats.items()},
                "v3_2_p_a": round(p_a, 8),
                "fair_odds_a": round(1.0 / p_a, 4),
                "fair_odds_b": round(1.0 / (1.0 - p_a), 4),
                "elo_v1_probability_a": (
                    round(v1_probs[key], 8) if key in v1_probs else ""
                ),
                "k": FINAL_K,
                "fit_start_year": FIT_START_YEAR,
                "fit_end_year": FIT_END_YEAR,
                "asof_utc": now.isoformat(timespec="seconds"),
                "model": MODEL_NAME,
                "cold_start": int(seen[a_id] == 0 or seen[b_id] == 0),
                "status": (
                    "DATE_ONLY_START_UNVERIFIED_NO_BET"
                    if date_only else
                    "MODEL_ONLY_NO_VERIFIED_BOOKMAKER_ODDS"
                ),
                "source": (
                    "https://github.com/berangerbeato-cmd/"
                    "badminton-lab/tree/main/data/bwf"
                ),
            }

    return sorted(
        future.values(),
        key=lambda r: (r["start_utc"], r["bwf_match_id"]),
    )


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def run(data_dir: Path, v1_model_dir: Path, selection_dir: Path,
        output_dir: Path, now: datetime, days: int):
    matches, quality = load_matches(data_dir)

    selection_path = selection_dir / "latest.json"
    if not selection_path.is_file():
        raise ValueError("Missing V3.2 walk-forward selection report")
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection.get("recommended_variant_from_walkforward") != SELECTED_VARIANT:
        raise ValueError(
            "V3.2 recommendation changed; audit before publishing live forecasts"
        )
    replay_cfg = selection.get("replay_2026_configuration") or {}
    if int(replay_cfg.get("selected_k_using_2025", -1)) != FINAL_K:
        raise ValueError("Expected final K=64 from 2025 selection")

    # Rebuild the frozen final coefficients from PRE-2026 data only.
    all_rows = build_rows_raw(matches, FINAL_K)
    training = [
        r for r in all_rows
        if FIT_START_YEAR <= r["year"] <= FIT_END_YEAR
    ]
    if len(training) < 1000:
        raise ValueError("Insufficient final-model training sample")
    fitted = fit_variant(training, SELECTED_FEATURES)

    # Retrospective audit only. Never call this archived live performance.
    replay = [r for r in all_rows if r["year"] == 2026]
    replay_probs = [predict(r, fitted) for r in replay]
    replay_metrics = metrics(replay, replay_probs) if replay else {"n": 0}

    cutoff = now.date().isoformat()
    ratings, seen, histories = snapshot_state(matches, FINAL_K, cutoff)
    v1_probs = load_v1_probabilities(v1_model_dir)
    upcoming = fixture_rows(
        data_dir, ratings, seen, histories, fitted, now, days, v1_probs
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "badminton_v3_2_live_v1",
        "generated_at_utc": now.isoformat(),
        "policy": "MODEL_ONLY_NO_BET_NO_T0_REWRITE",
        "model": MODEL_NAME,
        "selected_variant": SELECTED_VARIANT,
        "selected_features": SELECTED_FEATURES,
        "final_k": FINAL_K,
        "coefficient_fit_years": [FIT_START_YEAR, FIT_END_YEAR],
        "coefficient_fit_n": len(training),
        "coefficients_standardized": coefficient_map(fitted),
        "walkforward_2022_2025": (
            selection["variants"][SELECTED_VARIANT]["aggregate_2022_2025"]
        ),
        "retrospective_2026_current_cache": replay_metrics,
        "upcoming_count": len(upcoming),
        "snapshot_policy": (
            "Current player state uses completed results strictly before the "
            "current UTC date; same-day results are excluded."
        ),
        "selection_policy": (
            "Feature set and K were selected before this daily publication. "
            "2026 outcomes never refit coefficients."
        ),
        "trading_policy": (
            "NO BET. Bookmaker prices and verified prematch timing are separate "
            "requirements handled downstream."
        ),
        "data_quality": quality,
    }
    (output_dir / "latest_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(output_dir / "upcoming_latest.csv", upcoming)

    snapshot = output_dir / "snapshots" / f"{now.date().isoformat()}.csv"
    if not snapshot.exists():
        write_csv(snapshot, upcoming)

    print(json.dumps({
        "model": MODEL_NAME,
        "selected_variant": SELECTED_VARIANT,
        "fit_n": len(training),
        "retrospective_2026_n": replay_metrics.get("n"),
        "retrospective_2026_brier": replay_metrics.get("brier"),
        "upcoming": len(upcoming),
        "snapshot": str(snapshot),
    }, ensure_ascii=False))


def self_test():
    from types import SimpleNamespace

    def M(key, d, a, b, winner, score):
        return SimpleNamespace(
            key=key, date=d, tourney="Test", round="R1",
            p1_id=a, p1_name=a, p2_id=b, p2_name=b,
            winner=winner, score=score,
        )

    sample = [
        M("1", "2026-01-01", "A", "B", 1, "21-10 21-10"),
        M("2", "2026-01-01", "A", "C", 2, "21-18 18-21 15-21"),
        M("3", "2026-01-03", "A", "B", 1, "21-19 21-17"),
    ]
    ratings, seen, histories = snapshot_state(sample, 64, "2026-01-03")
    # Same-day outcomes are applied together; both are available on later dates.
    assert seen["A"] == 2 and seen["B"] == 1
    feats = selected_features(histories["A"], histories["B"], date(2026, 1, 3))
    assert feats["rest_days_diff"] == 0.0
    assert feats["set_margin5_diff"] != 0.0
    assert set(feats) == set(SELECTED_FEATURES)
    # A wins once and loses once on the same frozen morning snapshot, so its
    # two Elo changes cancel exactly. B loses and C wins, proving updates ran.
    assert ratings["A"] == INITIAL_ELO
    assert ratings["B"] < INITIAL_ELO
    assert ratings["C"] > INITIAL_ELO
    print("V3.2 daily self-test OK")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=Path("data/bwf"))
    p.add_argument("--v1-model-dir", type=Path, default=Path("data/model"))
    p.add_argument("--selection-dir", type=Path, default=Path("data/model_v3_2"))
    p.add_argument("--output", type=Path, default=Path("data/model_v3_2_live"))
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        self_test()
    else:
        run(
            args.data_dir,
            args.v1_model_dir,
            args.selection_dir,
            args.output,
            datetime.now(timezone.utc),
            args.days,
        )


if __name__ == "__main__":
    main()
