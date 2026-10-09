#!/usr/bin/env python3
"""Badminton Lab V3.2: strict multi-year walk-forward feature selection.

Purpose
-------
Test the V3.1 feature combinations across several historical years without
letting the tested year influence the model fitted for that year.

For each test year:
- K is selected only from the immediately preceding year's RAW Elo Brier score.
- Logistic coefficients are fitted only on years strictly before the test year.
- All pre-match features use results strictly before the match UTC date.
- Same-day results are applied only after all forecasts/features for that day
  have been frozen.

Tests:
2022, 2023, 2024, 2025 = historical walk-forward robustness folds.
2026 = retrospective replay only, never represented as archived live T0.

No bookmaker odds, no betting, no Google Sheets writes.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from elo_model import INITIAL_ELO, K_CANDIDATES, elo_p, load_matches
from badminton_features_v3 import (
    PlayerObs,
    H2HObs,
    FEATURE_NAMES,
    FAMILIES,
    parse_score,
    diff_features,
    fit_variant,
    predict,
    metrics,
    coefficient_map,
)

MODEL_NAME = "bwf_v3_2_walkforward"
FIT_START_YEAR = 2019
TEST_YEARS = (2022, 2023, 2024, 2025)
REPLAY_YEAR = 2026

SCORE = FAMILIES["score_dominance"]
REST = FAMILIES["rest_inactivity"]
H2H = FAMILIES["h2h"]

VARIANTS = {
    "elo_baseline": [],
    "elo_plus_score": SCORE,
    "elo_plus_score_rest": SCORE + REST,
    "elo_plus_score_h2h": SCORE + H2H,
    "elo_plus_score_rest_h2h": SCORE + REST + H2H,
    "elo_v3_1_all": FEATURE_NAMES,
}


def build_rows_raw(matches, k: int) -> list[dict]:
    """Date-safe rows using RAW Elo as the expected value inside residual features."""
    rating = defaultdict(lambda: INITIAL_ELO)
    histories: dict[str, list[PlayerObs]] = defaultdict(list)
    rating_history: dict[str, list[tuple[date, float]]] = defaultdict(list)
    h2h_hist: dict[tuple[str, str], list[H2HObs]] = defaultdict(list)

    out: list[dict] = []
    pos = 0
    while pos < len(matches):
        start = pos
        day_str = matches[pos].date
        today = date.fromisoformat(day_str)
        while pos < len(matches) and matches[pos].date == day_str:
            pos += 1
        batch = matches[start:pos]

        pending = []
        for m in batch:
            ra, rb = rating[m.p1_id], rating[m.p2_id]
            raw_p = elo_p(ra, rb)
            pair = tuple(sorted((m.p1_id, m.p2_id)))

            features = diff_features(
                histories[m.p1_id],
                histories[m.p2_id],
                rating_history[m.p1_id],
                rating_history[m.p2_id],
                h2h_hist[pair],
                today,
                m.p1_id,
                ra,
                rb,
            )

            sa, sb, pa, pb, _ = parse_score(m.score)
            out.append({
                "match_id": m.key,
                "date": m.date,
                "year": int(m.date[:4]),
                "tournament": m.tourney,
                "round": m.round,
                "player_a_id": m.p1_id,
                "player_a": m.p1_name,
                "player_b_id": m.p2_id,
                "player_b": m.p2_name,
                "rating_a_pre": ra,
                "rating_b_pre": rb,
                "raw_p_a": raw_p,
                "winner": m.winner,
                "score": m.score,
                **features,
            })
            pending.append((m, raw_p, sa, sb, pa, pb))

        # Critical date-safety rule: update only AFTER every forecast on that day.
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
            pair = tuple(sorted((m.p1_id, m.p2_id)))
            h2h_hist[pair].append(
                H2HObs(today, m.p1_id, m.p2_id, m.winner, raw_p)
            )

        for pid in touched:
            rating[pid] += delta[pid]
            rating_history[pid].append((today, rating[pid]))

    return out


def raw_brier(rows: list[dict], year: int) -> float:
    selected = [r for r in rows if r["year"] == year]
    if not selected:
        return math.inf
    return sum(
        (float(r["raw_p_a"]) - (1.0 if r["winner"] == 1 else 0.0)) ** 2
        for r in selected
    ) / len(selected)


def select_k(row_cache: dict[int, list[dict]], validation_year: int) -> tuple[int, dict]:
    scores = {}
    for k in K_CANDIDATES:
        b = raw_brier(row_cache[int(k)], validation_year)
        scores[str(k)] = round(b, 6) if math.isfinite(b) else None
    valid = [(float(v), int(k)) for k, v in scores.items() if v is not None]
    if not valid:
        raise ValueError(f"No validation data for K selection in {validation_year}")
    # Deterministic tie-break: smaller K.
    chosen = min(valid, key=lambda x: (x[0], x[1]))[1]
    return chosen, scores


def combined_metrics(rows: list[dict], probs: list[float]) -> dict:
    return metrics(rows, probs)


def run(data_dir: Path, output_dir: Path) -> dict:
    matches, quality = load_matches(data_dir)

    # Build each K once. This also makes the K search auditable.
    row_cache = {int(k): build_rows_raw(matches, int(k)) for k in K_CANDIDATES}

    report = {
        "schema_version": "badminton_v3_2_walkforward_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": "MODEL_RESEARCH_ONLY_NO_ODDS_NO_BET_NO_T0_REWRITE",
        "model": MODEL_NAME,
        "fit_start_year": FIT_START_YEAR,
        "test_years": list(TEST_YEARS),
        "replay_year": REPLAY_YEAR,
        "variants": {name: {"features": feats, "folds": {}} for name, feats in VARIANTS.items()},
        "fold_configuration": {},
        "source_quality": quality,
        "methodology": [
            "All pre-match features use only results strictly before the UTC match date.",
            "Same-day results are invisible to all forecasts/features generated for that same day.",
            "For test year Y, K is selected using only raw Elo Brier in Y-1.",
            "For test year Y, model coefficients use only rows from FIT_START_YEAR through Y-1.",
            "2022-2025 are walk-forward robustness folds; 2026 is retrospective replay only.",
            "No bookmaker odds, ROI, P&L, or market outcomes are used for model selection.",
        ],
        "selection_rule": (
            "Candidate must beat Elo baseline on aggregate Brier and aggregate log loss, "
            "and improve Brier in at least 3 of 4 walk-forward years. "
            "Among eligible candidates choose lowest aggregate Brier; ties within 0.00025 "
            "prefer fewer extra features."
        ),
    }

    aggregate_rows = {name: [] for name in VARIANTS}
    aggregate_probs = {name: [] for name in VARIANTS}

    for test_year in TEST_YEARS:
        validation_year = test_year - 1
        chosen_k, k_scores = select_k(row_cache, validation_year)
        rows = row_cache[chosen_k]

        train = [
            r for r in rows
            if FIT_START_YEAR <= r["year"] < test_year
        ]
        test = [r for r in rows if r["year"] == test_year]
        if len(train) < 500 or len(test) < 100:
            raise ValueError(
                f"Insufficient walk-forward sample for {test_year}: "
                f"train={len(train)}, test={len(test)}"
            )

        report["fold_configuration"][str(test_year)] = {
            "validation_year_for_k": validation_year,
            "selected_k": chosen_k,
            "raw_brier_by_k_on_validation_year": k_scores,
            "train_years": [FIT_START_YEAR, test_year - 1],
            "train_n": len(train),
            "test_n": len(test),
        }

        for name, features in VARIANTS.items():
            model = fit_variant(train, features)
            probs = [predict(r, model) for r in test]
            fold_metrics = metrics(test, probs)
            report["variants"][name]["folds"][str(test_year)] = {
                **fold_metrics,
                "coefficients_standardized": coefficient_map(model),
            }
            aggregate_rows[name].extend(test)
            aggregate_probs[name].extend(probs)

    baseline_name = "elo_baseline"
    baseline_agg = combined_metrics(
        aggregate_rows[baseline_name], aggregate_probs[baseline_name]
    )

    for name in VARIANTS:
        agg = combined_metrics(aggregate_rows[name], aggregate_probs[name])
        wins = 0
        deltas = {}
        for y in TEST_YEARS:
            b = report["variants"][name]["folds"][str(y)]["brier"]
            base = report["variants"][baseline_name]["folds"][str(y)]["brier"]
            delta = round(b - base, 6)
            deltas[str(y)] = delta
            if delta < 0:
                wins += 1
        agg["brier_delta_vs_baseline"] = round(
            agg["brier"] - baseline_agg["brier"], 6
        )
        agg["log_loss_delta_vs_baseline"] = round(
            agg["log_loss"] - baseline_agg["log_loss"], 6
        )
        agg["years_better_brier"] = wins
        agg["yearly_brier_delta_vs_baseline"] = deltas
        report["variants"][name]["aggregate_2022_2025"] = agg

    # Predeclared eligibility rule. Baseline remains fallback.
    eligible = []
    for name, features in VARIANTS.items():
        if name == baseline_name:
            continue
        a = report["variants"][name]["aggregate_2022_2025"]
        if (
            a["brier_delta_vs_baseline"] < 0
            and a["log_loss_delta_vs_baseline"] < 0
            and a["years_better_brier"] >= 3
        ):
            eligible.append(name)

    if eligible:
        eligible.sort(key=lambda n: (
            report["variants"][n]["aggregate_2022_2025"]["brier"],
            len(VARIANTS[n]),
            n,
        ))
        best = eligible[0]
        # Simplicity tie-break within 0.00025 aggregate Brier.
        best_brier = report["variants"][best]["aggregate_2022_2025"]["brier"]
        near = [
            n for n in eligible
            if report["variants"][n]["aggregate_2022_2025"]["brier"] <= best_brier + 0.00025
        ]
        best = min(near, key=lambda n: (len(VARIANTS[n]), report["variants"][n]["aggregate_2022_2025"]["brier"], n))
    else:
        best = baseline_name

    report["recommended_variant_from_walkforward"] = best

    # 2026 retrospective replay: train through 2025 and select K on 2025 only.
    replay_k, replay_k_scores = select_k(row_cache, 2025)
    replay_rows = row_cache[replay_k]
    replay_train = [
        r for r in replay_rows
        if FIT_START_YEAR <= r["year"] <= 2025
    ]
    replay_test = [r for r in replay_rows if r["year"] == REPLAY_YEAR]
    report["replay_2026_configuration"] = {
        "selected_k_using_2025": replay_k,
        "raw_brier_by_k_on_2025": replay_k_scores,
        "train_years": [FIT_START_YEAR, 2025],
        "train_n": len(replay_train),
        "test_n": len(replay_test),
        "status": "RETROSPECTIVE_ONLY_NOT_ARCHIVED_LIVE_T0",
    }
    for name, features in VARIANTS.items():
        model = fit_variant(replay_train, features)
        probs = [predict(r, model) for r in replay_test]
        report["variants"][name]["replay_2026"] = metrics(replay_test, probs)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "latest.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Small human-readable summary committed with the JSON.
    lines = [
        "# Badminton V3.2 walk-forward summary",
        "",
        f"Generated: {report['generated_at_utc']}",
        "",
        "| Variant | Brier 2022-25 | Δ Brier | Log loss | Better years |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in VARIANTS:
        a = report["variants"][name]["aggregate_2022_2025"]
        lines.append(
            f"| {name} | {a['brier']:.6f} | {a['brier_delta_vs_baseline']:+.6f} | "
            f"{a['log_loss']:.6f} | {a['years_better_brier']}/4 |"
        )
    lines += [
        "",
        f"**Recommended by predeclared rule:** `{best}`",
        "",
        "2026 remains retrospective replay only.",
    ]
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def self_test() -> None:
    from types import SimpleNamespace

    def M(key, d, a, b, winner, score):
        return SimpleNamespace(
            key=key, date=d, tourney="Test", round="R1",
            p1_id=a, p1_name=a, p2_id=b, p2_name=b,
            winner=winner, score=score,
        )

    sample = [
        M("1", "2021-01-01", "A", "B", 1, "21-10 21-10"),
        M("2", "2021-01-01", "A", "C", 2, "21-18 18-21 15-21"),
        M("3", "2021-01-03", "A", "B", 1, "21-19 21-17"),
    ]
    rows = build_rows_raw(sample, 48)
    # Same-day second match must not see first match.
    assert rows[1]["form10_residual_diff"] == 0.0
    # Later match sees both prior A matches, while B has one.
    assert rows[2]["matches7_diff"] == 1.0
    assert rows[2]["sets7_diff"] == 3.0
    assert rows[2]["h2h24_residual"] > 0.0
    assert parse_score("21-10 18-21 21-19") == (2, 1, 60, 50, 3)
    print("V3.2 self-test OK")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/bwf"))
    ap.add_argument("--output", type=Path, default=Path("data/model_v3_2"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    report = run(args.data_dir, args.output)
    print(json.dumps({
        "generated_at_utc": report["generated_at_utc"],
        "recommended_variant": report["recommended_variant_from_walkforward"],
        "aggregate": {
            name: v["aggregate_2022_2025"]
            for name, v in report["variants"].items()
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
