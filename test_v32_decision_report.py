"""Tests for V3.2 evidence decision report."""
import unittest
from datetime import datetime, timedelta, timezone
from v32_decision_report import summarize


class DecisionReportTests(unittest.TestCase):
    def test_stale_snapshot_not_actionable(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        report = summarize({'generated_at_utc': timestamp, 'results': []})
        self.assertEqual(report['snapshot_freshness'], 'STALE_OR_FUTURE')
        self.assertEqual(report['conclusion'], 'STALE_SNAPSHOT_NOT_ACTIONABLE')
        self.assertIsNone(report['validated_roi_pct'])

    def test_duplicate_bookmakers_are_one_fixture(self):
        data = {'verification_summary': {'model_fixture_count': 3, 'public_quote_count': 3},
                'results': [
                    {'status': 'UNVERIFIED_SHADOW_ONLY', 'bwf_match_id': 'm1',
                     'source': 'unibet_fr', 'flags': ['KICKOFF_UNVERIFIED']},
                    {'status': 'UNVERIFIED_SHADOW_ONLY', 'bwf_match_id': 'm1',
                     'source': 'netbet_quotes', 'flags': ['KICKOFF_UNVERIFIED']},
                    {'status': 'FIXTURE_ABSENT_FROM_MODEL', 'source': 'netbet_quotes'}]}
        report = summarize(data)
        self.assertEqual(report['unique_matched_fixtures'], 1)
        self.assertEqual(report['matched_bookmaker_observations'], 2)
        self.assertEqual(report['unmatched_observations'], 1)
        self.assertEqual(report['blockers']['KICKOFF_UNVERIFIED'], 2)
        self.assertIsNone(report['validated_roi_pct'])
        self.assertEqual(report['policy'], 'RESEARCH_ONLY_NO_BET')


if __name__ == '__main__':
    unittest.main()
