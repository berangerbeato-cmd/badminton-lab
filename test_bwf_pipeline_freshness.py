import unittest
from bwf_pipeline_freshness import validate


class TestFreshness(unittest.TestCase):
    def setUp(self):
        self.refresh = {"errors": [], "tournament_refreshes": [
            {"status": "REFRESHED", "checked_at_utc": "2026-10-10T17:53:09+00:00"}]}
        self.model = {"generated_at_utc": "2026-10-10T17:54:00+00:00"}
        self.comparison = {"generated_at_utc": "2026-10-10T17:55:00+00:00"}

    def test_valid_order(self):
        self.assertEqual(validate(self.refresh, self.model, self.comparison)["status"], "ORDER_VALIDATED")

    def test_stale_model(self):
        self.model["generated_at_utc"] = "2026-10-10T17:25:00+00:00"
        with self.assertRaisesRegex(ValueError, "predates"):
            validate(self.refresh, self.model)

    def test_stale_comparison(self):
        self.comparison["generated_at_utc"] = "2026-10-10T17:50:00+00:00"
        with self.assertRaisesRegex(ValueError, "predates"):
            validate(self.refresh, self.model, self.comparison)

    def test_failed_refresh(self):
        self.refresh["errors"] = [{"error": "timeout"}]
        with self.assertRaises(ValueError):
            validate(self.refresh, self.model)

    def test_missing_timezone(self):
        self.model["generated_at_utc"] = "2026-10-10T17:54:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            validate(self.refresh, self.model)

    def test_partial_refresh(self):
        self.refresh["tournament_refreshes"][0]["status"] = "EMPTY_RESPONSE"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            validate(self.refresh, self.model)


if __name__ == "__main__":
    unittest.main()
