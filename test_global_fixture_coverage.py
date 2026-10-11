import unittest
from datetime import date
from global_fixture_coverage import parse_date, tournament_window


class TournamentWindowTests(unittest.TestCase):
    def test_iso_dates(self):
        self.assertEqual(tournament_window({"startDate": "2026-10-13", "endDate": "2026-10-18"}), (date(2026, 10, 13), date(2026, 10, 18)))

    def test_iso_timestamp(self):
        self.assertEqual(parse_date("2026-10-13T00:00:00Z"), date(2026, 10, 13))

    def test_missing_dates_not_guessed(self):
        self.assertIsNone(tournament_window({"name": "Denmark Open 2026"}))

    def test_conflicting_dates_rejected(self):
        self.assertIsNone(tournament_window({"startDate": "2026-10-13", "start_date": "2026-10-14", "endDate": "2026-10-18"}))

    def test_reversed_dates_rejected(self):
        self.assertIsNone(tournament_window({"startDate": "2026-10-18", "endDate": "2026-10-13"}))

    def test_numeric_timestamps_not_guessed(self):
        self.assertIsNone(parse_date(1791849600))


if __name__ == "__main__":
    unittest.main()
