import copy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
from pathlib import Path

import aiscore_fixture_audit as audit

NOW = datetime(2026, 10, 11, 17, tzinfo=timezone.utc)
URL = audit.BASE + "/badminton/match-alex-lanier-li-shifeng/abc123"
EVENT = {"@type": "SportsEvent", "sport": "Badminton", "eventStatus": "EventScheduled",
         "homeTeam": {"name": "Alex Lanier"}, "awayTeam": {"name": "Li Shifeng"},
         "location": [{"name": "Denmark Open, Singles"}], "startDate": "2026-10-13T04:00:00-04:00"}
TOURNAMENTS = [{"id": 5210, "name": "VICTOR Denmark Open 2026",
                "category": "HSBC BWF World Tour Super 750",
                "start_date": "2026-10-13 00:00:00", "end_date": "2026-10-18 00:00:00"}]


def page(event):
    return '<script type="application/ld+json">' + json.dumps(event) + '</script>'


def identities():
    result = {}
    for name, pid in [("Alex LANIER", "94580"), ("LI Shi Feng", "95476"), ("Shi Feng LI", "95476")]:
        for key in audit.name_keys(name):
            result.setdefault(key, set()).add(pid)
    return result


def fixture_html(section="Alex Lanier Fixture", day="2026/10/13"):
    return f'''<h2>{section}</h2><div itemscope itemtype="http://schema.org/SportsEvent">
      <meta itemprop="location" content="Denmark Open, Singles">
      <meta itemprop="startDate" content="2026-10-12T12:00:00-04:00">
      <div itemprop="startDate">{day}</div><a itemprop="url" href="{URL}">
      <div itemprop="homeTeam">Alex Lanier</div><div itemprop="awayTeam">Li Shifeng</div>
      </a></div>'''


class FixtureAuditTests(unittest.TestCase):
    def row(self):
        return audit.parse_match(page(EVENT), URL, NOW, 14)

    def test_source_time_never_becomes_verified_kickoff(self):
        row = self.row()
        self.assertEqual(row["source_date"], "2026-10-13")
        self.assertEqual(row["source_start_raw"], EVENT["startDate"])
        self.assertIsNone(row["start_utc"])
        self.assertFalse(row["kickoff_verified"])
        self.assertEqual(row["decision"], "NO_BET")

    def test_old_cancelled_women_doubles_and_naive_dates_are_rejected(self):
        for change in [dict(startDate="2026-09-13T04:00:00-04:00"), dict(startDate="2026-11-13T04:00:00-04:00"),
                       dict(startDate="2026-10-13T04:00:00"), dict(eventStatus="EventCancelled"),
                       dict(location=[{"name": "Denmark Open, Women Singles"}]),
                       dict(location=[{"name": "Denmark Open, Doubles"}]),
                       dict(awayTeam={"name": "Qualifier 1"}), dict(awayTeam={"name": "Li Shifeng / Partner"})]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                audit.parse_match(page(dict(EVENT, **change)), URL, NOW, 14)

    def test_elapsed_same_day_time_and_invalid_names_are_rejected(self):
        for change in [dict(startDate="2026-10-11T08:00:00+00:00"), dict(homeTeam={"name": 123})]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                audit.parse_match(page(dict(EVENT, **change)), URL, NOW, 14)

    def test_ambiguous_structured_events_are_not_guessed(self):
        other = dict(EVENT, awayTeam={"name": "Other Player"})
        with self.assertRaises(ValueError):
            audit.parse_match(page(EVENT) + page(other), URL, NOW, 14)

    def test_historical_h2h_rows_cannot_create_future_fixture(self):
        self.assertEqual(audit.fixture_links(fixture_html("Past H2H Results"), NOW, 14), {})
        self.assertEqual(audit.fixture_links(fixture_html(day="2026/09/13"), NOW, 14), {})

    def test_visible_date_keeps_conflicting_discovery_metadata(self):
        links = audit.fixture_links(fixture_html(), NOW, 14)
        self.assertEqual(links[URL][0]["source_date"], "2026-10-13")
        self.assertEqual(links[URL][0]["metadata_start_raw"], "2026-10-12T12:00:00-04:00")

    def test_identity_spacing_order_and_ambiguity(self):
        index = identities()
        self.assertEqual(audit.resolve_player("Li Shifeng", index), "95476")
        self.assertEqual(audit.resolve_player("LANIER Alex", index), "94580")
        self.assertIsNone(audit.resolve_player("L. Shifeng", index))
        self.assertIsNone(audit.resolve_player("Alex Lainer", index))
        for key in audit.name_keys("Alex Lanier"):
            index[key].add("99999")
        self.assertIsNone(audit.resolve_player("Alex Lanier", index))

    def test_additional_fixture_requires_players_and_tournament(self):
        row = audit.compare_fixture(self.row(), identities(), [], TOURNAMENTS, {})
        self.assertEqual(row["comparison"], "ADDITIONAL_TO_BWF")
        self.assertEqual(row["player_b_bwf_id"], "95476")
        self.assertEqual(row["bwf_tournament_id"], "5210")
        self.assertEqual(row["bwf_match_ids"], [])
        for index, tournaments in [({}, TOURNAMENTS), (identities(), []), (identities(), TOURNAMENTS * 2)]:
            with self.subTest(index=index, tournaments=tournaments):
                self.assertEqual(audit.compare_fixture(self.row(), index, [], tournaments, {})["comparison"], "QUARANTINED")

    def test_reverse_order_matches_same_bwf_fixture(self):
        m = {"id": 123, "winner": None, "start_time": int(datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp()),
             "t1p1_detail": {"id": 95476}, "t2p1_detail": {"id": 94580}}
        row = audit.compare_fixture(self.row(), identities(), [("5210", m)], TOURNAMENTS, {})
        self.assertEqual(row["comparison"], "ALSO_IN_BWF")
        self.assertEqual(row["bwf_match_ids"], ["5210:123"])
        m["winner"] = 1
        row = audit.compare_fixture(self.row(), identities(), [("5210", m)], TOURNAMENTS, {})
        self.assertIn("ALREADY_COMPLETED_IN_BWF", row["blockers"])
        self.assertEqual(row["comparison"], "QUARANTINED")

    def test_source_and_bwf_date_conflicts_are_quarantined(self):
        observations = audit.fixture_links(fixture_html(day="2026/10/14"), NOW, 14)
        row = audit.compare_fixture(self.row(), identities(), [], TOURNAMENTS, observations)
        self.assertIn("SOURCE_DATE_CONFLICT", row["blockers"])
        m = {"id": 123, "winner": None, "start_time": int(datetime(2026, 10, 14, tzinfo=timezone.utc).timestamp()),
             "t1p1_detail": {"id": 94580}, "t2p1_detail": {"id": 95476}}
        row = audit.compare_fixture(self.row(), identities(), [("5210", m)], TOURNAMENTS, {})
        self.assertIn("BWF_DATE_CONFLICT", row["blockers"])

    def test_links_do_not_escape_public_source(self):
        for url in ["https://example.org/badminton/match-a-b/abc", "https://www.aiscore.com.evil/badminton",
                    "https://www.aiscore.com@evil.example/badminton", "http://www.aiscore.com/badminton",
                    "https://www.aiscore.com/badminton?key=value"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                audit.public_url(url)

    def test_multiple_source_ids_do_not_inflate_coverage(self):
        row = audit.compare_fixture(self.row(), identities(), [], TOURNAMENTS, {})
        duplicate = copy.deepcopy(row)
        duplicate["source_match_id"] = "other123"
        duplicate["player_a_bwf_id"], duplicate["player_b_bwf_id"] = row["player_b_bwf_id"], row["player_a_bwf_id"]
        audit.quarantine_duplicate_pairings([row, duplicate])
        self.assertEqual(row["comparison"], "QUARANTINED")
        self.assertEqual(duplicate["comparison"], "QUARANTINED")

    def test_access_block_stops_collection_without_fallback(self):
        class BlockedClient:
            pages = []
            request_count = 0
            def get(self, url):
                self.request_count += 1
                if url == audit.HOME:
                    return '<a href="/head-to-head/badminton/alex-lanier-vs-li-shifeng">Match</a>'
                raise audit.AccessStopped("HTTP 403: source access stopped")
        client = BlockedClient()
        with patch.object(audit, "player_index", return_value=(identities(), [])), \
             patch.object(audit, "read_json", side_effect=lambda p: {} if p.name == "bwf_refresh_latest.json" else TOURNAMENTS):
            report = audit.collect(Path("missing-test-data"), NOW, 14, client)
        self.assertEqual(client.request_count, 2)
        self.assertEqual(report["collection_status"], "PARTIAL_OR_FAILED")
        self.assertEqual(report["fixtures"], [])
        self.assertIn("403", report["errors"][0]["error"])

    def test_redirect_rejected_before_following_another_host(self):
        handler = audit.SameHostRedirect()
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://example.org/private")


if __name__ == "__main__":
    unittest.main()
