"""Offline checks for immutable shadow ledger reporting."""
import json
import tempfile
import unittest
from pathlib import Path
from v32_ledger_summary import summarize


class LedgerSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name)
        folder = self.ledger / '1001'
        folder.mkdir()
        (folder / 'manifest.json').write_text(json.dumps({'run_id': '1001'}))
        (folder / 'comparison.json').write_text(json.dumps({
            'policy': 'RESEARCH_ONLY_NO_BET_NO_T0',
            'source_audit': {'netbet_quotes': {'status': 'FRESH_SNAPSHOT'}},
            'results': [{'bwf_match_id': 'a:1', 'decision': 'NO_BET',
                         'flags': ['KICKOFF_UNVERIFIED']},
                        {'bwf_match_id': 'a:1', 'decision': 'NO_BET',
                         'flags': ['KICKOFF_UNVERIFIED']}]}))

    def test_summary_does_not_invent_profit(self):
        result = summarize(self.ledger)
        self.assertEqual(result['run_count'], 1)
        self.assertEqual(result['observation_count'], 2)
        self.assertEqual(result['unique_bwf_matches'], 1)
        self.assertEqual(result['quality_flag_counts']['KICKOFF_UNVERIFIED'], 2)
        self.assertIsNone(result['roi'])

    def test_rejects_betting_decision(self):
        file = self.ledger / '1001' / 'comparison.json'
        data = json.loads(file.read_text())
        data['results'][0]['decision'] = 'BET'
        file.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            summarize(self.ledger)

    def test_empty_ledger(self):
        self.assertEqual(summarize(self.ledger / 'nonexistent')['run_count'], 0)


if __name__ == '__main__':
    unittest.main()
