"""Relance avec backoff exponentiel (429 / 5xx / erreurs réseau)."""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import httpx

log = logging.getLogger(__name__)
T = TypeVar("T")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def _status_and_retry_after(exc: BaseException) -> tuple[int | None, float | None]:
    status = getattr(exc, "status_code", None)
    headers = getattr(exc, "headers", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        headers = exc.response.headers
    retry_after = None
    if headers is not None:
        try:
            retry_after = float(headers.get("retry-after"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            retry_after = None
    return status, retry_after


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)):
        return True
    status, _ = _status_and_retry_after(exc)
    return status in RETRYABLE_STATUS


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 6,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    retryable: Callable[[BaseException], bool] = is_retryable,
    label: str = "appel API",
) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - on filtre juste en dessous
            if attempt >= attempts or not retryable(exc):
                raise
            _, retry_after = _status_and_retry_after(exc)
            delay = retry_after if retry_after is not None else min(base_delay * 2 ** (attempt - 1), max_delay)
            delay += random.uniform(0, 0.5)
            log.warning("%s : échec (%s), nouvelle tentative %d/%d dans %.1f s", label, exc, attempt + 1, attempts, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")
