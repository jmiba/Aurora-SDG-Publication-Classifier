"""Shared HTTP retry and backoff helpers."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

import requests

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After delay expressed as seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _backoff(
    attempt: int,
    base: float,
    cap: float,
    retry_after: Optional[str],
) -> float:
    """Return a capped exponential delay with jitter or a Retry-After delay."""
    retry_after_seconds = _retry_after_seconds(retry_after)
    if retry_after_seconds is not None:
        return min(max(0.0, cap), retry_after_seconds)

    maximum = min(max(0.0, cap), max(0.0, base) * (2 ** max(0, attempt - 1)))
    return random.uniform(0.0, maximum)


def request_with_backoff(
    session: Any,
    method: str,
    url: str,
    *,
    retries: int = 4,
    base: float = 0.5,
    cap: float = 15.0,
    **kw: Any,
) -> Any:
    """Make an HTTP request with one shared transient-failure retry policy."""
    if retries < 1:
        raise ValueError("retries must be at least 1")

    before_request: Optional[Callable[[], None]] = kw.pop("_before_request", None)
    request = getattr(session, method)
    for attempt in range(1, retries + 1):
        try:
            if before_request:
                before_request()
            response = request(url, **kw)
        except requests.RequestException:
            if attempt >= retries:
                raise
            time.sleep(_backoff(attempt, base, cap, None))
            continue

        if response.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
            retry_after = response.headers.get("Retry-After")
            time.sleep(_backoff(attempt, base, cap, retry_after))
            continue
        response.raise_for_status()
        return response

    raise RuntimeError("HTTP retry loop exited unexpectedly")
