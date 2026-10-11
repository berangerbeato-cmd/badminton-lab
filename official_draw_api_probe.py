#!/usr/bin/env python3
"""Read-only BWF public draw API connectivity audit. No fixtures are imported."""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from official_draw_sources import UA

BASE = "https://extranet-lv.bwfbadminton.com"
PATH = "/api/vue-tournament-draws"
MAX_BYTES = 400_000


def probe(tournament_id):
    query = urllib.parse.urlencode({"tmtId": str(tournament_id), "tmtType": "0"})
    url = BASE + PATH + "?" + query
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        # Do not follow redirects to unrelated hosts.
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(req, timeout=12) as response:
            body = response.read(MAX_BYTES + 1)
            result = {"http_status": response.status, "content_type": response.headers.get("Content-Type", "")}
            if len(body) > MAX_BYTES:
                result["error"] = "response_too_large"
                return result
            try:
                data = json.loads(body)
                result["json_type"] = type(data).__name__
                if isinstance(data, dict):
                    result["keys"] = list(data.keys())[:20]
                    results = data.get("results")
                    result["results_count"] = len(results) if isinstance(results, list) else None
                    if isinstance(results, list) and results and isinstance(results[0], dict):
                        result["result_field_names"] = list(results[0].keys())[:20]
                    if isinstance(results, list):
                        result["draws"] = [
                            {"value": item.get("value"), "text": item.get("text"),
                             "type": item.get("type"), "doubles": item.get("doubles"),
                             "stage_type": item.get("stage_type")}
                            for item in results[:20] if isinstance(item, dict)
                        ]
                        result["ms_draw_details"] = []
                        for item in results:
                            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                                continue
                            label = item["text"].strip().upper()
                            if label != "MS" and not label.startswith("MS -"):
                                continue
                            draw_id = item.get("value")
                            if not str(draw_id).isdigit():
                                continue
                            endpoint = "/api/vue-tournament-draw-data"
                            params = {"tmtTab": "draw", "tmtId": str(tournament_id),
                                      "tmtType": "0", "drawId": str(draw_id), "isPara": "false"}
                            details_url = BASE + endpoint + "?" + urllib.parse.urlencode(params)
                            details_req = urllib.request.Request(
                                details_url, headers={"User-Agent": UA, "Accept": "application/json"})
                            detail = {"draw_id": str(draw_id), "label": label}
                            try:
                                with opener.open(details_req, timeout=12) as draw_response:
                                    payload = draw_response.read(MAX_BYTES + 1)
                                    detail["http_status"] = draw_response.status
                                    if len(payload) > MAX_BYTES:
                                        detail["error"] = "response_too_large"
                                    else:
                                        draw_data = json.loads(payload)
                                        detail["json_type"] = type(draw_data).__name__
                                        if isinstance(draw_data, dict):
                                            detail["keys"] = list(draw_data.keys())[:20]
                                            matches = draw_data.get("matches")
                                            if isinstance(matches, list):
                                                detail["match_audit"] = {
                                                    "total": len(matches),
                                                    "with_two_teams": sum(
                                                        bool(m.get("team1")) and bool(m.get("team2"))
                                                        for m in matches if isinstance(m, dict)),
                                                    "with_match_time_utc": sum(
                                                        bool(m.get("matchTimeUtc"))
                                                        for m in matches if isinstance(m, dict)),
                                                    "statuses": {},
                                                    "team1_types": {},
                                                }
                                                audit = detail["match_audit"]
                                                audit["two_identified_single_players"] = 0
                                                audit["two_identified_pending_without_time"] = 0
                                                audit["two_identified_with_time"] = 0
                                                audit["pending_status_codes"] = {}
                                                audit["candidate_samples"] = []
                                                for match in matches:
                                                    if not isinstance(match, dict):
                                                        continue
                                                    sides = []
                                                    for side in ("team1", "team2"):
                                                        team = match.get(side)
                                                        players = team.get("players") if isinstance(team, dict) else None
                                                        player = players[0] if isinstance(players, list) and len(players) == 1 else None
                                                        if (isinstance(player, dict) and str(player.get("id") or "").isdigit()
                                                                and isinstance(player.get("nameDisplay"), str)
                                                                and player["nameDisplay"].strip()):
                                                            sides.append({"id": str(player["id"]), "name": player["nameDisplay"]})
                                                    if len(sides) == 2 and sides[0]["id"] != sides[1]["id"]:
                                                        audit["two_identified_single_players"] += 1
                                                        if match.get("matchTimeUtc"):
                                                            audit["two_identified_with_time"] += 1
                                                        if match.get("matchStatus") == "N" and not match.get("matchTimeUtc"):
                                                            audit["two_identified_pending_without_time"] += 1
                                                            if len(audit["candidate_samples"]) < 5:
                                                                audit["candidate_samples"].append({
                                                                    "match_id": match.get("id"),
                                                                    "players": sides,
                                                                    "round": match.get("roundName"),
                                                                    "match_time": match.get("matchTime"),
                                                                })
                                                    status = str(match.get("matchStatus") or "missing")
                                                    audit["statuses"][status] = audit["statuses"].get(status, 0) + 1
                                                    kind = type(match.get("team1")).__name__
                                                    audit["team1_types"][kind] = audit["team1_types"].get(kind, 0) + 1
                                                for match in matches[:2]:
                                                    if isinstance(match, dict):
                                                        detail.setdefault("sample_match_shapes", []).append({
                                                            "team1": match.get("team1"),
                                                            "team2": match.get("team2"),
                                                            "matchTime": match.get("matchTime"),
                                                            "matchTimeUtc": match.get("matchTimeUtc"),
                                                            "matchStatus": match.get("matchStatus"),
                                                            "roundName": match.get("roundName"),
                                                        })
                                            for key in ("results", "matches", "drawsize"):
                                                value = draw_data.get(key)
                                                detail[key + "_count"] = len(value) if isinstance(value, list) else None
                                                if isinstance(value, list) and value:
                                                    first = value[0]
                                                    detail[key + "_first_type"] = type(first).__name__
                                                    if isinstance(first, dict):
                                                        detail[key + "_first_fields"] = list(first.keys())[:25]
                            except urllib.error.HTTPError as exc:
                                detail.update({"http_status": exc.code, "error": "http_error"})
                            except Exception as exc:
                                detail["error"] = type(exc).__name__
                            result["ms_draw_details"].append(detail)
                        result["ms_draw_candidates"] = [
                            item.get("value") for item in results
                            if isinstance(item, dict)
                            and isinstance(item.get("text"), str)
                            and ("men's singles" in item["text"].lower()
                                 or "mens singles" in item["text"].lower()
                                 or item["text"].strip().lower() in ("ms", "men singles"))
                        ]
            except (ValueError, UnicodeError):
                result["error"] = "non_json_response"
            return result
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "error": "http_error"}
    except Exception as exc:
        return {"error": type(exc).__name__}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    parser.add_argument("--out", type=Path, default=Path("official_draw_api_probe.json"))
    args = parser.parse_args()
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    rows = []
    for tournament in coverage.get("tournaments", []):
        if not tournament.get("needs_secondary_source") or len(rows) >= 4:
            continue
        tid = tournament.get("tournament_id")
        if not str(tid).isdigit():
            continue
        rows.append({"tournament_id": str(tid), **probe(tid)})
    result = {"policy": "RESEARCH_ONLY_NO_BET", "endpoint": BASE + PATH,
              "note": "tmtType=0 is a probe assumption, not a confirmed tournament type",
              "tournaments": rows}
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
