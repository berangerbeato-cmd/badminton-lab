"""Offline regression tests for conservative V3.2 bookmaker comparisons."""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'v32_odds_compare.py'


class ShadowComparatorTests(unittest.TestCase):
    def compare(self, age_minutes=5, listed_day=None, model_day=None, model_after=False):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        observed = now - timedelta(minutes=age_minutes)
        model_at = observed + timedelta(minutes=1) if model_after else observed - timedelta(minutes=1)
        day = model_day or now.date().isoformat()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / 'model.csv'
            row = dict(bwf_match_id='test:1', start_utc=day + 'T00:00:00+00:00',
                       player_a='Anders ANTONSEN', player_b='LEE Zii Jia',
                       v3_2_p_a='0.84286719', asof_utc=model_at.isoformat(),
                       status='DATE_ONLY_START_UNVERIFIED_NO_BET')
            with model.open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            folder = root / 'odds' / 'netbet_quotes'
            folder.mkdir(parents=True)
            quote = dict(market='H2H_FULL_MATCH', player_1_display='Anders Antonsen',
                         player_2_display='Lee Zii Jia', odds_1=1.65, odds_2=1.78)
            if listed_day:
                quote['listed_day_paris'] = listed_day
            (folder / 'latest.json').write_text(json.dumps({
                'observed_at_utc': observed.isoformat(), 'quotes': [quote]}))
            result = subprocess.run([sys.executable, str(SCRIPT), '--model', str(model),
                                     '--odds-dir', str(root / 'odds'),
                                     '--out', str(root / 'out')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads((root / 'out' / 'latest.json').read_text())

    def test_fresh_quote_is_shadow_only(self):
        output = self.compare()
        self.assertEqual(output['comparison_count'], 1)
        row = output['results'][0]
        self.assertEqual(row['decision'], 'NO_BET')
        self.assertIn('BWF_KICKOFF_DATE_ONLY', row['flags'])
        self.assertAlmostEqual(row['illustrative_ev_pct'][0], 39.07)

    def test_stale_quote_is_rejected(self):
        output = self.compare(age_minutes=90)
        self.assertEqual(output['comparison_count'], 0)
        self.assertEqual(output['rejected_sources'][0]['reason'], 'OBSERVATION_NOT_FRESH')

    def test_mismatched_day_is_rejected(self):
        today = datetime.now(timezone.utc).date()
        output = self.compare(listed_day=(today + timedelta(days=1)).isoformat(),
                              model_day=today.isoformat())
        self.assertEqual(output['results'][0]['status'], 'EVENT_DAY_MISMATCH')
        self.assertEqual(output['results'][0]['decision'], 'NO_BET')

    def test_model_after_quote_is_flagged(self):
        output = self.compare(model_after=True)
        self.assertIn('MODEL_AFTER_OBSERVATION', output['results'][0]['flags'])


if __name__ == '__main__':
    unittest.main()
