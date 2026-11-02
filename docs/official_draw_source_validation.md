# Official draw source validation (11 October 2026)

## Problem

The BWF historical-match feed may return no upcoming fixtures even after a tournament draw is publicly announced. AiScore's initial pilot found pairings, but the GitHub Actions manual run 38160927372 failed with HTTP 403. Do not retry through proxy, spoofing or anti-bot bypass.

## Independently published evidence

The Denmark Open organizer states that the 2026 draw has been published and names two men's singles first-round matchups:

- Alex Lanier vs Li Shi Feng
- Lakshya Sen vs Chou Tien Chen

Organizer article: https://denmarkopen.dk/2026/10/the-draw-for-this-years-victor-denmark-open-has-been-revealed/
Organizer schedule: https://denmarkopen.dk/tickets/schedule/

The first round is held 13-14 October 2026. Session start times are NOT individual verified match start times. Do not assign a specific match day or time solely from this article.

## Proposed safe ingestion contract

Store source URL, capture timestamp, tournament, discipline, round, two full player names, evidence text/hash, source publication date and status. Resolve both names uniquely against BWF player IDs; ambiguous IDs must be quarantined. Use statuses DRAW_CONFIRMED / SCHEDULED_VERIFIED / CONFLICT / UNKNOWN. The article above supports DRAW_CONFIRMED only. Never convert a tournament session time into a match kickoff. Keep all rows RESEARCH_ONLY_NO_BET until independent checks and odds validation.

## Implementation gates

1. Verify the organizer's complete draw link and whether it permits reliable, free, automated read access from GitHub Actions; do not assume browser success proves runner access.
2. Implement an organizer/BWF draw parser only after a successful runner-side access test. Store raw evidence and unit fixtures.
3. Compare against existing BWF fixtures using unordered player IDs and tournament ID, not string-only names.
4. Report counts: draw-known, BWF-known, additional unique pairings, ambiguous, date conflicts, and verified individual kickoffs.
5. Keep existing V3.2 predictions, odds, BWF archives and schedules unchanged until validated.

This is a documented alternative source assessment, NOT a working automated importer.
