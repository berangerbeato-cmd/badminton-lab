# Complementary fixture coverage

`aiscore_fixture_audit.py` collects publicly displayed AiScore fixtures and
compares them with the local BWF archive. It is a separate research input: it
does not write BWF archives, model forecasts, bookmaker odds or a betting ledger.

## Why this exists

The BWF synchronizer calls `vue-tournament-matches`. An empty result from that
endpoint does **not** establish that the tournament draw has not been published.
On 11 October 2026 the Denmark Open organizer publicly announced its draw,
including Lanier–Li Shi Feng and Sen–Chou Tien Chen, while our BWF match archive
for tournament 5210 was empty.

Evidence:

- [Organizer's draw announcement](https://denmarkopen.dk/2026/10/the-draw-for-this-years-victor-denmark-open-has-been-revealed/)
- [AiScore badminton discovery page](https://www.aiscore.com/badminton)
- [Example individual match page](https://www.aiscore.com/badminton/match-alex-lanier-li-shifeng/lk2d9cw8l3ltz73)

Sofascore's public schedule request and the official BWF draws page returned
access errors in this environment. No authentication, proxy or bypass was used.

## Run

```bash
python -m unittest -v test_aiscore_fixture_audit
python aiscore_fixture_audit.py --live --days 14 --out data/fixture_sources/aiscore
```

The GitHub workflow **AiScore fixture coverage audit (manual, NO BET)** runs the
offline tests for relevant pull requests. `workflow_dispatch` explicitly enables
live collection and uploads a report. There is no cron, background report,
write-back commit or automatic model promotion.

## Collection and comparison

1. Check `robots.txt`, then discover up to 20 linked badminton H2H pages from
   the public badminton home page. These links are navigation, not fixture proof.
2. Read only future rows under the visible **Fixture** headings, excluding
   historical results. Keep their dates and event URLs.
3. Fetch each linked individual match page and require one scheduled badminton
   singles `SportsEvent` with two named players and a future timestamp.
4. Map full player names to unique IDs from BWF MS archives. Normalization
   permits accents, punctuation, spacing and first/last-name order; it never
   chooses a fuzzy match, an initial-only name, an ambiguous player or a new ID.
5. Identify one in-scope BWF tournament using its name and date range. Compare
   the unordered player pair within that tournament. Retain BWF match IDs only
   when they actually exist.

Reports contain `ADDITIONAL_TO_BWF`, `ALSO_IN_BWF` and `QUARANTINED` rows, plus
blocking reasons. Multiple source IDs for the same dated pairing, conflicting
dates, unresolved identities and BWF-completed matches are quarantined.

`start_utc` is always null, `kickoff_verified` is always false, and every row is
`NO_BET`. `source_start_raw` records what AiScore said, including its offset; it
does not certify a start time. Some H2H rows have machine-readable dates different
from their visible dates. Their original metadata is retained with a warning;
the individual match page must agree with the visible discovery date, otherwise
the row is quarantined.

## Interpreting the output

`latest.json` and a timestamped snapshot include per-page fetch times and SHA-256
hashes, the comparison rows, errors and the BWF reference-refresh observations.
The BWF checkout can be older than the web observations; its audit timestamps
must accompany any coverage claim. The collector does not perform a BWF refresh.

Coverage is **bounded and not exhaustive**. It depends on which H2H links AiScore
exposes on its home page. A completed bounded scan is not a guarantee that every
match in the next 14 days was found. An empty or partial scan is not proof that
there are no matches. Unmatched names remain available for review in quarantine.

Requests are spaced by at least one second and capped at 40 by default, including
robots and failed requests. Authentication errors, HTTP 403/429, challenge pages,
robots exclusions and request limits stop collection; the partial report remains
available. Source failures return exit code 2. No paid service is needed.

## Observed pilot: 11 October 2026

The bounded scan ran from 2026-10-11T17:25:39.107544+00:00 to 2026-10-11T17:28:48.839795+00:00. It fetched 31 public pages including robots, found 9 distinct future match pages, and had no request errors. Comparison with the 406 local BWF archives found 7 additional fixtures with unique player identities and 2 quarantined fixtures. This is a coverage audit, not an additional set of model predictions.

| Source date | Match | Comparison |
|---|---|---|
| 2026-10-13 | Alex Lanier – Li Shifeng | ADDITIONAL_TO_BWF |
| 2026-10-13 | Angus Ng Ka Long – Tanaka Yushi | ADDITIONAL_TO_BWF |
| 2026-10-13 | Arnaud Merkle – Kidambi Srikanth | ADDITIONAL_TO_BWF |
| 2026-10-13 | Chou Tien-chen – Lakshya Sen | ADDITIONAL_TO_BWF |
| 2026-10-13 | Koki Watanabe – Leong Jun Hao | ADDITIONAL_TO_BWF |
| 2026-10-13 | Kunlavut Vitidsarn – Rasmus Gemke | ADDITIONAL_TO_BWF |
| 2026-10-13 | Lai Victor – Moh. Zaki Ubaidillah | QUARANTINED |
| 2026-10-13 | Nhat Nguyen – Panitchaphon Teeraratsakul | ADDITIONAL_TO_BWF |
| 2026-10-13 | Teh Jia Heng Jason – Lee Chia-hao | QUARANTINED |

The two quarantined names are meaningful: the BWF archive has two different Victor LAI records (Canada, ID 94151; USA, ID 81363), while AiScore uses `Teh Jia Heng Jason` and the BWF archive uses `Jia Heng TEH`. No identity is guessed. All nine starts remain unverified, and all nine rows remain NO_BET.

[Full observation and comparison evidence](evidence/aiscore_2026-10-11.json)
