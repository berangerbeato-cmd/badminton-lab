import unittest
from bwf_dated_fixture_bridge import enrich

class BridgeTests(unittest.TestCase):
    def test_unique_pair_date(self):
        p={"predictions":[{"player_a_id":"11","player_b_id":"22","fixture_source":"USER_CAPTURE_UNVERIFIED"}]}
        rows=[{"player_a_id":"22","player_b_id":"11","start_utc":"2026-10-15T00:00:00+00:00","status":"DATE_ONLY_START_UNVERIFIED_NO_BET"}]
        result=enrich(p,rows)["predictions"][0]
        self.assertEqual(result["match_date"],"2026-10-15")
        self.assertEqual(result["fixture_source"],"BWF_ARCHIVE_PAIR_DATE")
    def test_ambiguous_dates_rejected(self):
        p={"predictions":[{"player_a_id":"11","player_b_id":"22"}]}
        rows=[{"player_a_id":"11","player_b_id":"22","start_utc":f"2026-10-{day}T00:00:00+00:00","status":"DATE_ONLY_START_UNVERIFIED_NO_BET"} for day in ("15","16")]
        self.assertNotIn("match_date",enrich(p,rows)["predictions"][0])
if __name__=="__main__":unittest.main()
