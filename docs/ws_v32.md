# WS V3.2 — implementation and validation (20 November 2026)

Independent WS model, ready for review on `feature/ws-v32-model`. No merge into main. No existing MS source, workflow, parameter, report, archive or ledger changed.

## Audit and architecture

Base: `6ec78fc9`. Existing MS uses K=64, score/rest logistic features, frozen coefficient fit 2019–2025, daily prior-UTC-day state. Shared stateless `build_rows_raw`, logistic fit, prediction, features and Elo math are imported without modifying their source. WS has its own strict reader, selected parameters, coefficient artifact, ratings, cleaned history, predictions, quality reports and ledger namespace under `data/ws_v3_2` and `data/singles_ledgers/WS`.

Existing functionality checked: 129 baseline unittest tests, Elo/daily/walk-forward/sync self-tests, real MS daily generation. Run on a detached pristine main worktree and on the feature checkout with identical 2026-11-20T12:00Z input, the MS latest report, upcoming CSV and daily snapshot match byte for byte. Existing workflows often explicitly check out main; none were edited. New WS workflow checks out its triggering ref so branch validation is meaningful.

## Historical inventory

415 shared raw archives; 11,821 unique WS source encounters; 11,379 accepted, 442 excluded; 1,231 players; 331 tournaments with valid WS. Source mirrors are deduplicated by the existing `_matches` function and tournament/match ID. Mixed-ID gender conflicts and doubles are refused. Entire games must satisfy badminton score termination and winner consistency; explicit abnormal status, walkovers and retirements are excluded. Unknown source status codes are conservatively rejected. Same-day and future results are excluded.

| Year | Accepted WS |
|---|---:|
|2018|1568|
|2019|1538|
|2020|285|
|2021|603|
|2022|995|
|2023|1602|
|2024|1622|
|2025|1627|
|2026 through Nov 19|1539|

Rejections: abnormal status 357; invalid individual game score 71; missing player IDs 5; incomplete score 2; no winner 7. These are sequential, mutually exclusive primary reasons, not all defects per record. Raw source mirrors are not counted as additional encounters.

Coverage is **not proven complete**. Historical collection marks 2020, 2021 and 2023 partial; monthly counts and 13 missing indexed elite archives are listed in `inventory.json`. All 13 were queried once using existing BWF transport, without redownloading any existing archive; all returned empty match payloads. No cancellation was inferred. `backfill_attempts.json` preserves the responses. Index completeness, result-publication times and complete low-tier coverage cannot be established from this cache. No raw archives changed.

Latest accepted WS result: 2026-11-19. At execution, no future WS fixture with identified players was supplied by the cache. An empty future feed is reported honestly.

## Training and evaluation

Warm-up: 2018. Development folds: 2022–2025. Each fold selects K from raw Elo performance in its preceding year, fits logistic coefficients on 2019 through the preceding year, and evaluates the next year. Same-day ratings/features remain frozen before updates. Final K=64 selected on 2025. Final coefficients fit 2019–2025 only. 2026 is held out from all parameter and feature selection, then evaluated as a retrospective replay, never an archived live forecast.

Predeclared selection: enriched candidates must improve aggregate development Brier by >0.001 and beat calibrated Elo in at least three of four folds; otherwise use calibrated Elo. Among eligible candidates, minimum aggregate Brier wins. Selected `elo_v3_1_all` is the existing internal feature-set name; the delivered WS pipeline is V3.2. It contains form residual10, 7/14-day activity, set load, rest/inactivity, H2H residuals, 30/90-day Elo trends, and 5-match score dominance. Coefficients, means and scales are learned solely from WS. The 11-feature candidate only narrowly beats score/rest in development (0.171671 vs 0.171853); this does not prove every included feature is individually useful. Experience and tournament category are evaluated as quality segments, not asserted causal features. No medical or unobserved fatigue claims.

Calibration is logistic/Platt for Elo; enriched model jointly estimates a logistic probability. Same ridge=5 mathematical regularizer as existing code; no male fitted coefficients are copied. No extra post-test calibration was fitted. `model.json` persists coefficients, fit dates and training fingerprint; daily execution never retrains them. Training must be run deliberately for a new version.

| 2026 retrospective test | N | Brier | Log loss | Accuracy |
|---|---:|---:|---:|---:|
| WS raw Elo |1539|0.177207|0.525156|72.32%|
| WS calibrated Elo |1539|0.177157|0.525634|72.25%|
| WS selected enriched |1539|0.171638|0.512399|74.46%|
| Existing MS V3.2, same time window |1818|0.204388|0.593914|68.59%|

Different competitors, coverage and competitive balance prevent interpreting this as an inherent sex difference. No historical bookmaker price set exists here, so no betting ROI backtest is claimed.

Full yearly folds, category, experience, balanced-match and favourite segments plus reliability bins are in `backtest.json`. In the selected model, 90–100% favourite-confidence bin averaged 95.87%, observed 94.14% (324 matches): some overconfidence remains. 50–60% confidence bin: 54.93% predicted, 57.22% observed (388). Small samples remain noisy; the value gate refuses selected probabilities above 85%, unknown/low-history players, stale results and long inactivity. Thresholds are conservative engineering rules, not ROI-optimized parameters.

## Usage and screenshot intake

```bash
python ws_model.py daily
# Explicit research retraining only:
python ws_model.py train
python badminton_singles_value.py path/to/transcribed_quotes.json --out /tmp/value.json
python -m unittest discover -v
```

There is no OCR or automatic ChatGPT-interface integration. Transcribe each screenshot into a JSON list. Required fields for a betting decision:

```json
[{
  "discipline": "WS",
  "player_a": "FULL BWF NAME",
  "player_b": "FULL BWF NAME",
  "start_utc": "2026-11-21T12:00:00Z",
  "start_verified": true,
  "kickoff_source": "actual source identifying this fixture and exact kickoff",
  "odds_a": 1.90,
  "odds_b": 2.00,
  "observed_at": "2026-11-20T11:50:00Z",
  "bookmaker": "Unibet",
  "evidence": "actual screenshot file or source reference"
}]
```

This is a schema illustration with synthetic prices, **not a real quote**. Omit discipline to infer it only when both names resolve unambiguously in exactly one singles dataset. Supply BWF IDs when names are ambiguous; supplied names must still agree. Explicit MD/WD/XD entries are ignored. Existing matching helper is reused unchanged. Unrecognized names, ambiguous abbreviated names, missing or invalid prices result in NO BET. Future fixture identity, exact kickoff and price evidence must be checked by the human transcribing the screenshot; a boolean field does not independently verify a source.

Output has discipline, names, p_a, both quoted odds, raw and margin-normalized market probabilities, EV on both sides and BET/NO BET with reasons. EV=p*odds−1. Quote age must be <=2h; kickoff future within 14 days; latest scored results <=7 days; WS history >=10 per player, no >90-day absence. New unified analysis gate requires EV>=10%, selected p between 50% and 85%; this wrapper does not change legacy MS production decision policies. No wager is placed.

## Actual / simulation ledger

`python singles_ledger.py --append record.json` writes exclusively under the record discipline and kind (`actual` or `simulation`). Required: id, match_id, discipline, kind, prediction_at, placed_at, start_utc, stake, odds, p_model, result, prediction_evidence; actual bets additionally require bet_receipt. Times must prove prediction <= bet < kickoff. Append-only ID files refuse overwrite. Never label reconstructed forecasts as prematch evidence.

For a pending record, use `--settle settlement.json` with id, discipline, kind, result, settled_at and evidence. Settlement is a separate immutable event. `summary.json` separates MS, WS, combined, actual and simulation; pending stakes are shown but excluded from settled ROI. No legacy betting records were imported or inferred; therefore the new consolidated report covers explicit records only, and is not a claim about the user's all-time ROI. All legacy MS files remain preserved. Historical backtests and timestamped prospective forecasts remain separate from ledgers.

## Daily automation and remaining limits

New independent workflow `Badminton WS V3.2 independent daily` triggers on shared BWF refresh completion, daily at 09:50 UTC, and branch pushes / PRs. No second BWF downloader. WS failure cannot block MS. Frozen model is used; generated outputs and diagnostics are uploaded as Actions artifacts. Only main runs publish WS output paths using the existing guarded publication utility. Scheduled activation and main publication require merge; no merge performed. Stale-source enforcement follows diagnostic publication so the failure remains visible.

Tests: baseline 129, plus 11 WS tests initially (140 total); supplementary functional checks cover daily execution, real archive isolation, historical replay/inference equality, and identical MS outputs. See validation report for final count and remote Actions status. Main publication cannot be exercised from this branch and is not claimed as tested in production.

## Real-match demonstration

WANG Zhi Yi vs Natsuki NIDAIRA, Hong Kong Open, 19 November 2026 (`5218:1570078`). Retrospective prediction from information strictly before that UTC date: **95.2041% Wang**, raw Elo 94.2211%. Recorded outcome 21–13, 21–15. This was calculated now, not saved before the match. `demo_replay.json` records the pre-date history counts and selected features. No verified historical bookmaker quote available: no EV or betting recommendation is invented. Separate synthetic quote tests validate EV arithmetic and rejection gates, not real bookmaker profitability.
