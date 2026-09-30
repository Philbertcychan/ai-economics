"""LBNL "Queued Up" client and tidy helpers: US interconnection queues, one row per request.

What the source is
------------------
Lawrence Berkeley National Laboratory publishes "Queued Up", a yearly study of the generation
and storage projects waiting to connect to the US grid, with the data behind it as one Excel
workbook (about 15 MB, CC BY 4.0, attributed to LBNL and GridTracker) listed at
https://emp.lbl.gov/queues. The sheet ``03. Complete Queue Data`` holds one row per
interconnection request, about 38,200 of them across the seven ISOs and 50 non-ISO balancing
areas (``01. Balancing Areas`` lists the 57 entities), from the late 1990s through the end of
the data year. The sheet ``04. Data Codebook`` defines the fields; its definitions, carried
here so the tidy table can be read without the workbook:

``q_id``           queue position / ID number; with ``entity`` it identifies a request
``q_status``       current queue status: active, withdrawn, suspended or operational (the 2025
                   file also carries ten rows marked ``unknown``, which the codebook omits)
``q_date``         interconnection request date (the date the project entered the queue)
``prop_date``      proposed online date from the interconnection application; can be revised
``on_date``        date the project became operational, if it did
``wd_date``        date the project withdrew from the queue, if it did
``ia_date``        date of the signed interconnection agreement, if there is one
``IA_phase_raw``   the queue's own, non-standardised study phase or status
``IA_phase_clean`` the standardised phase: IA Executed (imputed when the status is
                   operational), Withdrawn, System Impact Study, Feasibility Study, Facility
                   Study, In Progress (unknown study), Cluster Study, IA Pending, Not Started,
                   Construction or Suspended
``county``         county of the project; the first listed when a request spans several
``state``          state of the project
``fips_code``      county FIPS code, kept as text (two codes run together for a request
                   that spans two counties)
``poi_name``       point of interconnection: a substation or a transmission line tap
``region``         standardised region: CAISO, ERCOT, ISO-NE, MISO, NYISO, PJM, SPP, or the
                   non-ISO West or Southeast
``project_name``   project name; blank for most requests
``utility``        utility name; the same as ``entity`` outside the ISOs
``entity``         transmission provider (ISO or utility): one of the 57 balancing areas
``developer``      non-standardised developer name; blank for most requests
``cluster``        queue cluster, where the queue has them
``service``        interconnection service: NRIS, ERIS, NRIS/ERIS or Other
``project_type``   Generation, Surplus, Upgrade or Replacement; not every uprate is identified
``type_1..3``      resource types; a second or third type marks a hybrid or co-located project
                   (Solar, Wind, Battery, Gas, Hydro, Coal, Offshore Wind, Nuclear,
                   Geothermal, Diesel, Oil, Hydrogen, Other Storage, Other)
``type_clean``     the standardised resource type, combined for hybrids (``Solar+Battery``)
``mw_1..3``        rated capacity of each type in MW. The codebook notes that the published
                   report imputes storage capacity for hybrids whose ``mw_2``/``mw_3`` are
                   blank and that those imputed values are not in this file, so capacity here
                   is what the queues state, no more
``q_year``         year the project entered the queue, from ``q_date``
``prop_year``      proposed online year, from ``prop_date``

Why it matters for the grid stage
---------------------------------
The power stage's EIA-860M tables say what is planned and being built; this file says what is
waiting for a grid connection and how long that wait has been. For a data-centre buyer the
useful cuts are the active capacity by region and type (how much gas sits in which queue), the
part of it that already holds an executed interconnection agreement (the closest to built), the
historical completion rate (most requested capacity is withdrawn, not built) and the median
time from request to operation, which has lengthened to around five years.

Cadence and caveats
-------------------
* Yearly. LBNL posts one file a year, in spring, with data through the previous December, so
  the figures are up to eighteen months old. A manual ``uv run scripts/pull_queues.py`` when
  the new file appears is enough; the daily refresh does not run it.
* The index page https://emp.lbl.gov/queues answers 403 to non-browser user agents, so the
  client takes the workbook URL as a constant (``FILE_URL``, with a ``--url`` override on the
  command line) and never scrapes the index. When the URL changes, the constant does.
* Requested capacity is not capacity that will be built: the completion rate says that plainly,
  and ``q_status`` says where each request stands.
* Hybrids. ``mw`` here is ``mw_1 + mw_2 + mw_3`` and a hybrid counts once under its combined
  ``type_clean`` (``Solar+Battery``) with all of its capacity. The site folds each type into
  five groups by its first-listed resource (``group_type``), so hybrid storage lands with the
  generator it is paired with and ``Battery`` means standalone batteries. LBNL's own charts
  split hybrids into their components and add the imputed storage this file leaves out, so
  their type totals differ from these.
* Data quirks kept as found: a handful of requests carry a negative capacity (derates and
  data-entry oddities in ISO-NE, MISO and ERCOT); they are kept, logged, and count in the sums
  as the file states them. About 1,500 operational rows have no ``on_date`` and drop out of
  the time-to-operation medians. Blank states and years stay blank.

What the module does
--------------------
1. ``LBNLQueuesClient`` downloads the workbook politely (one descriptive User-Agent, a pause
   between requests, retries on 429/5xx) into ``data/raw/LBNL_QUEUES/<UTC date>/`` and writes
   a manifest with a sha256 per file. A body that is not a workbook holding the data sheet is
   never stored; a cached copy is checked against the manifest before it is reused. Network
   access goes through one injectable ``fetch(url, headers) -> bytes`` callable, which is
   also how a copy fetched by hand is stored through the same path.
2. ``read_requests`` opens the workbook once, finds the data sheet's header row by its ``q_id``
   cell and hands the rows to ``tidy_rows``, which validates (required columns, statuses,
   regions and phases in their known sets, dates that parse, capacities that are numbers,
   two-letter states) and returns snake_case columns with plain dtypes plus the derived ``mw``.
3. The summaries are pure functions of that frame: ``active_by_region_and_type``,
   ``active_by_proposed_year_and_type``, ``ia_executed_not_operational``,
   ``completion_rates``, ``median_months_to_operation`` and ``active_by_state_and_type``.
4. ``process_workbook`` writes the processed folder (``requests.csv`` without the four
   free-text columns, ``summaries/*.csv``, ``source.json`` last); ``read_processed`` reads the
   request table back with its dtypes and ``read_summaries`` hands the site the four summaries
   the grid page shows.

There is no model logic here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook

from data import QUEUES_RAW_DIR
from data.edgar import USER_AGENT_ENV, sha256_file, utc_now_iso, utc_today

# Two helpers that know nothing about EIA: reading an .xlsx's sheet names from its zip directory
# and reading a dated raw folder's manifest. Shared rather than copied so the two public-workbook
# sources keep one manifest format.
from data.eia import raw_provenance, workbook_sheet_names

log = logging.getLogger(__name__)

# --- LBNL access policy ----------------------------------------------------------------------

SOURCE = "LBNL_QUEUES"
INDEX_URL = "https://emp.lbl.gov/queues"  # linked from the site; never fetched (see the docstring)
FILE_URL = "https://emp.lbl.gov/sites/default/files/2026-05/LBNL_Ix_Queue_Data_File_thru2025.xlsx"
LICENSE = "CC BY 4.0"
ATTRIBUTION = "LBNL and GridTracker"
DATA_SHEET = "03. Complete Queue Data"
CODEBOOK_SHEET = "04. Data Codebook"
DEFAULT_USER_AGENT = (
    "ai-economics/0.1 open research model (https://github.com/Philbertcychan/ai-economics)"
)
QUEUES_USER_AGENT_ENV = "LBNL_USER_AGENT"
MIN_REQUEST_INTERVAL_S = 1.0
MAX_RETRIES = 3  # extra attempts after the first, for 429 / 5xx / network errors
RETRY_BACKOFF_S = 1.0
REQUEST_TIMEOUT_S = 120.0  # a 15 MB workbook on a slow link


def user_agent() -> str:
    """The User-Agent sent to LBNL: ``LBNL_USER_AGENT``, else the SEC address, else the default."""
    for variable in (QUEUES_USER_AGENT_ENV, USER_AGENT_ENV):
        configured = os.environ.get(variable, "").strip()
        if configured:
            return configured
    return DEFAULT_USER_AGENT


_resolve_user_agent = user_agent  # ``__init__`` has a parameter of the same name


class QueuesError(RuntimeError):
    """An LBNL request or file failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


# --- HTTP ------------------------------------------------------------------------------------


def _retry_delay(retry_after: str | None, attempt: int) -> float:
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
    """The stdlib ``fetch(url, headers) -> bytes`` used when none is injected.

    Retries 429, 5xx and network errors up to ``MAX_RETRIES`` times with backoff; any other
    HTTP error becomes a ``QueuesError``. A 403 is reported as such: it is what the LBNL site
    answers to a client it does not take for a browser, and the fix is the User-Agent or a copy
    fetched by hand, not a retry.
    """

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        attempt = 0
        while True:
            request = urllib.request.Request(url, headers=headers)
            try:
                with urlopen(request, timeout=timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as err:
                retryable = err.code == 429 or err.code >= 500
                if not retryable or attempt >= MAX_RETRIES:
                    raise QueuesError(
                        f"HTTP {err.code} for {url}", status=err.code, url=url
                    ) from err
                retry_after = err.headers.get("Retry-After") if err.headers is not None else None
                delay = _retry_delay(retry_after, attempt)
                reason: object = err.code
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
            ) as err:
                reason = getattr(err, "reason", None) or repr(err)
                if attempt >= MAX_RETRIES:
                    raise QueuesError(f"network error for {url}: {reason}", url=url) from err
                delay = _retry_delay(None, attempt)
            attempt += 1
            log.warning(
                "LBNL request failed (%s); retry %d/%d in %.1fs: %s",
                reason,
                attempt,
                MAX_RETRIES,
                delay,
                url,
            )
            sleep(delay)

    return fetch


# --- Workbook bytes ---------------------------------------------------------------------------

_PART_SUFFIX = ".part"  # a download in progress; never listed in a manifest, never a cache hit


def workbook_name(url: str) -> str:
    """The file name a URL ends in; the workbook's identity in the raw folder and source.json."""
    return Path(urllib.parse.urlsplit(url).path).name


_THROUGH_RE = re.compile(r"thru(?P<year>\d{4})", re.IGNORECASE)


def through_from_name(name: str) -> int | None:
    """``LBNL_Ix_Queue_Data_File_thru2025.xlsx`` -> ``2025``; ``None`` when the name is silent."""
    match = _THROUGH_RE.search(Path(str(name)).name)
    return int(match.group("year")) if match else None


def check_workbook_bytes(data: bytes, what: str) -> None:
    """``QueuesError`` unless ``data`` is an ``.xlsx`` that carries the data sheet.

    An HTML error page served with status 200, or a truncated body, would otherwise be stored
    under the workbook's name and reused from the cache for the rest of the day.
    """
    try:
        names = workbook_sheet_names(data)
    except ValueError as exc:
        raise QueuesError(f"{what}: {exc}") from exc
    if DATA_SHEET not in names:
        raise QueuesError(f"{what}: workbook lacks sheet {DATA_SHEET!r}; found {names}")


# --- Client -----------------------------------------------------------------------------------

_PROVENANCE_KEYS: tuple[str, ...] = ("source_url", "fetched_at")


def _manifest_entries(dated_dir: Path) -> dict[str, dict]:
    """``path -> entry`` from a dated directory's manifest; empty when missing or corrupt."""
    try:
        with open(dated_dir / "manifest.json", encoding="utf-8") as fh:
            files = json.load(fh).get("files", [])
        return {entry["path"]: entry for entry in files}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


class LBNLQueuesClient:
    """Paced, cache-first client for the LBNL workbook.

    Everything downloaded lands in ``raw_dir / YYYY-MM-DD`` (the UTC date). Within one dated
    directory a second ``download`` of the same file returns the cached copy without touching
    the network. A copy fetched by hand (the site refuses some clients) is stored through the
    same path by constructing the client with a ``fetch`` that reads the local file, so the
    manifest records its sha256 and the URL it stands for.
    """

    def __init__(
        self,
        raw_dir: Path = QUEUES_RAW_DIR,
        *,
        user_agent: str | None = None,
        fetch: Callable[[str, dict[str, str]], bytes] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        today: dt.date | None = None,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.today = today or utc_today()
        self._user_agent = user_agent or _resolve_user_agent()
        self._fetch = fetch or build_default_fetch(sleep)
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        self._sources: dict[Path, dict[str, Any]] = {}

    def headers(self) -> dict[str, str]:
        """Headers sent with every request; the workbook is a zip already, so no encoding."""
        return {"User-Agent": self._user_agent}

    def cache_dir(self) -> Path:
        """``raw_dir / YYYY-MM-DD``, created on first use."""
        path = self.raw_dir / self.today.isoformat()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            wait = MIN_REQUEST_INTERVAL_S - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def get_bytes(self, url: str) -> bytes:
        """Fetch a URL with the pause between requests and the client's headers."""
        self._throttle()
        log.debug("GET %s", url)
        return self._fetch(url, self.headers())

    def cached_path(self, url: str) -> Path | None:
        """Newest local copy of the file ``url`` names, in any dated directory, else ``None``."""
        name = workbook_name(url)
        if not self.raw_dir.is_dir():
            return None
        dated_dirs = (p for p in self.raw_dir.iterdir() if p.is_dir())
        for dated in sorted(dated_dirs, reverse=True):
            candidate = dated / name
            if candidate.is_file():
                return candidate
        return None

    def download(self, url: str = FILE_URL) -> Path:
        """Store the workbook at ``url`` under today's directory and return its path.

        The body is checked to be a workbook with the data sheet before anything is written
        (to a ``.part`` name, then renamed), so a bad response is never cached. A copy already
        in today's directory is returned as is once it matches today's manifest and still opens
        as a workbook; a mismatch is a ``QueuesError``, because raw files are never edited.
        """
        name = workbook_name(url)
        if not name.lower().endswith(".xlsx"):
            raise QueuesError(f"expected a link to an .xlsx workbook, got {url}", url=url)
        root = self.cache_dir()
        target = root / name
        if target.exists():
            digest = sha256_file(target)
            recorded = _manifest_entries(root).get(name, {}).get("sha256")
            if recorded is not None and recorded != digest:
                raise QueuesError(
                    f"{target} does not match today's manifest (sha256 {digest[:12]}... on disk, "
                    f"{recorded[:12]}... recorded); raw files are never edited, so move today's "
                    "folder aside and pull again",
                    url=url,
                )
            check_workbook_bytes(target.read_bytes(), str(target))
            log.info("using cached %s", target)
            return target
        earlier = self.cached_path(url)
        body = self.get_bytes(url)
        check_workbook_bytes(body, url)
        digest = hashlib.sha256(body).hexdigest()
        if earlier is not None and sha256_file(earlier) == digest:
            log.info("%s is byte-identical to %s", name, earlier)
        partial = target.with_name(name + _PART_SUFFIX)
        partial.write_bytes(body)
        os.replace(partial, target)
        self._sources[target.resolve()] = {"source_url": url, "fetched_at": utc_now_iso()}
        return target

    def write_manifest(self) -> Path:
        """Write ``cache_dir/manifest.json`` listing every cached file with its sha256.

        Files fetched by this instance carry their source URL and fetch time; entries from a
        manifest written earlier the same day are carried forward when the bytes still match.
        """
        root = self.cache_dir()
        manifest_path = root / "manifest.json"
        previous = _manifest_entries(root)
        files = []
        paths = [
            p
            for p in root.rglob("*")
            if p.is_file() and p != manifest_path and not p.name.endswith(_PART_SUFFIX)
        ]
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
        manifest = {"source": SOURCE, "pulled_at": utc_now_iso(), "files": files}
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        return manifest_path


# --- Tidy the request table -------------------------------------------------------------------

HEADER_CELL = "q_id"  # first cell of the header row, below the "RETURN TO CONTENTS" title row

# source column -> tidy column, in the workbook's order
SOURCE_COLUMNS: dict[str, str] = {
    "q_id": "q_id",
    "q_status": "q_status",
    "q_date": "q_date",
    "prop_date": "prop_date",
    "on_date": "on_date",
    "wd_date": "wd_date",
    "ia_date": "ia_date",
    "IA_phase_raw": "ia_phase_raw",
    "IA_phase_clean": "ia_phase_clean",
    "county": "county",
    "state": "state",
    "fips_code": "fips_code",
    "poi_name": "poi_name",
    "region": "region",
    "project_name": "project_name",
    "utility": "utility",
    "entity": "entity",
    "developer": "developer",
    "cluster": "cluster",
    "service": "service",
    "project_type": "project_type",
    "type_1": "type_1",
    "type_2": "type_2",
    "type_3": "type_3",
    "type_clean": "type_clean",
    "mw_1": "mw_1",
    "mw_2": "mw_2",
    "mw_3": "mw_3",
    "q_year": "q_year",
    "prop_year": "prop_year",
}
MW_COLUMNS: tuple[str, ...] = ("mw_1", "mw_2", "mw_3")
DATE_COLUMNS: tuple[str, ...] = ("q_date", "prop_date", "on_date", "wd_date", "ia_date")
# fips_code stays text: a request spanning two counties carries both codes run together.
INT_COLUMNS: tuple[str, ...] = ("q_year", "prop_year")
# ``mw`` is derived: the sum of the three capacities, blank only when all three are blank.
TIDY_COLUMNS: tuple[str, ...] = (*SOURCE_COLUMNS.values(), "mw")
TEXT_COLUMNS: tuple[str, ...] = tuple(
    c for c in TIDY_COLUMNS if c not in (*MW_COLUMNS, *DATE_COLUMNS, *INT_COLUMNS, "mw")
)
# Free text that would double the committed CSV's size and that no summary reads; the tidy frame
# keeps it, ``requests.csv`` does not.
FREE_TEXT_COLUMNS: tuple[str, ...] = ("poi_name", "project_name", "developer", "cluster")
REQUESTS_COLUMNS: tuple[str, ...] = tuple(c for c in TIDY_COLUMNS if c not in FREE_TEXT_COLUMNS)
# Region, entity and queue ID identify a request; every other column follows as a tie-break so
# the sorted table depends on the data alone, never on LBNL's row order.
_SORT_COLUMNS: tuple[str, ...] = (
    "region",
    "entity",
    "q_id",
    *(c for c in TIDY_COLUMNS if c not in ("region", "entity", "q_id")),
)

# The codebook's vocabularies. The summaries filter on these, so a value outside them is an
# error rather than a request silently counted in nothing.
STATUSES: tuple[str, ...] = ("active", "withdrawn", "suspended", "operational", "unknown")
REGIONS: tuple[str, ...] = (
    "CAISO",
    "ERCOT",
    "ISO-NE",
    "MISO",
    "NYISO",
    "PJM",
    "SPP",
    "Southeast",
    "West",
)
ISOS: tuple[str, ...] = REGIONS[:7]  # the two after them are the non-ISO regions
IA_PHASES: tuple[str, ...] = (
    "IA Executed",
    "Withdrawn",
    "System Impact Study",
    "Feasibility Study",
    "Facility Study",
    "In Progress (unknown study)",
    "Cluster Study",
    "IA Pending",
    "Not Started",
    "Construction",
    "Suspended",
)
IA_EXECUTED = "IA Executed"
ACTIVE = "active"
OPERATIONAL = "operational"
# Queue entry years run from the 1990s; proposed years reach 2057 in the 2025 file. Outside this
# span a cell is a typo or a serial number, not a year.
YEAR_RANGE: tuple[int, int] = (1900, 2100)
_STATE_RE = re.compile(r"^[A-Za-z]{2}$")
# Blank labels in a summary would read back as NaN and vanish from a groupby; this names them.
NOT_STATED = "Not stated"
TOTAL = "Total"
MW_PER_GW = 1000.0
GW_DECIMALS = 3  # a thousandth of a GW is a MW, the file's own precision
SHARE_DECIMALS = 3
MONTH_DECIMALS = 1
DAYS_PER_MONTH = 365.25 / 12

# The five columns the site shows, and the first-listed resource that lands a type in each.
# A hybrid follows its first resource: ``Solar+Battery`` is Solar, so hybrid storage sits with
# the generator it is paired with and Battery means standalone batteries. Everything else
# (hydro, coal, nuclear, geothermal, oil, diesel, hydrogen, other storage, other) is Other.
TYPE_GROUPS: tuple[str, ...] = ("Gas", "Solar", "Wind", "Battery", "Other")
_TYPE_GROUP_OF: dict[str, str] = {
    "gas": "Gas",
    "solar": "Solar",
    "wind": "Wind",
    "offshore wind": "Wind",
    "battery": "Battery",
}


def _is_blank(value: Any) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return isinstance(value, pd.Timestamp) and pd.isna(value)


def group_type(type_clean: Any) -> str:
    """One of ``TYPE_GROUPS`` for a ``type_clean`` value.

    ``"Gas"`` -> ``"Gas"``, ``"Solar+Battery"`` -> ``"Solar"``, ``"Offshore Wind"`` ->
    ``"Wind"``, ``"Battery+Other Storage"`` -> ``"Battery"``, ``"Other Storage"`` and ``"Hydro"``
    -> ``"Other"``. Case and whitespace do not matter; a blank or unknown type is Other.
    """
    text = "" if _is_blank(type_clean) else str(type_clean)
    primary = text.split("+", 1)[0].strip().casefold()
    return _TYPE_GROUP_OF.get(primary, "Other")


def find_header_row(rows: Sequence[Sequence[Any]]) -> int:
    """Index of the first row whose first cell is ``q_id``; ``-1`` when there is none."""
    for index, row in enumerate(rows):
        first = row[0] if row else None
        if isinstance(first, str) and first.strip() == HEADER_CELL:
            return index
    return -1


def _problem(message: str) -> ValueError:
    return ValueError(f"sheet {DATA_SHEET!r}: {message}")


def _to_text(series: pd.Series) -> pd.Series:
    """Text with blanks as ``""``; a date-typed cell (nine queue IDs are) prints as its date."""

    def text(value: Any) -> str:
        if _is_blank(value):
            return ""
        if isinstance(value, dt.datetime):
            return value.date().isoformat() if value.time() == dt.time() else value.isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        return str(value).strip()

    return series.map(text).astype("str")


def _to_numbers(text: pd.Series, source: str) -> pd.Series:
    """Float64 with NaN for blanks; a bool, non-numeric text or an infinite value is an error."""
    blank = text.map(_is_blank)
    bools = text.map(lambda v: isinstance(v, bool))
    if bools.any():
        raise _problem(f"{source!r} has TRUE/FALSE cells on rows {text.index[bools].tolist()[:5]}")
    numbers = pd.to_numeric(text.where(~blank, None), errors="coerce").astype("float64")
    bad = text[numbers.isna() & ~blank]
    if not bad.empty:
        raise _problem(f"{source!r} has non-numeric values {bad.head(5).tolist()}")
    infinite = numbers[numbers.abs() == math.inf]
    if not infinite.empty:
        raise _problem(f"{source!r} has infinite values on rows {infinite.index.tolist()[:5]}")
    return numbers


def _to_integer(text: pd.Series, source: str, span: tuple[int, int]) -> pd.Series:
    """Nullable integers; a fraction or a value outside ``span`` is an error naming the column."""
    numbers = _to_numbers(text, source)
    fractional = numbers[~numbers.isna() & (numbers != numbers.round())]
    if not fractional.empty:
        raise _problem(f"{source!r} has non-integer values {fractional.head(5).tolist()}")
    outside = numbers[(numbers < span[0]) | (numbers > span[1])]
    if not outside.empty:
        shown = [int(v) for v in outside.head(5)]
        raise _problem(f"{source!r} has values outside {span[0]} to {span[1]}: {shown}")
    return numbers.round().astype("Int64")


def _to_dates(text: pd.Series, source: str) -> pd.Series:
    """``datetime64[ns]`` at midnight, NaT for blanks; a cell that is not a date is an error."""
    blank = text.map(_is_blank)
    dates = pd.to_datetime(text.where(~blank, None), errors="coerce")
    bad = text[dates.isna() & ~blank]
    if not bad.empty:
        raise _problem(f"{source!r} has values that are not dates {bad.head(5).tolist()}")
    return dates.dt.normalize().astype("datetime64[ns]")


def _check_vocabulary(series: pd.Series, source: str, allowed: Iterable[str]) -> None:
    unknown = sorted(set(series[(series != "") & ~series.isin(tuple(allowed))]))
    if unknown:
        raise _problem(f"{source!r} has values outside {list(allowed)}: {unknown[:5]}")


def tidy_rows(rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
    """Turn the data sheet's grid of cells into the frame ``read_requests`` documents.

    Pure: rows in, frame out. ``ValueError`` naming the sheet and the column when the header
    row is missing, a source column is absent or duplicated, a status, region or standardised
    phase is outside the codebook's vocabulary, a date does not parse, a capacity is not a
    number, a year or FIPS code is not a whole number in range, or a state is not two letters.
    A negative capacity is kept and logged (the 2025 file carries sixteen), because a derate
    recorded as a negative request is the file's statement, not a malformed sheet.
    """
    header_index = find_header_row(rows)
    if header_index < 0:
        raise _problem(f"no header row starting with {HEADER_CELL!r}")
    header = [str(c).strip() if c is not None else "" for c in rows[header_index]]
    missing = [c for c in SOURCE_COLUMNS if c not in header]
    if missing:
        raise _problem(f"missing columns {missing}")
    duplicates = sorted({c for c in header if c and header.count(c) > 1})
    if duplicates:
        raise _problem(f"duplicate columns {duplicates}")

    width = len(header)
    numbers, data = [], []
    for number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        cells = list(row)[:width] + [None] * max(0, width - len(row))
        if all(_is_blank(c) for c in cells):
            continue  # a trailing blank line
        numbers.append(number)  # the sheet row, 1-based, so an error can name it
        data.append(cells)
    grid = pd.DataFrame(data, columns=header, index=numbers, dtype="object")

    out = pd.DataFrame(index=grid.index)
    for source, name in SOURCE_COLUMNS.items():
        out[name] = grid[source]
    sources = {name: source for source, name in SOURCE_COLUMNS.items()}
    for name in TEXT_COLUMNS:
        out[name] = _to_text(out[name])
    for name in DATE_COLUMNS:
        out[name] = _to_dates(out[name], sources[name])
    for name in MW_COLUMNS:
        out[name] = _to_numbers(out[name], sources[name])
        negative = out.index[out[name] < 0].tolist()
        if negative:
            log.warning(
                "%s has %d negative values (kept as found) on rows %s",
                sources[name],
                len(negative),
                negative[:5],
            )
    spans = {"q_year": YEAR_RANGE, "prop_year": YEAR_RANGE}
    for name in INT_COLUMNS:
        out[name] = _to_integer(out[name], sources[name], spans[name])
    _check_vocabulary(out["q_status"], "q_status", STATUSES)
    _check_vocabulary(out["region"], "region", REGIONS)
    _check_vocabulary(out["ia_phase_clean"], "IA_phase_clean", IA_PHASES)
    bad_state = out.index[(out["state"] != "") & ~out["state"].str.match(_STATE_RE)].tolist()
    if bad_state:
        shown = out.loc[bad_state[:5], "state"].tolist()
        raise _problem(f"'state' has values that are not two letters {shown}")
    blank_status = out.index[out["q_status"] == ""].tolist()
    if blank_status:
        raise _problem(f"'q_status' is blank on rows {blank_status[:5]}")
    out["mw"] = out[list(MW_COLUMNS)].sum(axis=1, min_count=1)

    out = out[list(TIDY_COLUMNS)]
    return out.sort_values(list(_SORT_COLUMNS), na_position="last", kind="stable").reset_index(
        drop=True
    )


def _sheet_rows(path: Path, sheet: str) -> list[tuple[Any, ...]]:
    """One sheet's rows, read-only and with formulas evaluated; ``ValueError`` when absent.

    The ``<dimension>`` record is reset first so a workbook that understates a sheet's extent
    is not silently truncated.
    """
    path = Path(path)
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet not in workbook.sheetnames:
            raise ValueError(f"{path.name}: no sheet {sheet!r}; sheets: {workbook.sheetnames}")
        worksheet = workbook[sheet]
        worksheet.reset_dimensions()
        return list(worksheet.iter_rows(values_only=True))
    finally:
        workbook.close()


def read_requests(path: Path) -> pd.DataFrame:
    """The data sheet of an LBNL workbook as a tidy frame with columns ``TIDY_COLUMNS``.

    Text columns are ``str`` with blanks as ``""``; the five dates ``datetime64[ns]`` (NaT when
    blank); ``mw_1``..``mw_3`` and the derived ``mw`` ``float64`` (NaN when blank);
    ``fips_code``, ``q_year`` and ``prop_year`` nullable ``Int64``. Rows are sorted by region,
    entity and queue ID, then every other column.
    """
    return tidy_rows(_sheet_rows(path, DATA_SHEET))


def data_year(requests: pd.DataFrame) -> int:
    """The year the data runs through: the latest queue-entry year in the table."""
    _require(requests, ("q_year",), "data_year")
    years = requests["q_year"].dropna()
    if years.empty:
        raise ValueError("data_year: no request has a queue-entry year")
    return int(years.max())


def read_processed(path: Path) -> pd.DataFrame:
    """``requests.csv`` read back with the dtypes ``read_requests`` gives (minus the free text).

    Text stays text with blanks as ``""`` (a default read would turn a state of ``NA`` into a
    missing value), dates are parsed, the years and FIPS codes are nullable integers.
    ``ValueError`` when the header is not ``REQUESTS_COLUMNS``.
    """
    path = Path(path)
    header = list(pd.read_csv(path, nrows=0, encoding="utf-8").columns)
    if header != list(REQUESTS_COLUMNS):
        raise ValueError(f"{path.name}: columns {header} are not {list(REQUESTS_COLUMNS)}")
    text = [c for c in TEXT_COLUMNS if c in REQUESTS_COLUMNS]
    numeric = (*INT_COLUMNS, *MW_COLUMNS, "mw", *DATE_COLUMNS)
    frame = pd.read_csv(
        path,
        encoding="utf-8",
        dtype={
            **{c: "str" for c in text},
            **{c: "Int64" for c in INT_COLUMNS},
            **{c: "float64" for c in (*MW_COLUMNS, "mw")},
            **{c: "str" for c in DATE_COLUMNS},
        },
        keep_default_na=False,
        na_values={c: [""] for c in numeric},
    )
    for name in text:
        frame[name] = frame[name].fillna("").astype("str")
    for name in DATE_COLUMNS:
        frame[name] = pd.to_datetime(frame[name], format="%Y-%m-%d").astype("datetime64[ns]")
    return frame


# --- Summaries --------------------------------------------------------------------------------


def _require(frame: pd.DataFrame, columns: Iterable[str], what: str) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"{what}: missing columns {missing}")


def _labelled(series: pd.Series) -> pd.Series:
    """A text column with blanks named, so no summary row or column is left without a label."""
    text = series.astype("str")
    return text.where(text.str.strip() != "", NOT_STATED)


def _gw_table(frame: pd.DataFrame, rows: pd.Series, label: str) -> pd.DataFrame:
    """GW of ``mw`` per ``rows`` value and ``type_clean``, with a ``total`` column and a
    ``Total`` row, both summed before rounding.

    Type columns are sorted by name; row labels sorted with ``Total`` last. A blank type or row
    label is ``NOT_STATED``. An empty ``frame`` gives the header and the Total row of zeros.
    """
    mw = frame["mw"].fillna(0.0).astype("float64")
    pivot = pd.crosstab(_labelled(rows), _labelled(frame["type_clean"]), values=mw, aggfunc="sum")
    pivot = pivot.fillna(0.0).sort_index(axis=0).sort_index(axis=1)
    pivot["total"] = pivot.sum(axis=1)
    pivot.loc[TOTAL] = pivot.sum(axis=0)
    table = (pivot / MW_PER_GW).round(GW_DECIMALS).reset_index()
    table.columns = [label, *table.columns[1:]]
    table[label] = table[label].astype("str")
    return table


def active_by_region_and_type(requests: pd.DataFrame) -> pd.DataFrame:
    """GW of active requests per region and ``type_clean``: ``region``, one column per type,
    ``total``; a ``Total`` row last.

    A hybrid counts once, under its combined type, with ``mw`` (the sum of its parts).
    """
    _require(requests, ("q_status", "region", "type_clean", "mw"), "active_by_region_and_type")
    active = requests[requests["q_status"] == ACTIVE]
    return _gw_table(active, active["region"], "region")


def active_by_state_and_type(requests: pd.DataFrame) -> pd.DataFrame:
    """GW of active requests per state and ``type_clean``, same shape as the region table; a
    blank state is ``NOT_STATED``."""
    _require(requests, ("q_status", "state", "type_clean", "mw"), "active_by_state_and_type")
    active = requests[requests["q_status"] == ACTIVE]
    return _gw_table(active, active["state"], "state")


def ia_executed_not_operational(requests: pd.DataFrame) -> pd.DataFrame:
    """GW of active requests whose standardised phase is ``IA Executed`` (an interconnection
    agreement signed, the plant not yet operating), per region and type; the region table's
    shape. Suspended requests with an agreement are not counted, active means active."""
    _require(
        requests,
        ("q_status", "ia_phase_clean", "region", "type_clean", "mw"),
        "ia_executed_not_operational",
    )
    mask = (requests["q_status"] == ACTIVE) & (requests["ia_phase_clean"] == IA_EXECUTED)
    executed = requests[mask]
    return _gw_table(executed, executed["region"], "region")


LATER = "later"
EARLIER = "earlier"


def proposed_year_bin(prop_year: Any, through: int, years: int) -> str:
    """The row a proposed year falls in: ``earlier`` (before the data year), the year itself
    for ``through`` to ``through + years``, ``later`` beyond, ``NOT_STATED`` when blank."""
    if _is_blank(prop_year):
        return NOT_STATED
    year = int(prop_year)
    if year < through:
        return EARLIER
    if year > through + years:
        return LATER
    return str(year)


def active_by_proposed_year_and_type(
    requests: pd.DataFrame, through: int, years: int = 5
) -> pd.DataFrame:
    """GW of active requests per proposed online year and type: ``prop_year`` (text: ``earlier``,
    ``through`` to ``through + years`` one row each, ``later``, ``Not stated``), one column per
    type, ``total``; a ``Total`` row last. Every year row is present even when empty.

    ``through`` is the data year, so the first year row holds projects already late on their
    own proposed date.
    """
    _require(
        requests, ("q_status", "prop_year", "type_clean", "mw"), "active_by_proposed_year_and_type"
    )
    if years < 0:
        raise ValueError(f"active_by_proposed_year_and_type: years must be 0 or more, not {years}")
    active = requests[requests["q_status"] == ACTIVE]
    bins = active["prop_year"].map(lambda y: proposed_year_bin(y, through, years))
    table = _gw_table(active, bins, "prop_year")
    order = [EARLIER, *(str(y) for y in range(through, through + years + 1)), LATER, NOT_STATED]
    types = [c for c in table.columns if c not in ("prop_year", "total")]
    filled = table.set_index("prop_year").reindex([*order, TOTAL]).fillna(0.0)
    filled = filled[[*types, "total"]].reset_index()
    filled["prop_year"] = filled["prop_year"].astype("str")
    return filled


COMPLETION_STATUSES: tuple[str, ...] = ("operational", "withdrawn", "active", "suspended")


def completion_rates(
    requests: pd.DataFrame, first_year: int = 2000, last_year: int = 2020
) -> pd.DataFrame:
    """What became of the capacity requested in ``first_year`` to ``last_year``, per region.

    Columns: ``region``, ``requested_gw`` (every request in the window, whatever its status),
    ``operational_gw``, ``withdrawn_gw``, ``active_gw``, ``suspended_gw`` and the four
    ``*_share`` columns (each GW over ``requested_gw``, from unrounded sums, NaN when nothing
    was requested); a ``Total`` row last. The window is on ``q_year``; a blank year is outside
    it. The four shares sum to one less the share of rows with status ``unknown``.
    """
    _require(requests, ("q_status", "q_year", "region", "mw"), "completion_rates")
    if last_year < first_year:
        raise ValueError(f"completion_rates: {first_year}-{last_year} is not a window")
    window = requests[requests["q_year"].between(first_year, last_year).fillna(False).astype(bool)]
    mw = window["mw"].fillna(0.0).astype("float64")
    region = _labelled(window["region"])
    table = pd.DataFrame({"requested_mw": mw.groupby(region).sum()})
    for status in COMPLETION_STATUSES:
        table[f"{status}_mw"] = mw.where(window["q_status"] == status, 0.0).groupby(region).sum()
    table = table.sort_index()
    table.loc[TOTAL] = table.sum(axis=0)
    out = pd.DataFrame({"region": table.index.astype("str")}, index=table.index)
    requested = table["requested_mw"].where(table["requested_mw"] > 0)
    out["requested_gw"] = (table["requested_mw"] / MW_PER_GW).round(GW_DECIMALS)
    for status in COMPLETION_STATUSES:
        out[f"{status}_gw"] = (table[f"{status}_mw"] / MW_PER_GW).round(GW_DECIMALS)
    for status in COMPLETION_STATUSES:
        out[f"{status}_share"] = (table[f"{status}_mw"] / requested).round(SHARE_DECIMALS)
    return out.reset_index(drop=True)


def months_between(start: pd.Series, end: pd.Series) -> pd.Series:
    """Months from ``start`` to ``end`` as days over an average month; NaN when either is blank."""
    return (end - start).dt.days / DAYS_PER_MONTH


def median_months_to_operation(
    requests: pd.DataFrame, first_year: int = 2018, last_year: int = 2025
) -> pd.DataFrame:
    """Median months from queue entry to operation for the projects that came online each year.

    Columns: ``year``, ``n`` (projects in the median), ``overall``, then one column per region
    present in ``requests`` (its median, NaN when no project of that region came online that
    year); one row per year from ``first_year`` to ``last_year``. A project counts when its
    status is operational and both ``q_date`` and ``on_date`` are present and in order; the
    year is the ``on_date`` year. Medians are rounded to a tenth of a month.
    """
    _require(requests, ("q_status", "q_date", "on_date", "region"), "median_months_to_operation")
    if last_year < first_year:
        raise ValueError(f"median_months_to_operation: {first_year}-{last_year} is not a window")
    done = requests[
        (requests["q_status"] == OPERATIONAL)
        & requests["q_date"].notna()
        & requests["on_date"].notna()
    ]
    months = months_between(done["q_date"], done["on_date"])
    done = done.assign(months=months, year=done["on_date"].dt.year)[months >= 0]
    regions = sorted(set(_labelled(requests["region"])))
    years = list(range(first_year, last_year + 1))
    table = pd.DataFrame({"year": pd.array(years, dtype="Int64")})
    by_year = done.groupby("year")["months"]
    table["n"] = table["year"].map(by_year.size()).fillna(0).astype("int64")
    table["overall"] = table["year"].map(by_year.median()).astype("float64").round(MONTH_DECIMALS)
    by_region = done.groupby([_labelled(done["region"]), "year"])["months"].median()
    for region in regions:
        series = by_region.get(region, pd.Series(dtype="float64"))
        table[region] = table["year"].map(series).astype("float64").round(MONTH_DECIMALS)
    return table


# --- The processed folder ---------------------------------------------------------------------
#
# ``data/processed/queues/``: ``requests.csv`` (the tidy table without the free-text columns),
# the six summaries under ``summaries/`` and a ``source.json`` that says which workbook they came
# from. Written by ``process_workbook`` for the ``pull_queues`` command; read back by
# ``read_summaries`` for the site.

SUMMARIES_DIR = "summaries"
SOURCE_JSON = "source.json"
REQUESTS_CSV = "requests.csv"
# summary file name -> how it is computed from the tidy frame and the data year
SUMMARIES: dict[str, Callable[[pd.DataFrame, int], pd.DataFrame]] = {
    "active_by_region_and_type": lambda r, _: active_by_region_and_type(r),
    "active_by_proposed_year_and_type": active_by_proposed_year_and_type,
    "ia_executed_not_operational": lambda r, _: ia_executed_not_operational(r),
    "completion_rates": lambda r, _: completion_rates(r),
    "median_months_to_operation": lambda r, through: median_months_to_operation(
        r, last_year=through
    ),
    "active_by_state_and_type": lambda r, _: active_by_state_and_type(r),
}
# The summaries the site reads, with the name of their first (label) column.
SITE_SUMMARIES: dict[str, str] = {
    "active_by_region_and_type": "region",
    "ia_executed_not_operational": "region",
    "median_months_to_operation": "year",
    "active_by_proposed_year_and_type": "prop_year",
}


def _write_csv(table: pd.DataFrame, path: Path) -> None:
    # UTF-8, LF, no index and ISO dates on every platform, so a laptop and the CI runner commit
    # the same bytes.
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(path, index=False, encoding="utf-8", lineterminator="\n", date_format="%Y-%m-%d")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def summarise(requests: pd.DataFrame, through: int) -> dict[str, pd.DataFrame]:
    """Every summary in ``SUMMARIES``, computed once for the writer and the report alike."""
    return {name: compute(requests, through) for name, compute in SUMMARIES.items()}


def planned_outputs(out_dir: Path) -> list[Path]:
    """The files a run writes into ``out_dir``."""
    paths = [out_dir / REQUESTS_CSV]
    paths += [out_dir / SUMMARIES_DIR / f"{name}.csv" for name in SUMMARIES]
    return paths + [out_dir / SOURCE_JSON]


def read_source(out_dir: Path) -> dict[str, Any] | None:
    """The ``source.json`` an earlier run wrote into ``out_dir``, or ``None`` for an absent,
    unreadable or foreign file (one whose ``source`` is not this module's)."""
    try:
        source = json.loads((Path(out_dir) / SOURCE_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(source, dict) or source.get("source") != SOURCE:
        return None
    return source


def previous_outputs(out_dir: Path) -> list[str]:
    """The files (relative POSIX paths) an earlier run listed in ``source.json``; only these may
    be removed as stale."""
    source = read_source(out_dir)
    files = source.get("files") if source else None
    return [str(f) for f in files] if isinstance(files, list) else []


def source_record(workbook: Path, through: int) -> dict[str, Any]:
    """What ``source.json`` says about a workbook, before the rows and files are added.

    A function of the workbook alone: URL and fetch time come from the raw folder's manifest
    (``raw_provenance``), never from the clock, so the same workbook gives the same record.
    """
    workbook = Path(workbook)
    digest = sha256_file(workbook)
    return {
        "source": SOURCE,
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        **raw_provenance(workbook, digest),
        "file": workbook.name,
        "through": through,
        "sha256": digest,
        "bytes": workbook.stat().st_size,
    }


def write_processed(
    requests: pd.DataFrame,
    summaries: Mapping[str, pd.DataFrame],
    out_dir: Path,
    source: Mapping[str, Any],
) -> list[Path]:
    """Write ``requests.csv`` (without the free-text columns), the summaries and ``source.json``;
    drop CSVs an earlier run listed in ``source.json`` that this run did not write.

    ``source.json`` goes last, so a failure part-way leaves the previous one describing what is
    on disk; nothing in ``out_dir`` beyond the listed files is touched.
    """
    out_dir = Path(out_dir)
    stale_candidates = previous_outputs(out_dir)
    outputs: dict[str, pd.DataFrame] = {REQUESTS_CSV: requests[list(REQUESTS_COLUMNS)]}
    outputs |= {f"{SUMMARIES_DIR}/{name}.csv": table for name, table in summaries.items()}
    written: list[Path] = []
    for relative, table in outputs.items():
        path = out_dir / relative
        _write_csv(table, path)
        written.append(path)
    counts = requests["q_status"].value_counts()
    rows = {"requests": int(len(requests))}
    rows |= {status: int(counts.get(status, 0)) for status in STATUSES}
    source_path = out_dir / SOURCE_JSON
    _write_json(source_path, {**source, "rows": rows, "files": list(outputs)})
    written.append(source_path)

    root = out_dir.resolve()
    for relative in stale_candidates:
        if relative in outputs or relative == SOURCE_JSON:
            continue
        stale = (out_dir / relative).resolve()
        if root not in stale.parents or not stale.is_file():
            continue
        stale.unlink()
        log.info("removed stale %s", stale)
    return written


@dataclass(frozen=True)
class Processed:
    """What ``process_workbook`` made of one workbook: the tidy requests, the data year, the
    summaries, the ``source.json`` record and the paths written."""

    requests: pd.DataFrame
    through: int
    summaries: dict[str, pd.DataFrame]
    source: dict[str, Any]
    written: list[Path]


def process_workbook(workbook: Path, out_dir: Path) -> Processed:
    """Tidy the workbook's data sheet, compute the summaries and write the processed folder.

    Everything is computed before anything is written, so a workbook that fails validation
    (``ValueError``) leaves ``out_dir`` as it was. The data year is the latest queue-entry
    year; when the file name says ``thru<year>`` the two must agree.
    """
    workbook = Path(workbook)
    requests = read_requests(workbook)
    through = data_year(requests)
    named = through_from_name(workbook.name)
    if named is not None and named != through:
        raise ValueError(
            f"{workbook.name}: the file name says data through {named} but the latest "
            f"queue-entry year is {through}"
        )
    summaries = summarise(requests, through)
    source = source_record(workbook, through)
    written = write_processed(requests, summaries, out_dir, source)
    return Processed(
        requests=requests, through=through, summaries=summaries, source=source, written=written
    )


@dataclass(frozen=True)
class QueueSummaries:
    """The four summaries the grid page shows, with the data year and file name behind them."""

    through: int
    file: str | None
    active: pd.DataFrame  # active_by_region_and_type
    ia_executed: pd.DataFrame  # ia_executed_not_operational
    months: pd.DataFrame  # median_months_to_operation
    by_proposed_year: pd.DataFrame  # active_by_proposed_year_and_type


def _read_summary(path: Path, label: str) -> pd.DataFrame:
    """One summary CSV: a text label column first, numbers after; ``ValueError`` names the file.

    The GW tables must carry a ``total`` column and no blanks; the months table has ``n``
    and may leave a median blank (no project that year). Any non-numeric value is an error.
    """
    what = f"{path.parent.name}/{path.name}"
    try:
        header = list(pd.read_csv(path, nrows=0, encoding="utf-8").columns)
        if not header or header[0] != label:
            raise ValueError(f"first column {header[:1]} is not {[label]}")
        if label != "year" and "total" not in header:
            raise ValueError(f"columns {header} have no 'total'")
        frame = pd.read_csv(
            path,
            encoding="utf-8",
            dtype={label: "str"},
            keep_default_na=False,
            na_values={c: [""] for c in header[1:]},
        )
    except ValueError as exc:  # pandas' parser errors (an empty or ragged file) are ValueErrors
        raise ValueError(f"{what}: {exc}") from exc
    for column in header[1:]:
        try:
            numbers = pd.to_numeric(frame[column]).astype("float64")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{what}: {column!r} is not numeric ({exc})") from exc
        blank = numbers.isna()
        if label != "year" and blank.any():
            rows = frame.index[blank].tolist()[:5]
            raise ValueError(f"{what}: {column!r} is blank on rows {rows}")
        frame[column] = numbers
    frame[label] = frame[label].fillna("").astype("str")
    return frame


def read_summaries(directory: Path) -> QueueSummaries:
    """The site's view of a processed folder: data year, file and the four summaries it shows.

    ``FileNotFoundError`` when the folder, ``source.json`` or a summary is missing (a fresh
    clone before the first pull); ``ValueError`` naming the file when one is malformed: a
    ``source.json`` that is not this module's or has no whole-number ``through``, a summary
    with the wrong first column, no ``total``, a blank where none is allowed or a value that is
    not a number.
    """
    directory = Path(directory)
    paths = {name: directory / SUMMARIES_DIR / f"{name}.csv" for name in SITE_SUMMARIES}
    for path in (directory / SOURCE_JSON, *paths.values()):
        if not path.is_file():
            raise FileNotFoundError(f"{path} is missing")
    source = read_source(directory)
    if source is None:
        raise ValueError(f"{SOURCE_JSON}: not a readable {SOURCE} record")
    through = source.get("through")
    if isinstance(through, bool) or not isinstance(through, int):
        raise ValueError(f"{SOURCE_JSON}: through {through!r} is not a year")
    file = source.get("file")
    tables = {name: _read_summary(paths[name], label) for name, label in SITE_SUMMARIES.items()}
    return QueueSummaries(
        through=through,
        file=str(file) if file else None,
        active=tables["active_by_region_and_type"],
        ia_executed=tables["ia_executed_not_operational"],
        months=tables["median_months_to_operation"],
        by_proposed_year=tables["active_by_proposed_year_and_type"],
    )
