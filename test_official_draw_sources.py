import unittest
from unittest.mock import patch
from official_draw_sources import allowed_official_url, build


class OfficialSourceTests(unittest.TestCase):
    def test_official_url(self):
        self.assertTrue(allowed_official_url("https://bwfbadminton.com/tournament/5210/victor-denmark-open-2026"))

    def test_reject_untrusted_domains(self):
        for url in ("https://bwfbadminton.com.evil.test/tournament/5210",
                    "http://bwfbadminton.com/tournament/5210",
                    "https://example.com/tournament/5210",
                    "https://bwfbadminton.com@evil.test/tournament/5210"):
            self.assertFalse(allowed_official_url(url))

    def test_no_network_when_probe_limit_zero(self):
        with patch("official_draw_sources.probe", side_effect=AssertionError("network")):
            result = build({"tournaments": [{"tournament_id": "5210",
                "name": "Denmark Open", "needs_secondary_source": True,
                "official_tournament_url": "https://bwfbadminton.com/tournament/5210"}]}, 0)
        self.assertEqual(result["sources"][0]["result"]["status"], "NOT_PROBED_LIMIT")


if __name__ == "__main__":
    unittest.main()
