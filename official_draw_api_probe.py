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
