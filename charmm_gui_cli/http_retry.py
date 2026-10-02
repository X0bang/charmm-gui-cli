"""Bounded retries for read-only GETs; never replay POSTs or uploads."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time

import requests

RETRY_STATUSES = frozenset((429, 502, 503, 504))
MAX_ATTEMPTS = 3
MAX_DELAY = 30.0


def transient_exception(error):
    return isinstance(error, (requests.ConnectionError, requests.Timeout,
                              requests.exceptions.ChunkedEncodingError)) and not isinstance(error, requests.exceptions.SSLError)


def retry_delay(value, attempt):
    fallback = float(2 ** attempt)
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delay = (when - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = fallback
    if not math.isfinite(delay):
        delay = fallback
    return max(0.0, delay)


def request(session, method, url, **kwargs):
    """Return final response or raise final transport error; callers classify it."""
    attempts = MAX_ATTEMPTS if method.upper() == "GET" else 1
    for attempt in range(attempts):
        try:
            response = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            # Certificate failures are not transient connectivity problems.
            if not transient_exception(exc) or attempt + 1 == attempts:
                raise
            time.sleep(retry_delay(None, attempt))
            continue
        if response.status_code not in RETRY_STATUSES or attempt + 1 == attempts:
            return response
        delay = retry_delay(response.headers.get("Retry-After"), attempt)
        # Never shorten a server-requested minimum wait. A long Retry-After
        # returns control to the resumable caller instead of blocking here.
        if delay > MAX_DELAY:
            return response
        response.close()
        time.sleep(delay)
