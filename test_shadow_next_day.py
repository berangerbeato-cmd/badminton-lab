import unittest
from datetime import date
from shadow_next_day import next_day_predictions

class NextDayTests(unittest.TestCase):
    def test_only_dated_tomorrow(self):
        base={"status":"MODEL_READY","fixture_source":"BWF_OFFICIAL"}
        rows=[dict(base,match_date="2026-10-15",match_id="1"),
              dict(base,match_date="2026-10-16",match_id="2"),
              dict(base,match_id="3"),
              dict(base,match_date="2026-10-15",match_id="4",fixture_source="USER_CAPTURE_UNVERIFIED")]
        out=next_day_predictions({"predictions":rows},date(2026,10,14))
        self.assertEqual([x["match_id"] for x in out["predictions"]],["1"])
    def test_no_invented_date(self):
        self.assertEqual(next_day_predictions({"predictions":[{"status":"MODEL_READY"}]},date(2026,10,14))["count"],0)
if __name__=="__main__": unittest.main()
