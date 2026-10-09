"""Offline historical audit regression tests; use real repo Elo modules on GitHub."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from elo_model import Match
from badminton_historical_backtest import (
    chronological_replay, validate_against_holdout, summarize, group_confidence,
    wilson_interval, write_outputs, report_for, _load_configuration, run,
    SCHEMA, POLICY,
)


def mk(key, date, a='A', b='B', winner=1):
    return Match(key, date, 'Test tournament', 'QF', a, a, b, b, winner, '21-11 21-17')


def fake_record(id, date='2025-01-06', winner=1):
    return mk(id, date, a='A', b='B', winner=winner)


class HistoricalBacktestTests(unittest.TestCase):
    def test_01_prior_day_results_influence_future(self):
        matches=[mk('0','2024-10-01'),mk('1','2025-01-06'),mk('2','2026-01-03')]
        out=chronological_replay(matches,48,1,0)
        self.assertEqual([x['year'] for x in out],[2025,2026])
        self.assertGreater(out[0]['raw_p_a'],.5)
        self.assertGreater(out[1]['raw_p_a'],out[0]['raw_p_a'])

    def test_02_same_day_games_cannot_leak(self):
        prior=mk('0','2024-12-31')
        matches=[prior,mk('1','2025-01-02',winner=1),mk('2','2025-01-02',winner=2)]
        out=chronological_replay(matches,48,1,0)
        self.assertEqual(out[0]['raw_p_a'],out[1]['raw_p_a'])
        self.assertEqual(out[0]['cold_start'],out[1]['cold_start'])
        flipped=[prior,mk('1','2025-01-02',winner=2),mk('2','2025-01-02',winner=1)]
        other=chronological_replay(flipped,48,1,0)
        self.assertEqual([x['raw_p_a'] for x in out],[x['raw_p_a'] for x in other])

    def test_03_date_sorted_before_replay(self):
        matches=[mk('2','2026-01-01'),mk('0','2024-01-01'),mk('1','2025-01-01')]
        out=chronological_replay(matches,48,1,0)
        self.assertEqual([x['match_id'] for x in out],['1','2'])

    def test_04_probabilities_are_complementary(self):
        out=chronological_replay([mk('0','2024-12-31'),mk('1','2025-01-01')],48,1.05,-.03)
        r=out[0]
        self.assertAlmostEqual(1/r['fair_odds_a']+1/r['fair_odds_b'],1,places=3)
        self.assertTrue(r['favorite_won'])
        self.assertGreaterEqual(r['favorite_probability'],.5)

    def test_05_upset_count(self):
        prior=mk('0','2024-01-01')
        out=chronological_replay([prior,mk('1','2025-01-01',winner=2)],48,1,0)
        self.assertEqual(summarize(out)['upsets'],1)
        self.assertEqual(summarize(out)['favorite_wins'],0)

    def test_06_empty_year_and_wilson(self):
        self.assertIsNone(wilson_interval(0,0))
        self.assertEqual(summarize([])['n'],0)
        self.assertEqual(sum(g['n'] for g in group_confidence([])),0)

    def test_07_confidence_buckets_partition(self):
        rows=chronological_replay([mk(str(i),'2025-01-01',a=f'A{i}',b=f'B{i}') for i in range(20)],48,1,0)
        self.assertEqual(sum(g['n'] for g in group_confidence(rows)),20)
        self.assertEqual(group_confidence(rows)[0]['n'],20)

    def test_08_incompatible_years_rejected(self):
        with self.assertRaises(ValueError):
            chronological_replay([],48,1,0,years=(2024,2025))

    def test_09_duplicate_match_id_rejected(self):
        with self.assertRaises(ValueError):
            chronological_replay([mk('1','2025-01-01'),mk('1','2025-01-02')],48,1,0)

    def test_10_no_market_odds_or_roi_fabricated(self):
        rows=chronological_replay([mk('1','2025-01-01')],48,1,0)
        r=rows[0]
        self.assertEqual(r['historical_bookmaker_odds'],'')
        self.assertEqual(r['betting_roi'],'')
        self.assertFalse(r['historical_bookmaker_verified'])

    def build_frozen_2025(self, root):
        root=Path(root)
        model=root/'data'/'model'
        model.mkdir(parents=True,exist_ok=True)
        games=[mk(str(i),f'2025-01-{i//5+1:02d}',a=f'A{i}',b=f'B{i}',winner=(i%2)+1) for i in range(100)]
        out=chronological_replay(games,48,1,0)
        with (model/'holdout_2025.csv').open('w',newline='') as f:
            fields=['match_id','date','player_a_id','player_b_id','winner','p_a','cold_start']
            writer=csv.DictWriter(f,fieldnames=fields)
            writer.writeheader()
            for x in out:
                writer.writerow({**{k:x[k] for k in ('match_id','date','player_a_id','player_b_id','winner','cold_start')}, 'p_a':x['raw_p_a']})
        s=summarize(out)
        raw=sum((x['raw_p_a']-(x['winner']==1))**2 for x in out)/len(out)
        baseline={'model':'bwf_elo_ms_v0','validation_year':2024,'holdout_test_year':2025,'chosen_k':48,
                  'test_2025':{'n':100,'brier':raw}}
        cal={'model':'bwf_elo_ms_platt_v1','baseline_model':'bwf_elo_ms_v0',
             'fit_year':2024,'holdout_year':2025,'k':48,
             'platt_slope':1,'platt_intercept':0,
             'holdout_2025':{'n':100,'calibrated_brier':s['brier'],'accuracy':s['accuracy']}}
        (model/'elo_report.json').write_text(json.dumps(baseline))
        (model/'elo_calibration_report.json').write_text(json.dumps(cal))
        return model,out,baseline,cal

    def test_11_holdout_validation_accepts_identical_probs(self):
        with tempfile.TemporaryDirectory() as td:
            model,rows,b,c=self.build_frozen_2025(td)
            info=validate_against_holdout(rows,model,b,c)
            self.assertEqual(info['holdout_rows_verified'],100)
            self.assertEqual(info['max_probability_difference'],0)

    def test_12_holdout_mutation_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            model,rows,b,c=self.build_frozen_2025(td)
            tampered=[dict(x) for x in rows]
            tampered[10]['raw_p_a']=.999
            with self.assertRaisesRegex(ValueError,'probability changed'):
                validate_against_holdout(tampered,model,b,c)

    def test_13_holdout_missing_row_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            model,rows,b,c=self.build_frozen_2025(td)
            with self.assertRaisesRegex(ValueError,'archive mismatch'):
                validate_against_holdout(rows[:-1],model,b,c)

    def test_14_frozen_calibration_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            model,rows,b,c=self.build_frozen_2025(td)
            k,slope,intercept,*_= _load_configuration(model)
            self.assertEqual((k,slope,intercept),(48,1,0))
            c['fit_year']=2025
            (model/'elo_calibration_report.json').write_text(json.dumps(c))
            with self.assertRaisesRegex(ValueError,'provenance'):
                _load_configuration(model)

    def test_15_csv_and_report_no_betting_roi(self):
        with tempfile.TemporaryDirectory() as td:
            model,rows,b,c=self.build_frozen_2025(td)
            report=report_for(rows,b,{'src':'test'},{'archives_read':1},{'holdout_rows_verified':100})
            write_outputs(report,rows,Path(td)/'data'/'historical_backtest')
            dest=Path(td)/'data'/'historical_backtest'
            published=json.loads((dest/'latest.json').read_text())
            self.assertEqual(published['schema_version'],SCHEMA)
            self.assertEqual(published['policy'],POLICY)
            self.assertIsNone(published['historical_market_roi_eur'])
            self.assertEqual(published['model_metrics']['2025']['n'],100)
            self.assertEqual(published['model_metrics']['2026']['n'],0)
            with (dest/'predictions_2025_2026.csv').open() as f:
                data=list(csv.DictReader(f))
            self.assertEqual(len(data),100)
            self.assertTrue(all(not x['betting_roi'] for x in data))

    def test_16_report_has_nonzero_2026_when_present(self):
        sample=[mk('0','2024-01-01'),mk('1','2025-01-01'),mk('2','2026-03-01')]
        out=chronological_replay(sample,48,1,0)
        report=report_for(out,{'chosen_k':48},{},{},{})
        self.assertEqual(report['model_metrics']['2026']['n'],1)
        self.assertEqual(report['model_metrics']['2025']['n'],1)


if __name__=='__main__':
    unittest.main()
