import unittest
from datetime import datetime, timezone, timedelta
from v32_official_fixture_bridge import build

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
BASE = {"match_id": "123", "tournament_id": "5210", "draw_id": "1",
        "players": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}],
        "source": "BWF_OFFICIAL_DRAW"}

class BridgeTests(unittest.TestCase):
    def test_missing_time_blocked(self):
        result = build({"candidates": [BASE]}, NOW)
        self.assertEqual((result["eligible_count"], result["blocked_count"]), (0, 1))

    def test_verified_future_official_time(self):
        row = dict(BASE, match_time_utc=(NOW + timedelta(days=1)).isoformat(),
                   date_verified=True, time_verified=True, schedule_source="BWF_OFFICIAL_OOP")
        self.assertEqual(build({"candidates": [row]}, NOW)["eligible_count"], 1)

    def test_unverified_or_external_source_blocked(self):
        row = dict(BASE, match_time_utc=(NOW + timedelta(days=1)).isoformat(),
                   date_verified=True, time_verified=True, schedule_source="UNVERIFIED")
        self.assertEqual(build({"candidates": [row]}, NOW)["eligible_count"], 0)

    def test_past_match_blocked(self):
        row = dict(BASE, match_time_utc=(NOW - timedelta(days=1)).isoformat(),
                   date_verified=True, time_verified=True, schedule_source="BWF_OFFICIAL_OOP")
        self.assertEqual(build({"candidates": [row]}, NOW)["eligible_count"], 0)

    def test_deduplicate(self):
        self.assertEqual(build({"candidates": [BASE, BASE]}, NOW)["blocked_count"], 1)

if __name__ == "__main__":
    unittest.main()
