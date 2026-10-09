"""No network. Regression tests for badminton_compare_shadow.py."""
import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from badminton_compare_shadow import compare, write_outputs


def dt(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


class ComparatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        model = self.root / 'data/model/snapshots/2026-10-09.csv'
        model.parent.mkdir(parents=True)
        with model.open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['bwf_match_id','player_a','player_b',
                'player_a_id','player_b_id','calibrated_p_a','asof_utc','model','status'])
            w.writeheader()
            w.writerow({'bwf_match_id':'5594:1552538','player_a':'LEE Zii Jia',
                        'player_b':'Christo POPOV','player_a_id':'81561','player_b_id':'72885',
                        'calibrated_p_a':'0.11105876','asof_utc':'2026-10-09T05:26:01+00:00',
                        'model':'bwf_elo_ms_platt_v1',
                        'status':'DATE_ONLY_START_UNVERIFIED_NO_BET'})
        self.sha = hashlib.sha256(model.read_bytes()).hexdigest()
        self.snapshot = 'data/model/snapshots/2026-10-09.csv'
        self.record = {'bwf_match_id':'5594:1552538','market':'H2H_FULL_MATCH',
                       'decision':'NO_BET_SHADOW','valid_pre_match_T0':False,
                       'model_asof_utc':'2026-10-09T05:26:01+00:00',
                       'side_1':{'bwf_name':'LEE Zii Jia','decimal_odds':2.65,
                                 'model_probability':0.11105876},
                       'side_2':{'bwf_name':'Christo POPOV','decimal_odds':1.32,
                                 'model_probability':0.88894124}}
        self.u = {'operator':'Unibet.fr','status':'ANALYZED_SHADOW',
                  'source_observed_at_utc':'2026-10-09T09:31:01+00:00',
                  'model_snapshot':self.snapshot,'model_snapshot_sha256':self.sha,
                  'matches':[self.record]}
        self.n = {'operator':'NetBet.fr',
                  'status':'OBSERVED_SHADOW_NEEDS_CONFIRMATION',
                  'observed_at_utc':'2026-10-09T09:38:49+00:00',
                  'model_snapshot':self.snapshot,'model_snapshot_sha256':self.sha,
                  'elo_comparisons':[{'bwf_match_id':'5594:1552538','market':'H2H_FULL_MATCH',
                       'decision':'NO_BET_SHADOW','valid_pre_match_T0':False,
                       'model_asof_utc':'2026-10-09T05:26:01+00:00',
                       'side_1':{'bwf_name':'LEE Zii Jia','decimal_odds':2.62,
                                 'model_probability':0.11105876},
                       'side_2':{'bwf_name':'Christo POPOV','decimal_odds':1.28,
                                 'model_probability':0.88894124}}]}

    def run_compare(self, moment='2026-10-09T09:42:00Z', u=None, n=None, **kw):
        return compare(self.u if u is None else u, self.n if n is None else n,
                       self.root, dt(moment), **kw)

    def test_both_valid_current(self):
        report, rows = self.run_compare()
        self.assertEqual(report['match_count'], 1)
        self.assertEqual(report['within_window_match_count'], 1)
        self.assertEqual(report['matches'][0]['observation_gap_seconds'], 468)
        self.assertEqual(rows[1]['best_historical_operator'], 'Unibet.fr')
        self.assertEqual(rows[1]['best_historical_ev_pct'], 17.34)
        self.assertEqual(rows[1]['best_within_window_ev_pct'], 17.34)
        self.assertEqual(rows[1]['decision'], 'NO_BET_SHADOW')

    def test_observations_age_out(self):
        report, rows = self.run_compare('2026-10-09T12:00:00Z')
        self.assertEqual(report['matches'][0]['comparison_status'], 'STALE_AT_COMPARISON_TIME')
        self.assertEqual(rows[1]['best_within_window_odds'], '')
        self.assertEqual(rows[1]['best_historical_odds'], 1.32)

    def test_max_skew(self):
        report, rows = self.run_compare(max_gap_seconds=300)
        self.assertEqual(report['matches'][0]['comparison_status'], 'ASYNCHRONOUS_OUTSIDE_WINDOW')
        self.assertEqual(rows[1]['best_within_window_odds'], '')

    def test_missing_operator(self):
        report, rows = self.run_compare(n={})
        self.assertEqual(report['matches'][0]['comparison_status'], 'ONE_BOOKMAKER_ONLY')
        self.assertEqual(len(rows), 2)

    def test_model_hash_tampering(self):
        n = dict(self.n, model_snapshot_sha256='0' * 64)
        report, rows = self.run_compare(n=n)
        self.assertEqual(report['sources']['NetBet.fr']['rows_validated'], 0)
        self.assertEqual(report['matches'][0]['comparison_status'], 'ONE_BOOKMAKER_ONLY')
        self.assertIn('MODEL_SNAPSHOT_SHA256_MISMATCH', report['sources']['NetBet.fr']['rejected'])

    def test_probability_tampering(self):
        record = json.loads(json.dumps(self.record))
        record['side_2']['model_probability'] = 0.1
        u = dict(self.u, matches=[record])
        report, rows = self.run_compare(u=u)
        self.assertEqual(report['sources']['Unibet.fr']['rows_validated'], 0)
        self.assertEqual(report['matches'][0]['comparison_status'], 'ONE_BOOKMAKER_ONLY')

    def test_reversed_bookmaker_player_order(self):
        r = json.loads(json.dumps(self.n))
        r['elo_comparisons'][0]['side_1'], r['elo_comparisons'][0]['side_2'] = (r['elo_comparisons'][0]['side_2'], r['elo_comparisons'][0]['side_1'])
        report, rows = self.run_compare(n=r)
        self.assertEqual(report['within_window_match_count'], 1)
        self.assertEqual(rows[1]['best_historical_ev_pct'], 17.34)

    def test_conflicting_model_versions_not_merged(self):
        # Two independently verifiable snapshots with distinct model timestamps
        # must never produce a cross-bookmaker best price.
        changed = self.root / 'data/model/snapshots/2026-10-09-alt.csv'
        text = (self.root / self.snapshot).read_text()
        text = text.replace('bwf_elo_ms_platt_v1', 'bwf_elo_ms_platt_v1_other')
        changed.write_text(text)
        n = json.loads(json.dumps(self.n))
        n['model_snapshot'] = 'data/model/snapshots/2026-10-09-alt.csv'
        n['model_snapshot_sha256'] = hashlib.sha256(changed.read_bytes()).hexdigest()
        report, rows = self.run_compare(n=n)
        self.assertEqual(report['matches'][0]['comparison_status'], 'INCOMPATIBLE_SNAPSHOTS_NO_MERGE')
        self.assertEqual(rows[1]['best_historical_odds'], '')
        self.assertEqual(rows[1]['best_within_window_odds'], '')

    def test_cli_writes_archive_and_latest(self):
        x = self.root / 'data/odds_shadow/netbet_quotes'
        x.mkdir(parents=True)
        (self.root / 'data/odds_shadow/latest_value_shadow.json').write_text(json.dumps(self.u))
        (x / 'latest.json').write_text(json.dumps(self.n))
        command = [sys.executable, str(Path(__file__).with_name('badminton_compare_shadow.py')),
                   '--root', str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['matches'], 1)
        self.assertTrue((self.root / 'data/odds_shadow/comparison/latest.json').exists())
        self.assertTrue((self.root / 'data/odds_shadow/comparison/latest.csv').exists())

    def test_no_t0_and_files(self):
        report, rows = self.run_compare()
        files = write_outputs(report, rows, self.root / 'comparison')
        self.assertTrue(Path(files['archive_json']).exists())
        self.assertTrue(Path(files['archive_csv']).exists())
        data = json.loads((self.root / 'comparison/latest.json').read_text())
        self.assertFalse(data['matches'][0]['valid_pre_match_T0'])
        self.assertEqual(data['policy'], 'SHADOW_ONLY_NO_BET_NO_T0_NO_SHEETS_WRITE')


if __name__ == '__main__':
    unittest.main()
