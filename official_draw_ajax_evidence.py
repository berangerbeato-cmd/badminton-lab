#!/usr/bin/env python3
"""Inspect official BWF draw JavaScript for publicly referenced AJAX endpoints.

Read-only research. Never invoke discovered endpoints or import matches.
"""
import argparse
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from official_draw_sources import UA, allowed_official_url
from official_ms_draw_probe import ms_draw_url

MAX_JS_BYTES = 750_000
MAX_SCRIPTS = 12


class ScriptLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.inline = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            src = dict(attrs).get("src")
            if src:
                self.links.append(src)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and data.strip():
            self.inline.append(data)


def allowed_official_script_url(url):
    """Only HTTPS JavaScript assets hosted on the official BWF sites."""
    if not isinstance(url, str):
        return False
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https"
            and parsed.hostname in {"bwfbadminton.com", "www.bwfbadminton.com",
                                    "bwfworldtour.bwfbadminton.com"}
            and not parsed.username and not parsed.password
            and parsed.path.startswith("/wp-content/")
            and parsed.path.lower().endswith(".js"))


def candidate_scripts(html, base):
    parser = ScriptLinks()
    parser.feed(html)
    urls = []
    for src in parser.links:
        url = urllib.parse.urljoin(base, src)
        if not allowed_official_script_url(url):
            continue
        path = urllib.parse.urlsplit(url).path.lower()
        if path.endswith(".js") or ".js?" in url.lower():
            urls.append(url)
    unique = list(dict.fromkeys(urls))
    priority = ("draw", "match", "tournament", "result", "app", "main", "functions")
    unique.sort(key=lambda u: (not any(p in urllib.parse.urlsplit(u).path.lower() for p in priority),
                               not any(p in urllib.parse.urlsplit(u).path.lower() for p in ("tournament", "result", "draw")),
                               u))
    return unique[:MAX_SCRIPTS]


def endpoint_evidence(script):
    # Extract small, non-sensitive code snippets for a human to inspect.
    patterns = (r"\$\.ajax\s*\(", r"\$\.get(?:JSON)?\s*\(",
                r"axios\.(?:get|post)\s*\(", r"fetch\s*\(",
                r"ajaxurl", r"admin-ajax\.php", r"wp-json", r"graphql",
                r"ajaxTmt", r"loadResultContent", r"drawTable", r"child-match",
                r"drawTableRoundRobin", r"drawTableKnockout", r"drawTable(?:Data)?\s*[:=]",
                r"axios\s*\(", r"\\.get\s*\(", r"\\.post\s*\(",
                r"mounted\s*\(", r"created\s*\(", r"api[_/-]", r"endpoint")
    results = []
    for pattern in patterns:
        for match in list(re.finditer(pattern, script, re.I))[:12]:
            start, end = max(0, match.start() - 90), min(len(script), match.end() + 170)
            snippet = re.sub(r"\s+", " ", script[start:end])
            results.append({"kind": pattern, "snippet": snippet[:260]})
    return results[:60]


def read_html(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=12) as response:
        if not ((allowed_official_url(url) and allowed_official_url(response.geturl()))
                or (allowed_official_script_url(url) and allowed_official_script_url(response.geturl()))):
            raise ValueError("Unexpected redirect")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Response too large")
        return data.decode("utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage", type=Path, default=Path("global_coverage.json"))
    ap.add_argument("--out", type=Path, default=Path("official_draw_ajax_evidence.json"))
    ap.add_argument("--max-tournaments", type=int, default=4)
    args = ap.parse_args()
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    rows = []
    for tournament in coverage.get("tournaments", []):
        if not tournament.get("needs_secondary_source") or len(rows) >= args.max_tournaments:
            continue
        url = ms_draw_url(tournament.get("official_tournament_url"))
        entry = {"id": tournament.get("tournament_id"), "ms_url": url, "scripts": []}
        try:
            html = read_html(url, 2_000_000)
            selected = candidate_scripts(html, url)
            entry["selected_script_count"] = len(selected)
            parser = ScriptLinks()
            parser.feed(html)
            entry["inline_script_blocks"] = len(parser.inline)
            entry["inline_ajax_evidence"] = endpoint_evidence("\n".join(parser.inline))[:60]
            entry["inline_data_context"] = [
                {"term": term, "snippets": [re.sub(r"\\s+", " ", block[max(0, m.start()-130):m.end()+220])[:350]
                    for block in parser.inline for m in list(re.finditer(re.escape(term), block, re.I))[:3]][:6]}
                for term in ("drawTableRoundRobin", "drawTable", "axios", "ajaxTmt", "matchData")
            ]
            entry["draw_markup_evidence"] = endpoint_evidence(html)[:35]
            entry["script_tag_count"] = html.lower().count("<script")
            entry["script_src_count"] = len(re.findall(r"<script[^>]+src\s*=", html, re.I))
            entry["script_source_samples"] = re.findall(r"""<script[^>]+src\s*=\s*[\x27\x22]([^\x27\x22]+)""", html, re.I)[:8]
            for js_url in selected:
                item = {"url": js_url}
                try:
                    script = read_html(js_url, MAX_JS_BYTES)
                    item["evidence"] = endpoint_evidence(script)
                except Exception as exc:
                    item["error_type"] = type(exc).__name__
                entry["scripts"].append(item)
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
        rows.append(entry)
    args.out.write_text(json.dumps({"policy": "RESEARCH_ONLY_NO_BET", "tournaments": rows},
                                   indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
