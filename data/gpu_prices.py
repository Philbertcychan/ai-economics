"""GPU rental price interface with a CSV-backed default source.

Public GPU-hour quotes (hyperscaler list prices, neocloud price pages, marketplace indices)
are scattered across web pages and rarely licensed for redistribution, so the default source
is a hand-maintained CSV of quotes, each with the URL it was read from. Anything that needs
a price series depends on ``GPUPriceSource``; a live provider can be added later behind the
``GPU_PRICE_PROVIDER`` environment variable without touching callers.

The CSV lives at ``data/processed/gpu_prices.csv`` with the columns in ``PRICE_COLUMNS``:

    date,gpu_model,provider,region,price_per_gpu_hour,term,source_url
    2026-09-01,H100,ExampleCloud,us-east,2.49,on-demand,https://example.invalid/pricing
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

from data import PROCESSED_DIR

GPU_PRICE_PROVIDER_ENV = "GPU_PRICE_PROVIDER"
DEFAULT_PRICE_CSV = PROCESSED_DIR / "gpu_prices.csv"
PRICE_COLUMNS: tuple[str, ...] = (
    "date",  # YYYY-MM-DD the quote was observed
    "gpu_model",  # e.g. "H100", "GB200"
    "provider",
    "region",
    "price_per_gpu_hour",  # USD
    "term",  # "on-demand", "1y-reserved", "spot", ...
    "source_url",
)
_STR_COLUMNS = tuple(c for c in PRICE_COLUMNS if c != "price_per_gpu_hour")


@runtime_checkable
class GPUPriceSource(Protocol):
    """What any GPU price provider must offer."""

    name: str

    def get_prices(
        self,
        gpu_model: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        """Quotes as a frame with exactly ``PRICE_COLUMNS``, filtered and sorted by date."""
        ...


def empty_price_frame() -> pd.DataFrame:
    """An empty frame with the right columns and dtypes (what callers get when no data)."""
    return pd.DataFrame(
        {
            c: pd.Series(dtype="float64" if c == "price_per_gpu_hour" else "str")
            for c in PRICE_COLUMNS
        }
    )


class CSVGPUPriceSource:
    """Reads hand-maintained public quotes from a CSV; the default provider (needs no key)."""

    name = "csv"

    def __init__(self, path: Path = DEFAULT_PRICE_CSV) -> None:
        self.path = Path(path)

    def get_prices(
        self,
        gpu_model: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        """Quotes filtered by model (case-insensitive) and inclusive ``YYYY-MM-DD`` bounds."""
        if not self.path.is_file():
            return empty_price_frame()
        frame = pd.read_csv(self.path, encoding="utf-8", dtype={c: "str" for c in _STR_COLUMNS})
        # Tolerate extra or missing columns in a hand-edited file rather than crashing.
        frame = frame.reindex(columns=list(PRICE_COLUMNS))
        frame["price_per_gpu_hour"] = pd.to_numeric(frame["price_per_gpu_hour"], errors="coerce")
        for column in _STR_COLUMNS:
            frame[column] = frame[column].astype("str")
        if gpu_model is not None:
            frame = frame[frame["gpu_model"].str.upper() == gpu_model.upper()]
        if start is not None:
            frame = frame[frame["date"] >= start]
        if end is not None:
            frame = frame[frame["date"] <= end]
        return frame.sort_values(["date", "provider", "gpu_model"], kind="stable").reset_index(
            drop=True
        )


def get_gpu_price_source() -> GPUPriceSource:
    """Pick the provider named by ``GPU_PRICE_PROVIDER`` (default ``csv``)."""
    provider = os.environ.get(GPU_PRICE_PROVIDER_ENV, "csv").strip().lower() or "csv"
    if provider == "csv":
        return CSVGPUPriceSource()
    # TODO(philbert): register a live GPU price provider here when one is licensed
    raise ValueError(
        f"{GPU_PRICE_PROVIDER_ENV}={provider!r} is not implemented; only 'csv' exists."
    )
