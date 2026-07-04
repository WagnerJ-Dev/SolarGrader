"""Small shared utilities with no domain logic."""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def with_retries(fn: Callable[..., T], *args,
                 attempts: int = 4, base_delay: float = 5.0, **kwargs) -> T:
    """Call ``fn`` with exponential backoff on transient failures (e.g. an Overpass
    504 or a PVGIS hiccup). Re-raises only after the final attempt.

    >>> with_retries(lambda: 1 + 1)
    2
    """
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — deliberate: retry any transient error
            if i == attempts - 1:
                raise
            wait = base_delay * (2 ** i)
            print(f"    {type(e).__name__}: {e}\n"
                  f"    retry {i + 1}/{attempts - 1} in {wait:.0f}s...")
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover
