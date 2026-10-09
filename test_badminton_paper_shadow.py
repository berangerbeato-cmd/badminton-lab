"""Offline regression tests for forward-only exploratory paper betting."""
import csv
import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from badminton_paper_shadow import select_new, settle, summarize, fixture, complete_score


class PaperShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / 'data/shadow_paper'
        self.now = datetime(2026, 10, 9, 9, 40, tzinfo=timezone.utc)
        self.bwf = self.root / 'data/bwf/matches/5594.json.gz'
        self.bwf.parent.mkdir(parents=True)
        self.raw = {'id': 12345, 'draw_name': 'MS', 'winner': None,
                    't1p1_detail': {'id': 100, 'name_display': 'Alice'},
                    't2p1_detail': {'id': 200, 'name_display': 'Bob'},
                    'team1Score': '', 'team2Score': ''}
        self.put_bwf(self.raw)
        model_dir = self.root / 'data/model/snapshots'
        model_dir.mkdir(parents=True)
        model_csv = ('bwf_match_id,player_a_id,player_a,player_b_id,player_b,calibrated_p_a,asof_utc,model,status,start_utc\n'
                     '5594:12345,100,Alice,200,Bob,0.60000000,2026-10-09T05:00:00+00:00,bwf_elo_ms_platt_v1,DATE_ONLY_START_UNVERIFIED_NO_BET,2026-10-09T00:00:00+00:00\n')
        (model_dir / '2026-10-09.csv').write_text(model_csv)
        digest = hashlib.sha256(model_csv.encode()).hexdigest()
        self.match = {
            'bwf_match_id': '5594:12345', 'comparison_status': 'WITHIN_TIME_WINDOW_UNVERIFIED_SHADOW',
            'model': 'bwf_elo_ms_platt_v1', 'model_asof_utc': '2026-10-09T05:00:00+00:00',
            'model_snapshot_sha256': digest, 'decision': 'NO_BET_SHADOW',
            'valid_pre_match_T0': False, 'kickoff_verified': False, 'executable_price_verified': False,
            'observation_gap_seconds': 180,
            'source_details': {
                'Unibet.fr': {'current': True, 'observed_at_utc': '2026-10-09T09:31:00+00:00'},
                'NetBet.fr': {'current': True, 'observed_at_utc': '2026-10-09T09:34:00+00:00'},
            },
            'players': [
                {'bwf_player_id': '100', 'name': 'Alice', 'model_probability': .6,
                 'observed_odds': {'Unibet.fr': 1.8, 'NetBet.fr': 1.85},
                 'best_within_window': {'operator': 'NetBet.fr', 'decimal_odds': 1.85, 'ev_pct': 11.0}},
                {'bwf_player_id': '200', 'name': 'Bob', 'model_probability': .4,
                 'observed_odds': {'Unibet.fr': 2.1, 'NetBet.fr': 2.2},
                 'best_within_window': {'operator': 'NetBet.fr', 'decimal_odds': 2.2, 'ev_pct': -12.0}},
            ],
        }
        self.report = {
            'schema_version': 'badminton_shadow_comparator_v2.1',
            'policy': 'SHADOW_ONLY_NO_BET_NO_T0_NO_SHEETS_WRITE',
            'generated_at_utc': '2026-10-09T09:38:00+00:00',
            'matches': [self.match],
        }

    def put_bwf(self, row):
        with gzip.open(self.bwf, 'wt', encoding='utf-8') as h:
            json.dump({'results': {'by_time': {'time_group': [row]}}}, h)

    def test_01_no_historical_backfill(self):
        answer = select_new(self.root, self.report, self.now+timedelta(hours=3), self.out)
        self.assertEqual(answer['report_status'], 'COMPARISON_REPORT_TOO_OLD_OR_FROM_FUTURE')
        self.assertEqual(answer['candidates_created'], 0)

    def test_02_eligible_exploratory_frozen_once(self):
        answer = select_new(self.root, self.report, self.now, self.out)
        self.assertEqual(answer['candidates_created'], 1)
        file = self.out/'picks/5594_12345.json'
        first = json.loads(file.read_text())
        self.assertEqual(first['decimal_odds'], '1.85')
        self.assertEqual(first['stake_eur'], '10.00')
        self.assertEqual(first['ev_pct'], '11.00')
        self.assertEqual(first['selection_status'], 'EXPLORATORY_UNVERIFIED_PREMATCH')
        self.assertFalse(first['valid_pre_match_T0'])
        second = select_new(self.root, self.report, self.now, self.out)
        self.assertEqual(second['candidates_created'], 0)
        self.assertEqual(second['already_frozen'], 1)
        self.assertEqual(first, json.loads(file.read_text()))

    def test_03_no_selection_when_bwf_result_already_known(self):
        row = dict(self.raw, winner=1, team1Score='<span>21</span><span>21</span>',
                   team2Score='<span>16</span><span>18</span>')
        self.put_bwf(row)
        outcome = select_new(self.root, self.report, self.now, self.out)
        self.assertEqual(outcome['candidates_created'], 0)
        self.assertEqual(outcome['rejections'], {'MATCH_ALREADY_HAS_RESULT': 1})

    def test_04_reject_non_comparable_odds(self):
        self.match['comparison_status'] = 'STALE_AT_COMPARISON_TIME'
        answer = select_new(self.root, self.report, self.now, self.out)
        self.assertEqual(answer['candidates_created'], 0)
        self.assertEqual(answer['rejections']['COMPARISON_NOT_FRESH'], 1)

    def test_05_reject_tampered_model_hash(self):
        self.match['model_snapshot_sha256'] = 'deadbeef'
        answer = select_new(self.root, self.report, self.now, self.out)
        self.assertEqual(answer['rejections']['MODEL_SNAPSHOT_NOT_VERIFIED'], 1)

    def test_06_reject_ev_below_threshold(self):
        outcome = select_new(self.root, self.report, self.now, self.out, Decimal('12'))
        self.assertEqual(outcome['rejections']['NO_EV_ABOVE_THRESHOLD'], 1)

    def test_07_reject_changed_ev_claim(self):
        self.match['players'][0]['best_within_window']['ev_pct'] = 29
        outcome = select_new(self.root, self.report, self.now, self.out)
        self.assertEqual(outcome['rejections']['EV_RECOMPUTATION_MISMATCH'], 1)

    def test_08_win_settles_from_immutable_price(self):
        select_new(self.root, self.report, self.now, self.out)
        self.assertEqual(settle(self.root, self.out, self.now)['pending'], 1)
        row = dict(self.raw, winner=1, team1Score='<span>21</span><span>21</span>',
                   team2Score='<span>16</span><span>18</span>')
        self.put_bwf(row)
        settled = settle(self.root, self.out, self.now+timedelta(days=1))
        self.assertEqual(settled['new_settlements'], 1)
        summary = summarize(self.out, self.now+timedelta(days=1))
        self.assertEqual(summary['exploratory_pnl_eur'], '8.50')
        self.assertEqual(summary['exploratory_roi_pct_not_validated'], '85.00')
        self.assertEqual(summary['validated_picks'], 0)
        self.assertIsNone(summary['validated_roi_pct'])
        self.assertEqual(settle(self.root, self.out, self.now+timedelta(days=2))['new_settlements'], 0)

    def test_09_loss_and_no_overwrite(self):
        select_new(self.root, self.report, self.now, self.out)
        row = dict(self.raw, winner=2, team1Score='<span>17</span><span>19</span>',
                   team2Score='<span>21</span><span>21</span>')
        self.put_bwf(row)
        settle(self.root, self.out, self.now+timedelta(days=1))
        summary = summarize(self.out, self.now+timedelta(days=1))
        self.assertEqual(summary['exploratory_pnl_eur'], '-10.00')
        self.assertEqual(summary['exploratory_roi_pct_not_validated'], '-100.00')
        self.assertEqual(summary['exploratory_losses'], 1)
        self.assertTrue((self.out/'summary/ledger_latest.csv').exists())

    def test_10_incomplete_score_not_settled(self):
        select_new(self.root, self.report, self.now, self.out)
        self.put_bwf(dict(self.raw, winner=1, team1Score='<span>8</span>', team2Score='<span>6</span>'))
        answer = settle(self.root, self.out, self.now+timedelta(days=1))
        self.assertEqual(answer['unverifiable'], 1)
        self.assertFalse((self.out/'settlements/5594_12345.json').exists())

    def test_11_no_retrospective_pick_next_calendar_day(self):
        self.report['generated_at_utc'] = '2026-10-10T09:38:00+00:00'
        self.match['source_details']['Unibet.fr']['observed_at_utc'] = '2026-10-10T09:31:00+00:00'
        self.match['source_details']['NetBet.fr']['observed_at_utc'] = '2026-10-10T09:34:00+00:00'
        ans = select_new(self.root, self.report, self.now+timedelta(days=1), self.out)
        self.assertEqual(ans['rejections']['PAST_CALENDAR_DAY'], 1)

    def test_12_avoid_doubles_and_bad_ids(self):
        self.put_bwf(dict(self.raw, draw_name='MD'))
        self.assertIsNone(fixture(self.root, '5594:12345'))
        self.assertIsNone(fixture(self.root, '../bad'))


if __name__ == '__main__':
    unittest.main()
