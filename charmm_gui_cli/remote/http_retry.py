"""Bounded retries for read-only GETs; never replay POSTs or uploads."""

from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
import math
import time

import requests

RETRY_STATUSES = frozenset((429, 502, 503, 504))
MAX_ATTEMPTS = 3
MAX_DELAY = 30.0
_LONG_GET_POLICY = ContextVar("long_get_retry_policy", default=None)


@contextmanager
def long_get_retries(seconds=1200, *, on_wait=None):
    """Command-scoped waiting; standalone queries retain their short retries."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 1800:
        raise ValueError("GET retry window must be from 0 to 1800 seconds")
    token = _LONG_GET_POLICY.set({"seconds": seconds, "on_wait": on_wait} if seconds else None)
    try:
        yield
    finally:
        _LONG_GET_POLICY.reset(token)


def _notify(policy, **event):
    if policy["on_wait"] is not None:
        policy["on_wait"](event)


def _wait(policy, delay, started, attempt, category):
    """Sleep in interruptible chunks; callbacks contain no HTTP data or URLs."""
    until = time.monotonic() + delay
    while True:
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        _notify(policy, event="waiting", state="waiting_network", category=category,
                retry_attempt=attempt + 1, retry_in_seconds=remaining,
                waiting_elapsed_seconds=time.monotonic() - started)
        time.sleep(min(30, remaining))


def _long_request(session, method, url, policy, **kwargs):
    started = time.monotonic()
    deadline = started + policy["seconds"]
    attempt, waited = 0, False
    try:
        while True:
            try:
                response = session.request(method, url, **kwargs)
            except requests.RequestException as error:
                delay = min(60.0, float(2 ** min(attempt, 6)))
                if not transient_exception(error) or delay >= deadline - time.monotonic():
                    raise
                waited = True
                _wait(policy, delay, started, attempt, "transient_network")
                if time.monotonic() >= deadline:
                    raise
                attempt += 1
                continue
            if response.status_code not in RETRY_STATUSES:
                return response
            # Honor a server minimum exactly, even when it exceeds the local
            # backoff cap. If outside this window, return for durable scheduling.
            requested = response.headers.get("Retry-After")
            delay = retry_delay(requested, min(attempt, 6)) if requested is not None else min(60.0, float(2 ** min(attempt, 6)))
            delay = max(delay, 0.1)  # Retry-After: 0 must not cause a busy loop.
            if delay >= deadline - time.monotonic():
                return response
            waited = True
            try:
                _wait(policy, delay, started, attempt, "rate_limited" if response.status_code == 429 else "transient_http")
            except BaseException:
                response.close()
                raise
            if time.monotonic() >= deadline:
                return response
            response.close()
            attempt += 1
    finally:
        if waited:
            _notify(policy, event="finished", state="waiting_network")


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
    policy = _LONG_GET_POLICY.get()
    if method.upper() == "GET" and policy is not None:
        return _long_request(session, method, url, policy, **kwargs)
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
