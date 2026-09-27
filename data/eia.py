"""EIA-860M client and tidy helpers: the US generator fleet, what is being built, what is closing.

What the source is
------------------
Form EIA-860M is the U.S. Energy Information Administration's "Preliminary Monthly Electric
Generator Inventory": one public Excel workbook a month, no API key, listed at
https://www.eia.gov/electricity/data/eia860m/ and named ``<month>_generator<year>.xlsx``. Each
sheet is one row per generating unit of 1 MW or more: ``Operating`` (the fleet as of that
month), ``Planned`` (units with a planned operation date and a status from "regulatory
approvals not initiated" to "construction complete"), ``Retired`` and ``Canceled or Postponed``,
plus the same three for Puerto Rico. Every row carries the owner, plant, state, balancing
authority, technology, fuel code, prime mover, nameplate and net summer capacity in MW, and the
year and month the unit came, comes or went off line.

Why it matters for the power stage
----------------------------------
The power primer's question is whether firm supply is being added as fast as data-centre demand
is being announced. This workbook is the supply side of that comparison for the United States:
the planned sheet, by year and technology, is the pipeline of megawatts that could serve new
load, and its status codes say how much of it is actually under construction (V, U and TS)
rather than awaiting approval. The operating sheet is the base those additions land on and the
retired sheet is what leaves it. The demand pipeline (announced campuses in GW) lives
elsewhere; this module only tidies the supply table and leaves the comparison to the stack.

Cadence and caveats
-------------------
* Monthly. EIA posts each month's inventory roughly a month after month end (the August 2026
  file appeared on 23 September 2026), so the newest file is always one to two months old.
* Preliminary. Figures are revised in later months and superseded by the annual EIA-860.
* Planned dates slip. A planned operation year is the developer's latest estimate, not a
  commitment; units move to later years or to the canceled sheet from one month to the next.
* Nameplate versus net summer. Nameplate is the generator's rated capacity; net summer is what
  it can deliver to the grid on a hot afternoon, and is blank for some units. Summaries carry
  both where the data allows and use nameplate where one number is needed.
* No fuel supply or interconnection status. A planned gas plant here says nothing about whether
  its turbine has been delivered, its pipeline built or its grid connection agreed; those are
  the grid stage's questions. Units under 1 MW are excluded by EIA, which understates
  distributed solar.

What the module does
--------------------
1. ``EIA860MClient`` finds the newest workbook on the index page, downloads it politely (one
   descriptive User-Agent, a pause between requests, retries on 429/5xx) into
   ``data/raw/EIA860M/<UTC date>/`` and writes a manifest with a sha256 per file, so any
   number downstream can be traced to the exact bytes EIA served on a given day.
2. Pure functions tidy the workbook: ``read_sheet`` finds the header row by its ``Entity ID``
   cell, drops the title block and the footnotes and returns snake_case columns with plain
   dtypes; ``tidy_all`` does that for every sheet; the ``*_by_*`` summaries aggregate MW by
   technology, year and state.

There is no model logic here. Network access goes through one injectable
``fetch(url, headers) -> bytes`` callable so the tests run offline.
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
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook

from data import EIA_RAW_DIR
from data.edgar import USER_AGENT_ENV, sha256_file, utc_now_iso, utc_today

log = logging.getLogger(__name__)

# --- EIA access policy -------------------------------------------------------------------

SOURCE = "EIA860M"
INDEX_URL = "https://www.eia.gov/electricity/data/eia860m/"
# EIA publishes no fair-access policy for these static files, but a client that names itself
# and paces its requests is easier for a webmaster to live with. The refresh needs only two
# requests (index page, workbook), so a full second between them costs nothing.
DEFAULT_USER_AGENT = (
    "ai-economics/0.1 open research model (https://github.com/Philbertcychan/ai-economics)"
)
MIN_REQUEST_INTERVAL_S = 1.0
MAX_RETRIES = 3  # extra attempts after the first, for 429 / 5xx / network errors
RETRY_BACKOFF_S = 1.0  # first retry waits this long; each further retry doubles it
REQUEST_TIMEOUT_S = 120.0  # the workbook is ~14 MB; a slow link needs more than SEC's 60 s


def user_agent() -> str:
    """The User-Agent sent to EIA.

    The address configured for SEC (``EDGAR_USER_AGENT``) is reused when present, so one
    environment variable identifies this client to every public source; otherwise the default
    names the project without nagging, because EIA does not ask for a contact address.
    """
    configured = os.environ.get(USER_AGENT_ENV, "").strip()
    return configured or DEFAULT_USER_AGENT


# `EIA860MClient.__init__` has a parameter called `user_agent`, which would shadow the function
# above inside that method; this alias keeps the call unambiguous.
_resolve_user_agent = user_agent


class EIAError(RuntimeError):
    """An EIA request or file failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


# --- Index page ----------------------------------------------------------------------------

MONTHS: dict[str, int] = {
    name: number
    for number, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
# The index links the current month from xls/ and older months from archive/xls/, so only the
# file name is matched; the path before it is kept as found.
_FILE_LINK_RE = re.compile(
    r"""href\s*=\s*["'](?P<href>[^"']*?(?P<month>[A-Za-z]+)_generator(?P<year>\d{4})\.xlsx)["']""",
    re.IGNORECASE,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FILE_NAME_RE = re.compile(r"^(?P<month>[A-Za-z]+)_generator(?P<year>\d{4})\.xlsx$", re.IGNORECASE)


def parse_index(html: str, base_url: str = INDEX_URL) -> list[tuple[int, int, str]]:
    """Every ``(year, month, absolute url)`` linked from the index page, newest first.

    HTML comments are stripped first: EIA pre-writes the rows for months not yet published
    inside ``<!-- -->`` (December is listed in September), and taking those literally would
    point at a file that does not exist yet. Links whose month is not an English month name
    are skipped. Duplicates collapse to the first occurrence.
    """
    found: dict[tuple[int, int], str] = {}
    for match in _FILE_LINK_RE.finditer(_HTML_COMMENT_RE.sub("", html)):
        month = MONTHS.get(match.group("month").lower())
        if month is None:
            continue
        key = (int(match.group("year")), month)
        found.setdefault(key, urllib.parse.urljoin(base_url, match.group("href")))
    return [(year, month, url) for (year, month), url in sorted(found.items(), reverse=True)]


def newest_file_url(html: str, base_url: str = INDEX_URL) -> str:
    """URL of the newest workbook on the index page; ``EIAError`` when it lists none."""
    files = parse_index(html, base_url)
    if not files:
        raise EIAError(f"no <month>_generator<year>.xlsx links found on {base_url}", url=base_url)
    return files[0][2]


def period_from_name(name: str) -> str | None:
    """``august_generator2026.xlsx`` -> ``"2026-08"``; ``None`` when the name is not EIA's."""
    match = _FILE_NAME_RE.match(Path(str(name)).name)
    if match is None:
        return None
    month = MONTHS.get(match.group("month").lower())
    return f"{int(match.group('year')):04d}-{month:02d}" if month else None


# --- HTTP ----------------------------------------------------------------------------------


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

    Retries 429, 5xx and network errors (including a body that stalls mid-download) up to
    ``MAX_RETRIES`` times with backoff; any other HTTP error becomes an ``EIAError``.
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
                    raise EIAError(f"HTTP {err.code} for {url}", status=err.code, url=url) from err
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
                    raise EIAError(f"network error for {url}: {reason}", url=url) from err
                delay = _retry_delay(None, attempt)
            attempt += 1
            log.warning(
                "EIA request failed (%s); retry %d/%d in %.1fs: %s",
                reason,
                attempt,
                MAX_RETRIES,
                delay,
                url,
            )
            sleep(delay)

    return fetch


# --- Client --------------------------------------------------------------------------------

_PROVENANCE_KEYS: tuple[str, ...] = ("source_url", "fetched_at")


def _manifest_entries(dated_dir: Path) -> dict[str, dict]:
    """``path -> entry`` from a dated directory's manifest; empty when missing or corrupt."""
    try:
        with open(dated_dir / "manifest.json", encoding="utf-8") as fh:
            files = json.load(fh).get("files", [])
        return {entry["path"]: entry for entry in files}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


class EIA860MClient:
    """Paced, cache-first client for the EIA-860M index page and workbooks.

    Everything downloaded lands in ``raw_dir / YYYY-MM-DD`` (the UTC date, so the directory
    agrees with the UTC timestamps written inside it). Within one dated directory a second
    ``download`` of the same file returns the cached copy without touching the network; a new
    day means a fresh pull, because EIA re-posts a month's file when it corrects it.
    """

    def __init__(
        self,
        raw_dir: Path = EIA_RAW_DIR,
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
        # absolute path -> {source_url, fetched_at} for every file this instance wrote
        self._sources: dict[Path, dict[str, Any]] = {}

    def headers(self) -> dict[str, str]:
        """Headers sent with every request. The workbook is already compressed (a zip), so
        no transfer encoding is requested and the bytes on disk are the bytes served."""
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

    def latest_file_url(self) -> str:
        """Fetch the index page and return the URL of the newest workbook it links."""
        body = self.get_bytes(INDEX_URL)
        return newest_file_url(body.decode("utf-8", errors="replace"), INDEX_URL)

    def cached_path(self, url: str) -> Path | None:
        """Newest local copy of the file ``url`` names, in any dated directory, else ``None``.

        Never creates directories and never touches the network.
        """
        name = Path(urllib.parse.urlsplit(url).path).name
        if not self.raw_dir.is_dir():
            return None
        dated_dirs = (p for p in self.raw_dir.iterdir() if p.is_dir())
        for dated in sorted(dated_dirs, reverse=True):  # ISO-dated names: newest first
            candidate = dated / name
            if candidate.is_file():
                return candidate
        return None

    def download(self, url: str) -> Path:
        """Store the workbook at ``url`` under today's directory and return its path.

        A copy already in today's directory is returned as is (cache-first within a day). When
        the bytes fetched are identical to the newest copy from an earlier day, nothing new
        has been published; the copy is still written so today's manifest documents what EIA
        served today, and the log says which day it matches.
        """
        name = Path(urllib.parse.urlsplit(url).path).name
        if not name.lower().endswith(".xlsx"):
            raise EIAError(f"expected a link to an .xlsx workbook, got {url}", url=url)
        target = self.cache_dir() / name
        if target.exists():
            log.info("using cached %s", target)
            return target
        earlier = self.cached_path(url)
        body = self.get_bytes(url)
        digest = hashlib.sha256(body).hexdigest()
        if earlier is not None and sha256_file(earlier) == digest:
            log.info("%s is byte-identical to %s", name, earlier)
        target.write_bytes(body)
        self._sources[target.resolve()] = {"source_url": url, "fetched_at": utc_now_iso()}
        return target

    def write_manifest(self) -> Path:
        """Write ``cache_dir/manifest.json`` listing every cached file with its sha256.

        Files fetched by this instance carry their source URL and fetch time; entries from a
        manifest written earlier the same day are carried forward when the bytes still match.
        """
        root = self.cache_dir()
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
        manifest = {"source": SOURCE, "pulled_at": utc_now_iso(), "files": files}
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        return manifest_path


# --- Tidy sheets ---------------------------------------------------------------------------

HEADER_CELL = "Entity ID"  # the first cell of the header row, below a title block of varying height

# source column -> tidy column, for the columns every sheet carries
COMMON_COLUMNS: dict[str, str] = {
    "Entity ID": "entity_id",
    "Entity Name": "entity_name",
    "Plant ID": "plant_id",
    "Plant Name": "plant_name",
    "Plant State": "state",
    "Balancing Authority Code": "balancing_authority",
    "Technology": "technology",
    "Energy Source Code": "energy_source",
    "Prime Mover Code": "prime_mover",
    "Nameplate Capacity (MW)": "nameplate_mw",
    "Net Summer Capacity (MW)": "net_summer_mw",
}
# The event each sheet dates, in the order they are looked for. The retired sheet carries
# both the operating and the retirement date, so retirement must be checked first.
DATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Retirement Year", "Retirement Month"),
    ("Planned Operation Year", "Planned Operation Month"),
    ("Operating Year", "Operating Month"),
)
OPTIONAL_COLUMNS: dict[str, str] = {"Generator ID": "generator_id", "Status": "status"}

TIDY_COLUMNS: tuple[str, ...] = (
    "entity_id",
    "entity_name",
    "plant_id",
    "plant_name",
    "generator_id",
    "state",
    "balancing_authority",
    "technology",
    "energy_source",
    "prime_mover",
    "nameplate_mw",
    "net_summer_mw",
    "status",
    "status_code",
    "year",
    "month",
)
_INT_COLUMNS: tuple[str, ...] = ("entity_id", "plant_id", "year", "month")
_FLOAT_COLUMNS: tuple[str, ...] = ("nameplate_mw", "net_summer_mw")
_TEXT_COLUMNS: tuple[str, ...] = tuple(
    c for c in TIDY_COLUMNS if c not in _INT_COLUMNS and c not in _FLOAT_COLUMNS
)
# Sheets `tidy_all` insists on; anything else in the workbook (Canceled or Postponed, the
# Puerto Rico sheets) is tidied too and keyed by its snake_case name.
CORE_SHEETS: tuple[str, ...] = ("operating", "planned", "retired")
# Planned-status codes that mean steel is in the ground: more than or up to 50 percent built,
# and built but not yet in commercial operation. The other codes (P, L, T, OT) are paperwork.
UNDER_CONSTRUCTION_CODES: frozenset[str] = frozenset({"V", "U", "TS"})
_STATUS_CODE_RE = re.compile(r"^\((?P<code>[A-Za-z]{1,2})\)")
# EIA reports capacity to 0.1 MW; summing floats would otherwise print 12345.700000000001.
MW_DECIMALS = 1
SHARE_DECIMALS = 3


def sheet_key(sheet: str) -> str:
    """``"Canceled or Postponed"`` -> ``"canceled_or_postponed"``, ``"Operating_PR"`` ->
    ``"operating_pr"``: the dict key in ``tidy_all`` and the processed CSV's file name."""
    return re.sub(r"[^a-z0-9]+", "_", str(sheet).strip().lower()).strip("_")


def status_code(status: Any) -> str:
    """``"(V) Under construction, more than 50 percent complete"`` -> ``"V"``; blank otherwise."""
    match = _STATUS_CODE_RE.match(str(status).strip()) if status is not None else None
    return match.group("code").upper() if match else ""


def _is_blank(value: Any) -> bool:
    """EIA writes an empty cell as a single space, so whitespace counts as blank."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return True
    return isinstance(value, str) and not value.strip()


def find_header_row(rows: Sequence[Sequence[Any]]) -> int:
    """Index of the first row whose first cell is ``Entity ID``; ``-1`` when there is none."""
    for index, row in enumerate(rows):
        first = row[0] if row else None
        if isinstance(first, str) and first.strip().casefold() == HEADER_CELL.casefold():
            return index
    return -1


def _data_rows(rows: Iterable[Sequence[Any]], width: int) -> list[list[Any]]:
    """Rows below the header with the footnotes dropped and every row padded to ``width``.

    A footnote is a row with content in its first cell only (the "NOTES:" block) or in no
    cell at all (the blank line above it). A data row with a blank Entity ID but a plant name
    and a capacity is kept: the retired sheet lists 1960s reactors that way.
    """
    kept = []
    for row in rows:
        cells = list(row)[:width] + [None] * max(0, width - len(row))
        if all(_is_blank(c) for c in cells[1:]):
            continue
        kept.append(cells)
    return kept


def _problem(sheet: str, message: str) -> ValueError:
    return ValueError(f"sheet {sheet!r}: {message}")


def _to_integer(frame: pd.DataFrame, column: str, sheet: str, *, source: str) -> pd.Series:
    """Nullable integers from a column of ints, floats or text; a non-integer is an error."""
    text = frame[column]
    numbers = pd.to_numeric(text.where(~text.map(_is_blank), None), errors="coerce")
    bad = text[numbers.isna() & ~text.map(_is_blank)]
    if not bad.empty:
        raise _problem(sheet, f"{source!r} has non-integer values {bad.head(5).tolist()}")
    fractional = numbers.dropna()
    fractional = fractional[fractional != fractional.round()]
    if not fractional.empty:
        raise _problem(sheet, f"{source!r} has non-integer values {fractional.head(5).tolist()}")
    return numbers.round().astype("Int64")


def _to_float(frame: pd.DataFrame, column: str, sheet: str, *, source: str) -> pd.Series:
    text = frame[column]
    numbers = pd.to_numeric(text.where(~text.map(_is_blank), None), errors="coerce")
    bad = text[numbers.isna() & ~text.map(_is_blank)]
    if not bad.empty:
        raise _problem(sheet, f"{source!r} has non-numeric values {bad.head(5).tolist()}")
    return numbers.astype("float64")


def _to_text(series: pd.Series) -> pd.Series:
    # Blank text stays "" rather than NaN so the CSV round-trips and the site can test for it.
    return series.map(lambda v: "" if _is_blank(v) else str(v).strip()).astype("str")


def tidy_rows(rows: Sequence[Sequence[Any]], sheet: str) -> pd.DataFrame:
    """Turn one sheet's grid of cells into the tidy frame ``read_sheet`` documents.

    Raises ``ValueError`` naming the sheet when the header row is missing, a required column
    is absent, a capacity is not a number or a year, month or ID is not a whole number.
    """
    header_index = find_header_row(rows)
    if header_index < 0:
        raise _problem(sheet, f"no header row starting with {HEADER_CELL!r}")
    header = [str(c).strip() if c is not None else "" for c in rows[header_index]]
    missing = [c for c in COMMON_COLUMNS if c not in header]
    if missing:
        raise _problem(sheet, f"missing columns {missing}")
    duplicates = sorted({c for c in header if c and header.count(c) > 1})
    if duplicates:
        raise _problem(sheet, f"duplicate columns {duplicates}")

    data = _data_rows(rows[header_index + 1 :], len(header))
    grid = pd.DataFrame(data, columns=header, dtype="object")
    year_source, month_source = next(
        ((y, m) for y, m in DATE_COLUMNS if y in header and m in header), (None, None)
    )
    # tidy name -> source header, for the columns this sheet has; error messages use the
    # source name because that is what the reader sees in Excel
    sources: dict[str, str] = {name: source for source, name in COMMON_COLUMNS.items()}
    sources |= {name: source for source, name in OPTIONAL_COLUMNS.items() if source in header}
    if year_source and month_source:
        sources |= {"year": year_source, "month": month_source}

    out = pd.DataFrame(index=grid.index)
    for name in TIDY_COLUMNS:
        out[name] = grid[sources[name]] if name in sources else None
    out["status_code"] = out["status"].map(status_code)
    for name in _INT_COLUMNS:
        out[name] = _to_integer(out, name, sheet, source=sources.get(name, name))
    for name in _FLOAT_COLUMNS:
        out[name] = _to_float(out, name, sheet, source=sources[name])
    for name in _TEXT_COLUMNS:
        out[name] = _to_text(out[name])

    out = out[list(TIDY_COLUMNS)]
    # Plant then unit is the natural key; the sort makes the CSV independent of EIA's row order.
    return out.sort_values(
        ["plant_id", "generator_id", "entity_id"], kind="stable", na_position="last"
    ).reset_index(drop=True)


def _sheet_grid(path: Path, sheet: str) -> list[tuple[Any, ...]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet not in workbook.sheetnames:
            raise _problem(sheet, f"not in {Path(path).name}; sheets: {workbook.sheetnames}")
        return list(workbook[sheet].iter_rows(values_only=True))
    finally:
        workbook.close()


def read_sheet(path: Path, sheet: str) -> pd.DataFrame:
    """One sheet of an EIA-860M workbook as a tidy frame with columns ``TIDY_COLUMNS``.

    ``entity_id``, ``plant_id``, ``year`` and ``month`` are nullable ``Int64``; the two
    capacities are ``float64`` (NaN when EIA left them blank); everything else is text with
    blanks kept as ``""``. ``year``/``month`` date the event the sheet is about (retirement on
    the retired sheet, planned operation on the planned sheet, first operation otherwise) and
    are blank on sheets without dates. ``status_code`` is the code in parentheses at the start
    of ``status`` (``V``, ``U``, ``TS`` ...), for filtering. Rows are sorted by plant and unit.
    """
    return tidy_rows(_sheet_grid(Path(path), sheet), sheet)


def tidy_all(path: Path) -> dict[str, pd.DataFrame]:
    """Every sheet of the workbook, tidied, keyed by ``sheet_key`` (``operating`` ...).

    The three core sheets must be present; any extra sheet (canceled or postponed, Puerto Rico)
    is included under its own key. ``ValueError`` names a missing core sheet.
    """
    path = Path(path)
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        frames = {
            sheet_key(name): tidy_rows(list(workbook[name].iter_rows(values_only=True)), name)
            for name in workbook.sheetnames
        }
    finally:
        workbook.close()
    missing = [s for s in CORE_SHEETS if s not in frames]
    if missing:
        raise ValueError(f"{path.name}: missing sheets {missing}; found {sorted(frames)}")
    return frames


# --- Summaries -----------------------------------------------------------------------------


def _require(frame: pd.DataFrame, columns: Iterable[str], what: str) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"{what}: missing columns {missing}")


def _mw(series: pd.Series) -> pd.Series:
    return series.round(MW_DECIMALS)


def capacity_by_fuel(operating: pd.DataFrame) -> pd.DataFrame:
    """Units, nameplate MW and net summer MW per technology, largest nameplate first.

    Every row of the operating sheet counts, including standby and out-of-service units (their
    ``status`` says so); this is EIA's inventory, not an availability figure.
    """
    _require(operating, ("technology", "nameplate_mw", "net_summer_mw"), "capacity_by_fuel")
    table = (
        operating.groupby("technology", sort=True)
        .agg(
            units=("technology", "size"),
            nameplate_mw=("nameplate_mw", "sum"),
            net_summer_mw=("net_summer_mw", "sum"),
        )
        .reset_index()
    )
    table["nameplate_mw"] = _mw(table["nameplate_mw"])
    table["net_summer_mw"] = _mw(table["net_summer_mw"])
    return table.sort_values(
        ["nameplate_mw", "technology"], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)


def planned_by_year_and_fuel(planned: pd.DataFrame) -> pd.DataFrame:
    """Planned MW per planned-operation year and technology, with how much is being built.

    ``under_construction_mw`` sums the units whose status code is in
    ``UNDER_CONSTRUCTION_CODES``; ``under_construction_share`` divides it by the nameplate
    total (NaN when that is zero). Sorted by year, then largest nameplate first; a blank year
    sorts last.
    """
    _require(
        planned, ("year", "technology", "nameplate_mw", "net_summer_mw", "status_code"), "planned"
    )
    frame = planned.assign(
        under_construction_mw=planned["nameplate_mw"].where(
            planned["status_code"].isin(UNDER_CONSTRUCTION_CODES), 0.0
        )
    )
    table = (
        frame.groupby(["year", "technology"], sort=True, dropna=False)
        .agg(
            units=("technology", "size"),
            nameplate_mw=("nameplate_mw", "sum"),
            net_summer_mw=("net_summer_mw", "sum"),
            under_construction_mw=("under_construction_mw", "sum"),
        )
        .reset_index()
    )
    for column in ("nameplate_mw", "net_summer_mw", "under_construction_mw"):
        table[column] = _mw(table[column])
    share = table["under_construction_mw"] / table["nameplate_mw"].where(table["nameplate_mw"] != 0)
    table["under_construction_share"] = share.round(SHARE_DECIMALS)
    table["year"] = table["year"].astype("Int64")
    return table.sort_values(
        ["year", "nameplate_mw", "technology"],
        ascending=[True, False, True],
        kind="stable",
        na_position="last",
    ).reset_index(drop=True)


def retired_by_year_and_fuel(retired: pd.DataFrame) -> pd.DataFrame:
    """Retired units, nameplate MW and net summer MW per retirement year and technology.

    Sorted by year, then largest nameplate first; a blank year sorts last.
    """
    _require(retired, ("year", "technology", "nameplate_mw", "net_summer_mw"), "retired")
    table = (
        retired.groupby(["year", "technology"], sort=True, dropna=False)
        .agg(
            units=("technology", "size"),
            nameplate_mw=("nameplate_mw", "sum"),
            net_summer_mw=("net_summer_mw", "sum"),
        )
        .reset_index()
    )
    table["nameplate_mw"] = _mw(table["nameplate_mw"])
    table["net_summer_mw"] = _mw(table["net_summer_mw"])
    table["year"] = table["year"].astype("Int64")
    return table.sort_values(
        ["year", "nameplate_mw", "technology"],
        ascending=[True, False, True],
        kind="stable",
        na_position="last",
    ).reset_index(drop=True)


def state_summary(operating: pd.DataFrame, planned: pd.DataFrame) -> pd.DataFrame:
    """Operating and planned nameplate MW per state, most planned MW first.

    Columns: ``state``, ``operating_units``, ``operating_mw``, ``planned_units``,
    ``planned_mw``, ``under_construction_mw``. A state with units on only one of the two
    sheets shows zero on the other. Ties in planned MW sort by state code.
    """
    _require(operating, ("state", "nameplate_mw"), "state_summary(operating)")
    _require(planned, ("state", "nameplate_mw", "status_code"), "state_summary(planned)")
    op = operating.groupby("state", sort=True).agg(
        operating_units=("state", "size"), operating_mw=("nameplate_mw", "sum")
    )
    under = planned["nameplate_mw"].where(
        planned["status_code"].isin(UNDER_CONSTRUCTION_CODES), 0.0
    )
    pl = (
        planned.assign(under_construction_mw=under)
        .groupby("state", sort=True)
        .agg(
            planned_units=("state", "size"),
            planned_mw=("nameplate_mw", "sum"),
            under_construction_mw=("under_construction_mw", "sum"),
        )
    )
    table = op.join(pl, how="outer").reset_index()
    for column in ("operating_units", "planned_units"):
        table[column] = table[column].fillna(0).astype("int64")
    for column in ("operating_mw", "planned_mw", "under_construction_mw"):
        table[column] = _mw(table[column].fillna(0.0).astype("float64"))
    return table.sort_values(
        ["planned_mw", "state"], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)
