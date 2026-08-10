from __future__ import annotations

import time
from typing import Any, Callable

import pandas as pd

from app.fundamental.evidence import ResearchSourceError
from app.fundamental.schemas import ResearchSearchResults, ResearchSource


def _retry_call(call: Callable[[], Any], max_retries: int) -> Any:
    for attempt in range(max_retries + 1):
        try:
            return call()
        except Exception:
            if attempt >= max_retries:
                raise
            time.sleep(0.1 * (attempt + 1))
    raise RuntimeError("unreachable")


def _coerce_frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value
    return pd.DataFrame(value)
