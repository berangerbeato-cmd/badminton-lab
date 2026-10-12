#!/usr/bin/env python3
"""Probe explicit men's singles draw URLs on official BWF sites.

Research-only: no player pairing extraction and no betting decisions.
"""
import argparse
import json
import re
from html.parser import HTMLParser
import urllib.parse
import urllib.request
from pathlib import Path
from official_draw_sources import UA, allowed_official_url

LIMIT = 2_000_000


def ms_draw_url(tournament_url):
    if not allowed_official_url(tournament_url):
        return None
    parsed = urllib.parse.urlsplit(tournament_url)
    path = parsed.path.rstrip("/") + "/draws/draw/ms"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))



class ScriptEvidence(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = set()
        self.inline_scripts = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        props = dict(attrs)
        if props.get("src"):
            self.sources.add(props["src"])
        self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.inline_scripts.append(data)


def javascript_evidence(html, base_url):
    parser = ScriptEvidence()
    parser.feed(html)
    inline = "\n".join(parser.inline_scripts)
    urls = sorted(set(urllib.parse.urljoin(base_url, x) for x in parser.sources
                      if x.startswith(("/", "https://"))))
    # Extract only non-sensitive endpoint *hints*, not raw page scripts.
    endpoint_hints = sorted(set(re.findall(
        r"""["']([^"'\\s]{1,180}(?:/api/|/wp-json/|graphql|/draws/)[^"'\\s]{0,180})["']""",
        inline, re.I)))
    return {"script_count": len(parser.sources) + len(parser.inline_scripts),
            "script_urls": urls[:20],
            "endpoint_hints": endpoint_hints[:30],
            "inline_mentions_fetch": len(re.findall(r"\bfetch\s*\(", inline)),
            "inline_mentions_ajax": len(re.findall(r"ajax|axios|XMLHttpRequest", inline, re.I)),
            "inline_mentions_draw_data": len(re.findall(r"drawData|drawsData|drawTable|matchData", inline, re.I))}

def probe(url, timeout=12):
    if not url:
        return {"status": "INVALID_URL"}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            if not allowed_official_url(final_url):
                return {"status": "UNEXPECTED_REDIRECT"}
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                return {"status": "NON_HTML", "final_url": final_url}
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                return {"status": "TOO_LARGE", "final_url": final_url}
            html = data.decode("utf-8", errors="replace")
            player_urls = sorted(set(re.findall(r"""href=["\x27]([^"\x27]*?/players?/[^"\x27]+)""", html, re.I)))
            structural_tokens = sorted(set(re.findall(r"""(?:class|id)=["\x27]([^"\x27]*(?:draw|match|bracket|player)[^"\x27]*)""", html, re.I)))
            # Do not confuse a generic HTML page with an actual published MS draw.
            return {"status": "PAGE_ACCESSIBLE", "final_url": final_url,
                    "html_bytes": len(data),
                    "explicit_ms_url": urllib.parse.urlsplit(final_url).path.rstrip("/").endswith("/draw/ms"),
                    "contains_draw_table": "drawTable" in html,
                    "contains_match_draw_link": "matchDrawLink" in html,
                    "contains_single_draw_link": "singleDrawLink" in html,
                    "player_link_count": len(player_urls),
                    "sample_player_links": player_urls[:8],
                    "draw_structural_tokens": structural_tokens[:25],
                    "player_id_mentions": len(re.findall(r"player[_-]?id", html, re.I)),
                    "match_id_mentions": len(re.findall(r"match[_-]?id", html, re.I)),
                    "javascript_evidence": javascript_evidence(html, final_url)}
    except Exception as exc:
        return {"status": "FETCH_ERROR", "error_type": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    parser.add_argument("--out", type=Path, default=Path("official_ms_draw_probe.json"))
    args = parser.parse_args()
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    rows = []
    for t in coverage.get("tournaments", []):
        if not t.get("needs_secondary_source"):
            continue
        url = ms_draw_url(t.get("official_tournament_url"))
        rows.append({"id": t.get("tournament_id"), "name": t.get("name"),
                     "requested_url": url, "probe": probe(url)})
    args.out.write_text(json.dumps({"policy": "RESEARCH_ONLY_NO_BET",
                                    "tournaments": rows}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
