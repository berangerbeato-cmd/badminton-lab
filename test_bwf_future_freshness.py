#!/usr/bin/env python3
"""Regression checks for unpublished future BWF tournament draws."""
from datetime import datetime, timedelta, timezone
from bwf_pipeline_freshness import validate

now = datetime.now(timezone.utc)
stamp = lambda d: d.isoformat()
model = {"generated_at_utc": stamp(now)}
good = {"status": "REFRESHED", "checked_at_utc": stamp(now - timedelta(minutes=2))}
empty = {"status": "EMPTY_RESPONSE", "checked_at_utc": stamp(now - timedelta(minutes=1))}
failed = {"status": "REFRESH_FAILED", "checked_at_utc": stamp(now - timedelta(minutes=1))}
def check(items, errors=None, accepted=False):
    try:
        result = validate({"tournament_refreshes": items, "errors": errors or []}, model)
        assert accepted and result["status"] == "ORDER_VALIDATED"
    except ValueError:
        assert not accepted

check([good, empty], accepted=True)
check([empty], accepted=False)
check([good, failed], accepted=False)
check([good], errors=["HTTP failure"], accepted=False)
check([good], accepted=True)
print("BWF future-draw freshness regression checks passed")
