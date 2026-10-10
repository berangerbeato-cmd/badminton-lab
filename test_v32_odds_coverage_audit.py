import unittest
from v32_odds_coverage_audit import classify, next_actions


class TestCoverageBlockers(unittest.TestCase):
    def test_netbet_rejections_are_not_quotes(self):
        item = {"status": "NO_CONFIDENT_QUOTES", "quote_count": 0,
                "index_market_audit": {"rejections": {"COMPETITION_NOT_PROVEN": 4}}}
        self.assertEqual(classify("netbet_quotes", item), "MARKETS_SEEN_BUT_UNVERIFIED")

    def test_robots_is_access_blocker(self):
        self.assertEqual(classify("bwin_fr", {"status": "SKIPPED_ROBOTS_DENIED_OR_UNAVAILABLE", "quote_count": 0}),
                         "ACCESS_NOT_PERMITTED_OR_UNAVAILABLE")

    def test_http_reject_not_empty_market(self):
        self.assertEqual(classify("oddspedia_badminton", {"status": "REJECT_HTTP_STATUS_OR_REDIRECT", "quote_count": 0}),
                         "HTTP_OR_REDIRECT_REJECTED")

    def test_observed_quote_not_confirmed(self):
        self.assertEqual(classify("unibet_fr", {"status": "OK", "quote_count": 1}),
                         "QUOTE_OBSERVED_REQUIRES_VALIDATION")

    def test_prioritize_public_evidence(self):
        sources = {"netbet_quotes": {"blocker": "MARKETS_SEEN_BUT_UNVERIFIED"},
                   "unibet_fr": {"blocker": "NO_CONFIDENT_QUOTES_EXTRACTED"},
                   "bwin_fr": {"blocker": "ACCESS_NOT_PERMITTED_OR_UNAVAILABLE"}}
        actions = next_actions(sources)
        self.assertEqual([a["priority"] for a in actions], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
