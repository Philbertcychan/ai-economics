"""SEC EDGAR client and tidy-facts helpers.

Everything the company models and the dashboard know about *reported* numbers arrives
through this module. It does three jobs:

1. Pull the free EDGAR JSON endpoints (submissions, XBRL company facts, full-text search)
   politely: a descriptive User-Agent, at most ~9 requests per second, gzip accepted,
   retries on 429/5xx.
2. Cache every raw response on disk under ``data/raw/<TICKER>/<YYYY-MM-DD>/`` and write a
   manifest with a sha256 per file, so any number in the model can be traced back to the
   exact bytes SEC served on a given day.
3. Reshape the nested XBRL "company facts" JSON into one flat table (``facts_to_frame``)
   and turn that table into calendar quarterly/annual series (``calendar_series``) that
   pandas, Excel and the site can all read without knowing anything about XBRL.

There is no financial-model logic here. Turning reported facts into drivers and forecasts
is the job of the company models in ``companies/``.

Network access goes through one injectable ``fetch(url, headers) -> bytes`` callable, so
the test suite runs fully offline against the trimmed real responses in
``tests/fixtures/edgar/``.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import gzip
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from data import PROCESSED_DIR, RAW_DIR

log = logging.getLogger(__name__)

# --- SEC access policy -------------------------------------------------------------------

USER_AGENT_ENV = "EDGAR_USER_AGENT"
# SEC's fair-access policy asks every automated client to identify itself with a contact
# address. The placeholder below keeps requests working out of the box; the warning in
# `user_agent()` nags until a real address is configured.
DEFAULT_USER_AGENT = (
    "ai-economics/0.1 open research model "
    "(set EDGAR_USER_AGENT to 'Your Name you@example.com') contact@ai-economics.invalid"
)
MIN_REQUEST_INTERVAL_S = 0.11  # SEC allows 10 requests/s; 0.11 s leaves a small margin
MAX_RETRIES = 3  # extra attempts after the first one, for 429 / 5xx / network errors
RETRY_BACKOFF_S = 0.5  # first retry waits this long; each further retry doubles it
REQUEST_TIMEOUT_S = 60.0

SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
COMPANY_FACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

_warned_default_user_agent = False


def user_agent() -> str:
    """Return the User-Agent to send to SEC: ``EDGAR_USER_AGENT`` if set, else the default.

    The default is logged as a warning once per process because SEC may throttle or block
    clients that do not carry a real contact address.
    """
    global _warned_default_user_agent
    configured = os.environ.get(USER_AGENT_ENV, "").strip()
    if configured:
        return configured
    if not _warned_default_user_agent:
        log.warning(
            "%s is not set; using the placeholder User-Agent. SEC asks automated clients "
            "to identify themselves with a contact address.",
            USER_AGENT_ENV,
        )
        _warned_default_user_agent = True
    return DEFAULT_USER_AGENT


# `EdgarClient.__init__` takes a parameter called `user_agent`, which would shadow the
# function above inside that method. This alias keeps the call unambiguous.
_resolve_user_agent = user_agent


# --- Company registry --------------------------------------------------------------------


@dataclass(frozen=True)
class CompanyInfo:
    """Static facts about a covered company, used to build URLs and pick filing forms."""

    ticker: str
    cik: str  # 10-digit, zero-padded, as the JSON endpoints want it
    name: str
    layer: str  # "neocloud" | "chip" | "hyperscaler"
    filer_type: str  # "domestic" (10-K/10-Q) | "foreign" (20-F/6-K)
    periodic_forms: tuple[str, ...]


COMPANIES: dict[str, CompanyInfo] = {
    "CRWV": CompanyInfo(
        "CRWV",
        "0001769628",
        "CoreWeave, Inc.",
        "neocloud",
        "domestic",
        ("10-K", "10-Q", "S-1", "S-1/A", "424B4"),
    ),
    "NBIS": CompanyInfo(
        "NBIS", "0001513845", "Nebius Group N.V.", "neocloud", "foreign", ("20-F", "6-K")
    ),
    "NVDA": CompanyInfo("NVDA", "0001045810", "NVIDIA Corp", "chip", "domestic", ("10-K", "10-Q")),
    "MSFT": CompanyInfo(
        "MSFT", "0000789019", "Microsoft Corp", "hyperscaler", "domestic", ("10-K", "10-Q")
    ),
    "GOOGL": CompanyInfo(
        "GOOGL", "0001652044", "Alphabet Inc.", "hyperscaler", "domestic", ("10-K", "10-Q")
    ),
    "AMZN": CompanyInfo(
        "AMZN", "0001018724", "Amazon.com, Inc.", "hyperscaler", "domestic", ("10-K", "10-Q")
    ),
    "META": CompanyInfo(
        "META", "0001326801", "Meta Platforms, Inc.", "hyperscaler", "domestic", ("10-K", "10-Q")
    ),
}


def company(ticker: str) -> CompanyInfo:
    """Look up a covered company by ticker (case-insensitive); KeyError lists the known ones."""
    try:
        return COMPANIES[ticker.upper()]
    except KeyError:
        raise KeyError(f"unknown ticker {ticker!r}; known: {', '.join(COMPANIES)}") from None


class EdgarError(RuntimeError):
    """An EDGAR request failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


# --- Identifiers and URLs ----------------------------------------------------------------


def cik10(cik: str | int) -> str:
    """Zero-pad a CIK to the 10 digits the JSON endpoints expect (``1769628`` -> ``0001769628``)."""
    return str(int(str(cik).strip())).zfill(10)


def accession_nodash(accession: str) -> str:
    """``0001193125-25-044231`` -> ``000119312525044231`` (the Archives folder name)."""
    return accession.replace("-", "")


def submissions_url(cik: str | int) -> str:
    """URL of the submissions JSON (filing index) for a CIK."""
    return f"{SUBMISSIONS_BASE}/CIK{cik10(cik)}.json"


def company_facts_url(cik: str | int) -> str:
    """URL of the XBRL company-facts JSON for a CIK."""
    return f"{COMPANY_FACTS_BASE}/CIK{cik10(cik)}.json"


def filing_url(cik: str | int, accession: str, document: str) -> str:
    """URL of one document inside a filing.

    The Archives path uses the *company's* CIK without padding, even when the accession
    number was issued to a filing agent (e.g. ``0001193125-...`` for Donnelley).
    """
    return f"{ARCHIVES_BASE}/{int(cik10(cik))}/{accession_nodash(accession)}/{document}"


def filing_index_url(cik: str | int, accession: str) -> str:
    """URL of the human-readable index page listing every document in a filing."""
    return filing_url(cik, accession, f"{accession}-index.htm")


def full_text_search_url(
    query: str,
    *,
    forms: Iterable[str] | None = None,
    ciks: Iterable[str | int] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """Build an EDGAR full-text-search URL.

    ``ciks`` accepts covered tickers as well as raw CIKs. A date range is only added when
    at least one bound is given; the other bound then defaults to the earliest indexed
    year (2001) or today.
    """
    params: dict[str, str] = {"q": query}
    if forms:
        params["forms"] = ",".join(forms)
    if ciks:
        params["ciks"] = ",".join(_normalise_cik(c) for c in ciks)
    if start or end:
        params["dateRange"] = "custom"
        params["startdt"] = start or "2001-01-01"
        params["enddt"] = end or dt.date.today().isoformat()
    # `quote` (not `quote_plus`) with commas kept literal matches the URLs SEC's own UI issues.
    encoded = urllib.parse.urlencode(params, safe=",", quote_via=urllib.parse.quote)
    return f"{FULL_TEXT_SEARCH_URL}?{encoded}"


def _normalise_cik(value: str | int) -> str:
    if isinstance(value, str) and value.upper() in COMPANIES:
        return COMPANIES[value.upper()].cik
    return cik10(value)


# --- Filings -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Filing:
    """One row of a company's filing index."""

    cik: str
    accession: str  # with dashes, e.g. 0001193125-25-044231
    form: str
    filing_date: str  # YYYY-MM-DD
    report_date: str | None  # period the filing covers; None for registration statements
    primary_document: str
    description: str | None
    size: int | None
    is_xbrl: bool
    items: str | None = None  # 8-K item codes, e.g. "2.02,9.01" (2.02 = results of operations)

    @property
    def url(self) -> str:
        """Direct link to the primary document."""
        return filing_url(self.cik, self.accession, self.primary_document)

    @property
    def index_url(self) -> str:
        """Link to the filing's index page (all exhibits)."""
        return filing_index_url(self.cik, self.accession)

    def to_row(self) -> dict[str, Any]:
        """Flat dict for ``filings.csv`` (column order matches the refresh pipeline)."""
        return {
            "accession": self.accession,
            "form": self.form,
            "filing_date": self.filing_date,
            "report_date": self.report_date,
            "primary_document": self.primary_document,
            "url": self.url,
            "size": self.size,
            "is_xbrl": self.is_xbrl,
        }


def iter_filings(cik: str | int, columnar: Mapping[str, Sequence[Any]]) -> Iterator[Filing]:
    """Yield ``Filing`` objects from a columnar block (``filings.recent`` or an older page).

    SEC stores the index as parallel arrays keyed by field name; a missing array is treated
    as all-empty so a page with fewer fields still parses.
    """
    accessions = columnar.get("accessionNumber", [])
    count = len(accessions)
    cik_padded = cik10(cik)

    def column(name: str) -> Sequence[Any]:
        values = columnar.get(name)
        return values if values is not None and len(values) == count else [None] * count

    forms = column("form")
    filing_dates = column("filingDate")
    report_dates = column("reportDate")
    documents = column("primaryDocument")
    descriptions = column("primaryDocDescription")
    sizes = column("size")
    xbrl_flags = column("isXBRL")
    items = column("items")
    for i in range(count):
        yield Filing(
            cik=cik_padded,
            accession=str(accessions[i]),
            form=str(forms[i] or ""),
            filing_date=str(filing_dates[i] or ""),
            report_date=str(report_dates[i]) if report_dates[i] else None,
            primary_document=str(documents[i] or ""),
            description=str(descriptions[i]) if descriptions[i] else None,
            size=int(sizes[i]) if sizes[i] not in (None, "") else None,
            is_xbrl=bool(xbrl_flags[i]),
            items=str(items[i]) if items[i] else None,
        )


def merge_submissions(main: Mapping[str, Any], pages: Sequence[Mapping[str, Any]]) -> dict:
    """Return a copy of the submissions JSON with every older page appended to ``recent``.

    Pages are bare columnar objects; ``recent`` is newest-first and each page is older than
    the last, so appending in order keeps the merged block newest-first. The ``files`` list
    is left in place so the merged view still documents where each page came from.
    """
    merged = copy.deepcopy(dict(main))
    filings = merged.setdefault("filings", {})
    recent = filings.setdefault("recent", {})
    for page in pages:
        have = len(recent.get("accessionNumber", []))
        count = len(page.get("accessionNumber", []))
        for key in list(recent.keys()) + [k for k in page if k not in recent]:
            # a column only the page has is back-filled with None for the rows already merged
            recent.setdefault(key, [None] * have)
            recent[key].extend(page.get(key, [None] * count))
    return merged


# --- HTTP ---------------------------------------------------------------------------------


def _decode_body(body: bytes, content_encoding: str | None) -> bytes:
    """Undo the transfer compression urllib leaves in place."""
    encoding = (content_encoding or "").lower()
    if "gzip" in encoding:
        return gzip.decompress(body)
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate, no zlib header
    return body


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    """Honour a numeric Retry-After header, else back off exponentially.

    Retries happen inside one throttled request, so the client's rate limiter never sees
    them; flooring the delay keeps a ``Retry-After: 0`` from sending back-to-back requests.
    """
    delay = RETRY_BACKOFF_S * (2**attempt)
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            pass
    return max(delay, MIN_REQUEST_INTERVAL_S)


def build_default_fetch(
    sleep: Callable[[float], None] = time.sleep,
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = REQUEST_TIMEOUT_S,
) -> Callable[[str, dict[str, str]], bytes]:
    """Create the stdlib-based ``fetch(url, headers) -> bytes`` used when none is injected.

    Retries 429 and 5xx responses and network errors up to ``MAX_RETRIES`` times with
    backoff via ``sleep``; any other HTTP error becomes an ``EdgarError`` carrying the
    status code. "Network errors" includes failures while reading the body (a stalled
    20 MB company-facts download raises a bare ``TimeoutError``, not ``URLError``).
    ``urlopen`` is injectable so the retry and gzip paths are testable offline.
    """

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        attempt = 0
        while True:
            request = urllib.request.Request(url, headers=headers)
            try:
                with urlopen(request, timeout=timeout) as response:
                    body = response.read()
                    content_encoding = response.headers.get("Content-Encoding")
                return _decode_body(body, content_encoding)
            except urllib.error.HTTPError as err:
                retryable = err.code == 429 or err.code >= 500
                if not retryable or attempt >= MAX_RETRIES:
                    raise EdgarError(
                        f"HTTP {err.code} for {url}", status=err.code, url=url
                    ) from err
                retry_after = err.headers.get("Retry-After") if err.headers is not None else None
                delay = _retry_delay(retry_after, attempt)
                reason: object = err.code
            # urllib wraps only connect-phase failures in URLError; a timeout, reset or
            # truncated body during `response.read()` arrives as the bare exception.
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
            ) as err:
                reason = getattr(err, "reason", None) or repr(err)
                if attempt >= MAX_RETRIES:
                    raise EdgarError(f"network error for {url}: {reason}", url=url) from err
                delay = _retry_delay(None, attempt)
            attempt += 1
            log.warning(
                "EDGAR request failed (%s); retry %d/%d in %.1fs: %s",
                reason,
                attempt,
                MAX_RETRIES,
                delay,
                url,
            )
            sleep(delay)

    return fetch


# --- Client --------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file, streamed so 20 MB filings do not load into memory twice."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now_iso() -> str:
    """Current UTC time as ``2026-09-12T11:00:00Z`` (the repo-wide timestamp format)."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_today() -> dt.date:
    """Today's date in UTC, matching the UTC timestamps written next to it."""
    return dt.datetime.now(dt.UTC).date()


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _parse_json(body: bytes, url: str) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise EdgarError(f"response from {url} is not JSON", url=url) from err


def _page_number(name: str, fallback: int) -> int:
    """``CIK0001769628-submissions-001.json`` -> 1; falls back to the list position."""
    match = re.search(r"-(\d+)\.json$", name)
    return int(match.group(1)) if match else fallback


# Manifest fields saying where a cached file came from (see `EdgarClient.write_manifest`).
_PROVENANCE_KEYS: tuple[str, ...] = ("source_url", "fetched_at", "copied_from")


def _filing_relative_path(filing: Filing) -> Path:
    """Where a filing's primary document sits inside a dated cache directory."""
    return Path("filings") / filing.accession / filing.primary_document


def _manifest_entries(dated_dir: Path) -> dict[str, dict]:
    """``path -> entry`` from a dated directory's manifest; empty when missing or corrupt."""
    try:
        files = _read_json(dated_dir / "manifest.json").get("files", [])
        return {entry["path"]: entry for entry in files}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _copied_provenance(earlier: Path, relative: Path, url: str) -> dict[str, Any]:
    """Manifest fields for a document copied forward from ``earlier`` (``<dated>/<relative>``).

    Nothing was fetched today, so the entry must not claim a fetch time of its own. It keeps
    the original ``fetched_at`` when the earlier day's manifest vouches for the same bytes,
    and otherwise names the file it was copied from (relative to the new manifest).
    """
    dated_dir = earlier.parents[len(relative.parts) - 1]
    prior = _manifest_entries(dated_dir).get(relative.as_posix(), {})
    if prior.get("fetched_at") and prior.get("sha256") == sha256_file(earlier):
        return {"source_url": prior.get("source_url") or url, "fetched_at": prior["fetched_at"]}
    copied_from = Path("..") / dated_dir.name / relative
    return {"source_url": url, "copied_from": copied_from.as_posix()}


class EdgarClient:
    """Rate-limited, cache-first EDGAR client.

    All raw responses for a ticker land in ``raw_dir / TICKER / YYYY-MM-DD`` (the UTC date).
    Within one dated directory a second call for the same resource reads the cached file;
    a new day means a fresh pull (except filing documents, which are copied forward from
    any earlier day rather than re-downloaded).
    """

    def __init__(
        self,
        raw_dir: Path = RAW_DIR,
        *,
        user_agent: str | None = None,
        fetch: Callable[[str, dict[str, str]], bytes] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        today: dt.date | None = None,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        # UTC, not the local date: the manifest timestamps are UTC, and an evening run on a
        # laptop in the Americas would otherwise land in a directory dated a day before its
        # own `pulled_at` (and sort before the CI pull of the same UTC day).
        self.today = today or utc_today()
        self._user_agent = user_agent or _resolve_user_agent()
        self._fetch = fetch or build_default_fetch(sleep)
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        # absolute path -> manifest provenance fields for every file this instance wrote,
        # so the manifest can say where each byte came from
        self._sources: dict[Path, dict[str, Any]] = {}

    # -- plumbing --------------------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        """Headers sent with every request (SEC requires the User-Agent; gzip halves transfer)."""
        return {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}

    def cache_dir(self, ticker: str) -> Path:
        """``raw_dir / TICKER / YYYY-MM-DD`` (``today``, by default the UTC date), created on
        first use."""
        path = self.raw_dir / ticker.upper() / self.today.isoformat()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _throttle(self) -> None:
        """Sleep just enough to stay under SEC's requests-per-second limit."""
        now = self._clock()
        if self._last_request_at is not None:
            wait = MIN_REQUEST_INTERVAL_S - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def get_bytes(self, url: str) -> bytes:
        """Fetch a URL with rate limiting and the SEC headers."""
        self._throttle()
        log.debug("GET %s", url)
        return self._fetch(url, self.headers())

    def get_json(self, url: str) -> Any:
        """Fetch and decode a JSON endpoint."""
        return _parse_json(self.get_bytes(url), url)

    def _write_bytes(self, path: Path, body: bytes, url: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        self._sources[path.resolve()] = {"source_url": url, "fetched_at": utc_now_iso()}

    def _cached_json(self, path: Path, url: str) -> Any:
        """Parse the cached file, or fetch ``url`` and cache the body byte for byte.

        The file on disk is never re-serialised, so its sha256 in the manifest can be checked
        against what SEC serves at ``source_url``.
        """
        if path.exists():
            return _read_json(path)
        raw = self.get_bytes(url)
        payload = _parse_json(raw, url)  # validate before caching a bad body
        self._write_bytes(path, raw, url)
        return payload

    # -- endpoints -------------------------------------------------------------------------

    def submissions(self, ticker: str) -> dict:
        """Filing index with every older page merged into ``filings.recent``.

        Only SEC's own responses are cached: ``submissions.json`` (the main page) and
        ``submissions-NNN.json`` (older pages). The merged view is rebuilt in memory on each
        call, because a merged file on disk would match no URL's bytes.
        """
        info = company(ticker)
        root = self.cache_dir(ticker)
        main = self._cached_json(root / "submissions.json", submissions_url(info.cik))
        pages: list[dict] = []
        for position, page_ref in enumerate(main.get("filings", {}).get("files", []), start=1):
            number = _page_number(page_ref["name"], position)
            page_url = f"{SUBMISSIONS_BASE}/{page_ref['name']}"
            pages.append(self._cached_json(root / f"submissions-{number:03d}.json", page_url))
        return merge_submissions(main, pages)

    def filings(
        self, ticker: str, forms: Iterable[str] | None = None, since: str | None = None
    ) -> list[Filing]:
        """Filings of the given forms (default: the company's periodic forms), newest first.

        ``since`` is an inclusive ``YYYY-MM-DD`` lower bound on the filing date.
        """
        info = company(ticker)
        if forms is None:
            wanted = set(info.periodic_forms)
        elif isinstance(forms, str):
            wanted = {forms}
        else:
            wanted = set(forms)
        recent = self.submissions(ticker).get("filings", {}).get("recent", {})
        selected = [
            filing
            for filing in iter_filings(info.cik, recent)
            if filing.form in wanted and (since is None or filing.filing_date >= since)
        ]
        selected.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
        return selected

    def company_facts(self, ticker: str) -> dict:
        """XBRL company facts JSON, cached as ``companyfacts.json``."""
        info = company(ticker)
        cache = self.cache_dir(ticker) / "companyfacts.json"
        return self._cached_json(cache, company_facts_url(info.cik))

    def cached_filing_path(self, ticker: str, filing: Filing) -> Path | None:
        """Newest local copy of a filing's primary document in any dated directory, else None.

        Lets a caller tell "already on disk" from "a past download failed" without knowing
        the cache layout. Never creates directories and never touches the network.
        """
        company_dir = self.raw_dir / ticker.upper()
        if not company_dir.is_dir():
            return None
        relative = _filing_relative_path(filing)
        dated_dirs = (p for p in company_dir.iterdir() if p.is_dir())
        for dated in sorted(dated_dirs, reverse=True):  # ISO-dated names: newest first
            candidate = dated / relative
            if candidate.is_file():
                return candidate
        return None

    def download_filing(self, ticker: str, filing: Filing) -> Path:
        """Store a filing's primary document under ``cache_dir/filings/<accession>/``.

        Filing documents never change once published, so a copy from any other dated
        directory is reused instead of re-downloading a 20 MB 10-K every day.
        """
        relative = _filing_relative_path(filing)
        target = self.cache_dir(ticker) / relative
        if target.exists():
            return target
        earlier = self.cached_filing_path(ticker, filing)
        if earlier is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(earlier, target)
            self._sources[target.resolve()] = _copied_provenance(earlier, relative, filing.url)
            log.info("reused %s from %s", relative.as_posix(), earlier)
            return target
        self._write_bytes(target, self.get_bytes(filing.url), filing.url)
        return target

    def filing_exhibits(self, filing: Filing) -> list[str]:
        """Names of a filing's exhibit documents, from SEC's per-filing ``index.json``.

        Operating KPIs that are not in the structured data (active power, backlog) are usually
        disclosed in the earnings press release, which is an EXHIBIT to an 8-K, not its primary
        document. The index carries no exhibit type, so an exhibit is any ``.htm`` file that is
        neither the primary document nor one of SEC's own renderings (``R1.htm``, the index).
        """
        folder = f"{ARCHIVES_BASE}/{int(filing.cik)}/{accession_nodash(filing.accession)}"
        listing = self.get_json(f"{folder}/index.json")
        names = [str(item.get("name", "")) for item in listing.get("directory", {}).get("item", [])]
        return [
            name
            for name in names
            if name.lower().endswith((".htm", ".html"))
            and name != filing.primary_document
            and not re.fullmatch(r"R\d+\.htm", name)
            and "-index" not in name
        ]

    def download_document(self, ticker: str, filing: Filing, name: str) -> Path:
        """Store any document of a filing (for example an exhibit) beside its primary document.

        Same caching rule as ``download_filing``: a copy in any dated directory is reused.
        """
        return self.download_filing(ticker, dataclasses.replace(filing, primary_document=name))

    def full_text_search(
        self,
        query: str,
        *,
        forms: Iterable[str] | None = None,
        ciks: Iterable[str | int] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        """Run an EDGAR full-text search; each hit is its ``_source`` plus ``id`` (``_id``).

        Results are not cached: searches are ad hoc and the index changes daily.
        """
        url = full_text_search_url(query, forms=forms, ciks=ciks, start=start, end=end)
        payload = self.get_json(url)
        hits = payload.get("hits", {}).get("hits", [])
        return [{**hit.get("_source", {}), "id": hit.get("_id")} for hit in hits]

    def write_manifest(self, ticker: str) -> Path:
        """Write ``cache_dir/manifest.json`` listing every cached file with its sha256.

        Files fetched by this instance carry their source URL and fetch time; entries from
        a manifest written earlier the same day are carried forward when the bytes match.
        A filing document copied from an earlier day keeps its original ``fetched_at``, or,
        when that is unknown, has ``copied_from`` (a path relative to this manifest) in
        place of ``fetched_at``.
        """
        info = company(ticker)
        root = self.cache_dir(ticker)
        manifest_path = root / "manifest.json"
        previous = _manifest_entries(root)  # a corrupt manifest is simply rebuilt
        files = []
        paths = [p for p in root.rglob("*") if p.is_file() and p != manifest_path]
        for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            digest = sha256_file(path)
            provenance = self._sources.get(path.resolve())
            prior = previous.get(relative, {})
            if provenance is None and prior.get("sha256") == digest:
                provenance = {k: prior[k] for k in _PROVENANCE_KEYS if k in prior}
            if not provenance:
                provenance = {"source_url": None, "fetched_at": None}
            files.append(
                {"path": relative, "bytes": path.stat().st_size, "sha256": digest, **provenance}
            )
        manifest = {
            "ticker": ticker.upper(),
            "cik": info.cik,
            "pulled_at": utc_now_iso(),
            "files": files,
        }
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        return manifest_path


# --- Tidy facts ----------------------------------------------------------------------------

# friendly concept -> candidate XBRL tags. The candidate whose data reaches the latest period
# wins and ties keep this order (see `resolve_concept`). A candidate may carry a taxonomy
# prefix ("dei:..."); bare tags are looked up in us-gaap, then ifrs-full.
STANDARD_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "Revenue"),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfSales"),
    "operating_income": ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "d_and_a": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        # Last resort: Microsoft and Alphabet publish no combined D&A element, only PP&E
        # depreciation. The `tag` column (and the site's source-elements list) says which
        # element a series really is, so the narrower measure is never passed off silently.
        "Depreciation",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        # Nvidia and Amazon report capex together with purchased intangibles under this element.
        "PaymentsToAcquireProductiveAssets",
    ),
    "ppe_net": (
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipment",
        # Amazon, Alphabet and Meta moved to this element, which includes finance-lease assets.
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ),
    "long_term_debt": (
        "LongTermDebt",
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "Borrowings",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalents",
    ),
    "cfo": (
        "NetCashProvidedByUsedInOperatingActivities",
        "CashFlowsFromUsedInOperatingActivities",
    ),
    "interest_expense": ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"),
    "shares_outstanding": (
        "dei:EntityCommonStockSharesOutstanding",
        "CommonStockSharesOutstanding",
    ),
}
# Balance-sheet items are point-in-time; everything else accumulates over a period.
INSTANT_CONCEPTS: frozenset[str] = frozenset(
    {"ppe_net", "long_term_debt", "cash", "shares_outstanding"}
)
FLOW_CONCEPTS: frozenset[str] = frozenset(STANDARD_CONCEPTS) - INSTANT_CONCEPTS

FACT_COLUMNS: tuple[str, ...] = (
    "concept",
    "tag",
    "taxonomy",
    "unit",
    "start",
    "end",
    "val",
    "fy",
    "fp",
    "form",
    "filed",
    "accn",
    "frame",
)
_STR_FACT_COLUMNS = tuple(c for c in FACT_COLUMNS if c not in ("val", "fy"))
# `fy` is nullable-integer rather than float so the committed CSV reads "2025", not "2025.0";
# proxy-statement facts (DEF 14A) genuinely carry fy = null.
_FACT_DTYPES: dict[str, str] = {c: "str" for c in _STR_FACT_COLUMNS} | {
    "val": "float64",
    "fy": "Int64",
}

SERIES_COLUMNS: tuple[str, ...] = ("period", "end", "val", "derived")
PREFERRED_UNITS: tuple[str, ...] = ("USD", "shares")
DEFAULT_TAXONOMIES: tuple[str, ...] = ("us-gaap", "ifrs-full")
FRAME_RE = re.compile(r"^CY(?P<year>\d{4})(?:Q(?P<quarter>[1-4]))?(?P<instant>I)?$")


def resolve_concept(
    company_facts: Mapping[str, Any], candidates: Sequence[str]
) -> tuple[str, str, dict] | None:
    """Return ``(taxonomy, tag, payload)`` for the candidate tag with the most recent data.

    Filers change tags over the years and the old tag stays in company facts forever, so
    "first present" can return a series that stopped a decade ago while a later candidate
    carries the current figures. Among candidates with data, the one whose facts in the
    unit ``choose_unit`` would pick reach the latest ``end`` wins; ties keep the declared
    order (and us-gaap before ifrs-full).
    """
    facts = company_facts.get("facts") or {}
    best: tuple[str, str, dict] | None = None
    best_end = ""
    for candidate in candidates:
        taxonomy, _, tag = candidate.rpartition(":")
        for tax in (taxonomy,) if taxonomy else DEFAULT_TAXONOMIES:
            payload = (facts.get(tax) or {}).get(tag) or {}
            unit = choose_unit(payload.get("units") or {})
            if unit is None:
                continue
            last_end = max(str(entry.get("end") or "") for entry in payload["units"][unit])
            if best is None or last_end > best_end:  # ISO dates compare correctly as text
                best, best_end = (tax, tag, payload), last_end
    return best


def choose_unit(units: Mapping[str, Sequence[Any]]) -> str | None:
    """Pick one unit per concept: USD, then shares, then alphabetical (deterministic).

    Foreign filers such as Nebius report the same tag in RUB and USD; the model works in
    USD, and the chosen unit stays visible in the ``unit`` column.
    """
    for preferred in PREFERRED_UNITS:
        if units.get(preferred):
            return preferred
    non_empty = sorted(unit for unit, entries in units.items() if entries)
    return non_empty[0] if non_empty else None


def facts_to_frame(
    company_facts: Mapping[str, Any],
    concepts: Mapping[str, Sequence[str]] = STANDARD_CONCEPTS,
) -> pd.DataFrame:
    """Flatten company-facts JSON into a tidy long table, one row per reported fact.

    Columns are exactly ``FACT_COLUMNS``; dtypes are plain (str / float64 / Int64) so the
    frame round-trips through CSV. Rows are sorted by concept, period end and filing date,
    which puts the most recently filed (usually restated) value last within a period.
    """
    rows: list[dict[str, Any]] = []
    for concept, candidates in concepts.items():
        found = resolve_concept(company_facts, candidates)
        if found is None:
            continue
        taxonomy, tag, payload = found
        unit = choose_unit(payload["units"])
        if unit is None:
            continue
        for entry in payload["units"][unit]:
            rows.append(
                {
                    "concept": concept,
                    "tag": tag,
                    "taxonomy": taxonomy,
                    "unit": unit,
                    "start": entry.get("start"),
                    "end": entry.get("end"),
                    "val": entry.get("val"),
                    "fy": entry.get("fy"),
                    "fp": entry.get("fp"),
                    "form": entry.get("form"),
                    "filed": entry.get("filed"),
                    "accn": entry.get("accn"),
                    "frame": entry.get("frame"),
                }
            )
    frame = _coerce_fact_dtypes(pd.DataFrame(rows, columns=list(FACT_COLUMNS)))
    return frame.sort_values(["concept", "end", "filed"], kind="stable").reset_index(drop=True)


def _coerce_fact_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for column in _STR_FACT_COLUMNS:
        out[column] = out[column].astype("str")  # pandas-3 `str` dtype keeps missing as NaN
    out["val"] = pd.to_numeric(out["val"], errors="coerce").astype("float64")
    out["fy"] = pd.to_numeric(out["fy"], errors="coerce").astype("Int64")
    return out


def parse_frame(frame: Any) -> tuple[int, int | None, bool] | None:
    """Decode an XBRL calendar frame: ``CY2025Q2`` -> (2025, 2, False), ``CY2025`` ->
    (2025, None, False), ``CY2025Q2I`` -> (2025, 2, True). ``None`` when not a frame."""
    if not isinstance(frame, str):
        return None
    match = FRAME_RE.match(frame)
    if match is None:
        return None
    quarter = match.group("quarter")
    return int(match.group("year")), int(quarter) if quarter else None, bool(match.group("instant"))


# Calendar quarters are 90-92 days long, so a period end is at most 46 days from the nearest
# calendar quarter end; 45 leaves only the dead-centre dates unassigned.
QUARTER_END_TOLERANCE_DAYS = 45
# A fiscal quarter is 12 to 14 weeks (84-98 days). The slack absorbs month-end conventions
# but can never admit the ~180 days left behind by a missing 10-Q.
QUARTER_LENGTH_DAYS = (80, 100)
_QUARTER_END_MONTH_DAY: tuple[tuple[int, int], ...] = ((3, 31), (6, 30), (9, 30), (12, 31))


def _iso_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):  # NaN for a missing date, or text that is not a date
        return None


def calendar_quarter_for(end: str) -> tuple[int, int] | None:
    """Calendar ``(year, quarter)`` whose quarter-end date is nearest to the period end.

    This is how SEC places fiscal periods into ``CY`` frames, so an unframed quarter of a
    January-year-end filer that ends 2026-04-26 lands in 2026Q1, next to SEC's own frames.
    ``None`` when ``end`` is not an ISO date or is more than ``QUARTER_END_TOLERANCE_DAYS``
    from every calendar quarter end.
    """
    day = _iso_date(end)
    if day is None:
        return None

    def days_away(candidate: tuple[int, int]) -> int:
        year, quarter = candidate
        return abs((dt.date(year, *_QUARTER_END_MONTH_DAY[quarter - 1]) - day).days)

    # early-January period ends belong to the previous year's Q4
    candidates = [(day.year - 1, 4), *((day.year, quarter) for quarter in (1, 2, 3, 4))]
    nearest = min(candidates, key=days_away)
    return nearest if days_away(nearest) <= QUARTER_END_TOLERANCE_DAYS else None


def _quarters_from_ytd(rows: pd.DataFrame) -> dict[str, tuple[str, float]]:
    """Quarterly flows recovered from unframed year-to-date facts: ``period -> (end, val)``.

    A 10-Q reports cash-flow items cumulatively from the fiscal-year start, so SEC frames
    only the 3-month Q1 figure and the full year. Facts that share a ``start`` are one
    cumulative series: the change between consecutive ``end`` dates is the flow of that
    window. A window that is not about one quarter long (a 10-Q is missing, or the full
    year stands alone) is skipped rather than guessed. ``rows`` must be sorted by ``end``
    then ``filed`` so that a restated period ends up with its latest filed value.
    """
    cumulative: dict[str, dict[str, float]] = {}  # start -> {end: val}
    for row in rows.itertuples(index=False):
        if _iso_date(row.start) is None or _iso_date(row.end) is None or pd.isna(row.val):
            continue
        cumulative.setdefault(row.start, {})[row.end] = float(row.val)

    reported: dict[str, tuple[str, float]] = {}
    differenced: dict[str, tuple[str, float]] = {}
    shortest, longest = QUARTER_LENGTH_DAYS
    for start, by_end in cumulative.items():
        # the day before `start`, so the first window is measured end to end like the rest
        previous_day = dt.date.fromisoformat(start) - dt.timedelta(days=1)
        previous_val = 0.0
        for position, (end, val) in enumerate(sorted(by_end.items())):
            end_day = dt.date.fromisoformat(end)
            quarter = calendar_quarter_for(end)
            if quarter is not None and shortest <= (end_day - previous_day).days <= longest:
                found = reported if position == 0 else differenced
                found[f"{quarter[0]}Q{quarter[1]}"] = (end, val - previous_val)
            previous_day, previous_val = end_day, val
    # a quarter the filer reported on its own beats one obtained by subtraction
    return differenced | reported


def _is_instant(concept: str, rows: pd.DataFrame) -> bool:
    if concept in INSTANT_CONCEPTS:
        return True
    if concept in FLOW_CONCEPTS:
        return False
    # Custom concept: balance-sheet facts carry no `start` date.
    return len(rows) > 0 and bool(rows["start"].isna().all())


def _empty_series() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "period": pd.Series(dtype="str"),
            "end": pd.Series(dtype="str"),
            "val": pd.Series(dtype="float64"),
            "derived": pd.Series(dtype="bool"),
        }
    )


def calendar_series(facts: pd.DataFrame, concept: str, freq: str = "Q") -> pd.DataFrame:
    """Calendar-period series for one concept, built from SEC's de-duplicated ``frame`` tags.

    Returns columns ``period`` ('2025Q2' or '2025'), ``end`` (YYYY-MM-DD), ``val`` (float),
    ``derived`` (bool), sorted by period.

    * Flow concepts (revenue, capex, ...) use ``CYyyyyQn`` frames for ``freq="Q"`` and
      ``CYyyyy`` for ``freq="A"``. SEC frames a quarter only when a fact covers exactly that
      quarter, which leaves gaps: cash-flow items are reported year-to-date (only Q1 is
      framed) and 10-Ks report full years (Q4 is rarely framed). Quarters without a frame
      are filled, flagged ``derived=True``, first by differencing the year-to-date facts
      (``_quarters_from_ytd``) and then, for a Q4 still missing, as FY - Q1 - Q2 - Q3. A
      framed point is never replaced and a gap that is not one quarter long stays a gap.
    * Instant concepts (balance-sheet items) use ``CYyyyyQnI`` frames; the annual series
      takes the Q4 instant as the year-end balance. Nothing is derived.
    * Otherwise values without a frame (duplicated comparatives, superseded originals) are
      ignored; when a frame or period appears more than once the latest filed value wins.
    """
    if freq not in ("Q", "A"):
        raise ValueError(f"freq must be 'Q' or 'A', got {freq!r}")
    rows = facts[facts["concept"] == concept]
    if rows.empty:
        return _empty_series()
    instant = _is_instant(concept, rows)
    rows = rows.sort_values(["end", "filed"], kind="stable")

    points: dict[str, tuple[str, float, bool]] = {}  # period -> (end, val, derived)
    annual: dict[int, tuple[str, float]] = {}  # year -> (end, val), for Q4 derivation
    for row in rows.itertuples(index=False):
        parsed = parse_frame(row.frame)
        if parsed is None or pd.isna(row.val):
            continue
        year, quarter, is_instant_frame = parsed
        if is_instant_frame != instant:
            continue  # a flow concept ignores instant frames and vice versa
        value = float(row.val)
        end = str(row.end)
        if freq == "Q" and quarter is not None:
            points[f"{year}Q{quarter}"] = (end, value, False)
        elif freq == "A" and quarter is None:
            points[str(year)] = (end, value, False)
        elif freq == "A" and instant and quarter == 4:
            points[str(year)] = (end, value, False)
        elif freq == "Q" and quarter is None:
            annual[year] = (end, value)

    if freq == "Q" and not instant:
        for period, (end, value) in _quarters_from_ytd(rows).items():
            points.setdefault(period, (end, value, True))  # never replaces a framed point
        # Fallback for a filer that tags only 3-month values: there is no 9-month figure to
        # subtract, so Q4 is the full year less the three quarters. That only holds when the
        # fiscal year is that calendar year; a June year-end is not the sum of Q1..Q4.
        for year, (end, fy_value) in annual.items():
            if calendar_quarter_for(end) != (year, 4):
                continue
            quarters = [points.get(f"{year}Q{q}") for q in (1, 2, 3)]
            if f"{year}Q4" not in points and all(q is not None for q in quarters):
                q4 = fy_value - sum(q[1] for q in quarters if q is not None)
                points[f"{year}Q4"] = (end, q4, True)

    if not points:
        return _empty_series()
    table = pd.DataFrame(
        [
            {"period": p, "end": e, "val": v, "derived": d}
            for p, (e, v, d) in sorted(points.items())
        ],
        columns=list(SERIES_COLUMNS),
    )
    table["period"] = table["period"].astype("str")
    table["end"] = table["end"].astype("str")
    table["val"] = table["val"].astype("float64")
    table["derived"] = table["derived"].astype("bool")
    return table.reset_index(drop=True)


def load_processed_facts(ticker: str, processed_dir: Path = PROCESSED_DIR) -> pd.DataFrame | None:
    """Read ``processed/<TICKER>/reported.csv`` back with the dtypes ``facts_to_frame`` uses.

    Returns ``None`` when the file does not exist (offline runs before any refresh).
    """
    path = Path(processed_dir) / ticker.upper() / "reported.csv"
    if not path.is_file():
        return None
    frame = pd.read_csv(path, encoding="utf-8", dtype=_FACT_DTYPES)
    return frame.reindex(columns=list(FACT_COLUMNS))
