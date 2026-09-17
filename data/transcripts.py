"""Earnings-call transcript interface (stub: no provider wired in yet).

There is no free, redistributable, machine-readable source of earnings-call transcripts,
so this module only fixes the *shape* the rest of the repository codes against. Company
models and writeups depend on ``TranscriptSource``; the concrete provider (a paid API, a
scraper you are licensed to run, or hand-saved text files) is plugged in later behind the
``TRANSCRIPT_PROVIDER`` environment variable. Until then the default source explains
itself instead of failing obscurely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

TRANSCRIPT_PROVIDER_ENV = "TRANSCRIPT_PROVIDER"


@dataclass(frozen=True)
class TranscriptRef:
    """Identifies one call without carrying its text (cheap to list, cache and compare)."""

    ticker: str
    fiscal_period: str  # the company's own label, e.g. "FY2026Q2"
    date: str  # YYYY-MM-DD, the call date
    title: str
    url: str


@dataclass(frozen=True)
class Transcript:
    """A full transcript. ``speakers`` is optional metadata some providers expose."""

    ref: TranscriptRef
    text: str
    speakers: tuple[str, ...] = ()


class SourceNotConfigured(RuntimeError):
    """Raised when a transcript is requested but no provider has been configured."""


@runtime_checkable
class TranscriptSource(Protocol):
    """What any transcript provider must offer."""

    name: str

    def list_transcripts(self, ticker: str) -> list[TranscriptRef]:
        """Known calls for a ticker, newest first."""
        ...

    def get_transcript(self, ref: TranscriptRef) -> Transcript:
        """Full text for one call."""
        ...


class UnconfiguredTranscriptSource:
    """Default source: every call raises ``SourceNotConfigured`` with setup instructions."""

    name = "none"

    def _fail(self) -> SourceNotConfigured:
        return SourceNotConfigured(
            "No transcript provider is configured. Set the "
            f"{TRANSCRIPT_PROVIDER_ENV} environment variable and register an implementation "
            "of TranscriptSource in data/transcripts.py (only 'none' exists today)."
        )

    def list_transcripts(self, ticker: str) -> list[TranscriptRef]:
        # TODO(philbert): plug in a provider
        raise self._fail()

    def get_transcript(self, ref: TranscriptRef) -> Transcript:
        # TODO(philbert): plug in a provider
        raise self._fail()


def get_transcript_source() -> TranscriptSource:
    """Pick the provider named by ``TRANSCRIPT_PROVIDER`` (default ``none``).

    Only ``none`` is implemented; any other value raises ``SourceNotConfigured`` so a typo
    in CI configuration is loud rather than silently returning nothing.
    """
    provider = os.environ.get(TRANSCRIPT_PROVIDER_ENV, "none").strip().lower() or "none"
    if provider == "none":
        return UnconfiguredTranscriptSource()
    # TODO(philbert): plug in a provider
    raise SourceNotConfigured(
        f"{TRANSCRIPT_PROVIDER_ENV}={provider!r} is not implemented; only 'none' exists."
    )
