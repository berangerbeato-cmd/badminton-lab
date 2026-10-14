import unittest
from datetime import datetime, timezone
from research_value_shortlist import build

NOW = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)
MODEL = {"policy":"RESEARCH_ONLY_NO_BET","predictions":[{"status":"MODEL_READY","match_id":"1","player_a":"A","player_b":"B","v3_2_p_a":0.6}]}
def quote(a,b):
    return [{"match_id":"1","player_a":"A","player_b":"B","verified_pair":True,"market":"H2H_FULL_MATCH","observed_at_utc":"2026-10-14T18:59:00Z","odds_a":a,"odds_b":b,"operator":"test"}]
class StatusTests(unittest.TestCase):
    def test_positive_ev(self):
        row=build(MODEL,quote(2.0,2.0),NOW)[0]
        self.assertEqual(row["status"],"VALUE")
        self.assertEqual(row["decision"],"SHADOW_ONLY")
    def test_no_value(self):
        self.assertEqual(build(MODEL,quote(1.4,2.0),NOW)[0]["status"],"NO VALUE")
    def test_missing_odds(self):
        self.assertEqual(build(MODEL,[],NOW)[0]["status"],"BLOCKED")
    def test_old_status_compatibility(self):
        m={"policy":"RESEARCH_ONLY_NO_BET","predictions":[dict(MODEL["predictions"][0],status="SCHEDULE_PENDING_NO_BET")]}
        self.assertEqual(build(m,quote(2,2),NOW)[0]["status"],"VALUE")
if __name__=="__main__": unittest.main()
