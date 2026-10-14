import unittest
from shadow_portfolio import selections, append_once, settle, summary

class ShadowPortfolioTests(unittest.TestCase):
    def sample(self, ev=8):
        return {"status":"VALUE","match_id":"42","player_a":"Alpha","player_b":"Beta",
                "p_a":0.6,"p_b":0.4,"odds_a":1.8,"odds_b":2.0,
                "ev_a_pct":ev,"ev_b_pct":-20,"operator":"test",
                "observed_at_utc":"2026-10-14T18:00:00Z"}
    def test_threshold_strict(self):
        self.assertEqual(selections([self.sample(5)],"2026-10-14T18:05:00Z","2026-10-15","Denmark"),[])
        self.assertEqual(len(selections([self.sample(5.01)],"2026-10-14T18:05:00Z","2026-10-15","Denmark")),1)
    def test_fixed_stake_and_idempotence(self):
        new=selections([self.sample()],"2026-10-14T18:05:00Z","2026-10-15","Denmark")
        ledger=append_once([],new)
        append_once(ledger,new)
        self.assertEqual(len(ledger),1)
        self.assertEqual(ledger[0]["stake_eur"],10.0)
    def test_verified_win_and_loss(self):
        first=selections([self.sample()],"2026-10-14T18:05:00Z","2026-10-15","Denmark")[0]
        settle([first],{first["selection_id"]:{"winner":"Alpha","verified":False}})
        self.assertEqual(first["status"],"OPEN")
        settle([first],{first["selection_id"]:{"winner":"Alpha","verified":True,"source":"BWF"}})
        self.assertEqual(first["net_eur"],8.0)
        self.assertEqual(summary([first])["roi_pct"],80.0)
        settle([first],{first["selection_id"]:{"winner":"Beta","verified":True}})
        self.assertEqual(first["status"],"WON")
    def test_same_day_cannot_be_main_portfolio(self):
        self.assertEqual(selections([self.sample()], "2026-10-15T06:00:00Z", "2026-10-15", "Denmark"), [])
        self.assertEqual(selections([self.sample()], "2026-10-14T18:05:00Z", "2026-10-14", "Denmark"), [])
    def test_no_fake_roi(self):
        self.assertIsNone(summary([])["roi_pct"])
if __name__=="__main__": unittest.main()
