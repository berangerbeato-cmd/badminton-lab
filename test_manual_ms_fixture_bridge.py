import unittest
from types import SimpleNamespace
from manual_ms_fixture_bridge import build, normalize


class ManualFixtureBridgeTests(unittest.TestCase):
    def test_name_order_and_accents(self):
        self.assertEqual(normalize("Chou Tien-Chên"), normalize("Tien Chen Chou"))

    def test_known_unique_players(self):
        matches = [SimpleNamespace(p1_id="1", p1_name="Alex Lanier", p2_id="2", p2_name="Kidambi Srikanth")]
        result = build(matches, {"fixtures": [{"players": ["Lanier Alex", "Kidambi Srikanth"]}]})
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["blocked_count"], 0)
        self.assertEqual(result["schedule_pending"][0]["source"], "USER_CAPTURE_UNVERIFIED")

    def test_ambiguous_identity_blocked(self):
        matches = [
            SimpleNamespace(p1_id="1", p1_name="Victor Lai", p2_id="2", p2_name="Other"),
            SimpleNamespace(p1_id="3", p1_name="Victor Lai", p2_id="4", p2_name="Else"),
        ]
        result = build(matches, {"fixtures": [{"players": ["Victor Lai", "Other"]}]})
        self.assertEqual(result["matched_count"], 0)
        self.assertEqual(result["blocked"][0]["reason"], "PLAYER_ID_NOT_UNIQUE")

    def test_pinned_identity(self):
        matches = [SimpleNamespace(p1_id="94151", p1_name="Victor Lai", p2_id="7", p2_name="Jia Heng Teh"), SimpleNamespace(p1_id="81363", p1_name="Victor Lai", p2_id="8", p2_name="Other")]
        result = build(matches, {"fixtures": [{"players": ["Victor Lai", "Jia Heng Teh"], "player_ids": {"Victor Lai": "94151"}}]})
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["schedule_pending"][0]["players"][0]["id"], "94151")

    def test_invalid_pin(self):
        matches = [SimpleNamespace(p1_id="81363", p1_name="Victor Lai", p2_id="7", p2_name="Jia Heng Teh")]
        result = build(matches, {"fixtures": [{"players": ["Victor Lai", "Jia Heng Teh"], "player_ids": {"Victor Lai": "94151"}}]})
        self.assertEqual(result["blocked"][0]["reason"], "PINNED_ID_NOT_IN_HISTORY")


if __name__ == "__main__":
    unittest.main()
