"""Offline tests for the EDGAR client, tidy-facts helpers and the data-source stubs.

No network: every HTTP call goes through an injected ``fetch`` that serves the trimmed real
responses in ``tests/fixtures/edgar/`` (pulled 2026-09-12), or through a fake ``urlopen``
for the default fetch's gzip / retry paths.
"""

from __future__ import annotations

import datetime as dt
import email.message
import gzip
import hashlib
import http.client
import json
import logging
import urllib.error
from pathlib import Path

import pandas as pd
import pytest

from data import edgar
from data.edgar import (
    ARCHIVES_BASE,
    COMPANIES,
    DEFAULT_USER_AGENT,
    FACT_COLUMNS,
    FLOW_CONCEPTS,
    INSTANT_CONCEPTS,
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL_S,
    SERIES_COLUMNS,
    STANDARD_CONCEPTS,
    SUBMISSIONS_BASE,
    EdgarClient,
    EdgarError,
    Filing,
    accession_nodash,
    build_default_fetch,
    calendar_quarter_for,
    calendar_series,
    cik10,
    company_facts_url,
    facts_to_frame,
    filing_index_url,
    filing_url,
    full_text_search_url,
    iter_filings,
    load_processed_facts,
    merge_submissions,
    parse_frame,
    resolve_concept,
    submissions_url,
    user_agent,
)
from data.gpu_prices import PRICE_COLUMNS, CSVGPUPriceSource, GPUPriceSource, get_gpu_price_source
from data.transcripts import (
    SourceNotConfigured,
    TranscriptRef,
    TranscriptSource,
    UnconfiguredTranscriptSource,
    get_transcript_source,
)

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
TODAY = dt.date(2026, 9, 12)
CRWV_CIK = COMPANIES["CRWV"].cik
S1_ACCESSION = "0001193125-25-044231"
S1_DOC = "d899798ds1.htm"
S1_URL = f"{ARCHIVES_BASE}/1769628/000119312525044231/{S1_DOC}"
OLDER_PAGE_URL = f"{SUBMISSIONS_BASE}/CIK0001769628-submissions-001.json"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict:
    return json.loads(fixture_bytes(name).decode("utf-8"))


class FakeClock:
    """Monotonic clock that only advances when someone sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeFetch:
    """Serves fixture bytes by exact URL and records every request."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append((url, headers))
        if url not in self.routes:
            raise EdgarError(f"unexpected URL {url}", status=404, url=url)
        return self.routes[url]


def crwv_routes() -> dict[str, bytes]:
    return {
        submissions_url(CRWV_CIK): fixture_bytes("crwv_submissions.json"),
        OLDER_PAGE_URL: fixture_bytes("crwv_submissions_001.json"),
        company_facts_url(CRWV_CIK): fixture_bytes("crwv_companyfacts.json"),
        S1_URL: b"<html><body>S-1 body</body></html>",
    }


def make_client(
    tmp_path: Path, routes: dict[str, bytes] | None = None, **kwargs
) -> tuple[EdgarClient, FakeFetch, FakeClock]:
    fetch = FakeFetch(crwv_routes() if routes is None else routes)
    clock = FakeClock()
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("user_agent", "tests/1.0 tests@example.invalid")
    client = EdgarClient(tmp_path / "raw", fetch=fetch, sleep=clock.sleep, clock=clock, **kwargs)
    return client, fetch, clock


CAPEX_TAG = "PaymentsToAcquirePropertyPlantAndEquipment"


def fact_entry(
    start: str | None,
    end: str,
    val: float,
    frame: str | None = None,
    *,
    filed: str = "2026-01-31",
) -> dict:
    """One company-facts entry shaped like SEC's: no ``start`` on instants, no ``frame`` key
    on year-to-date or superseded facts. Values are made up (no real company)."""
    entry: dict = {"end": end, "val": val, "accn": f"acc-{filed}", "fy": int(end[:4])}
    entry |= {"fp": "FY", "form": "10-Q", "filed": filed}
    if start is not None:
        entry["start"] = start
    if frame is not None:
        entry["frame"] = frame
    return entry


def capex_facts(*entries: dict) -> pd.DataFrame:
    return facts_to_frame({"facts": {"us-gaap": {CAPEX_TAG: {"units": {"USD": list(entries)}}}}})


# --- User-Agent and headers ----------------------------------------------------------------


def test_user_agent_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGAR_USER_AGENT", "Ada Lovelace ada@example.com")
    assert user_agent() == "Ada Lovelace ada@example.com"


def test_user_agent_default_contains_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    assert user_agent() == DEFAULT_USER_AGENT
    assert "@" in DEFAULT_USER_AGENT
    assert "EDGAR_USER_AGENT" in DEFAULT_USER_AGENT


def test_default_user_agent_warns_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    monkeypatch.setattr(edgar, "_warned_default_user_agent", False)
    with caplog.at_level(logging.WARNING, logger="data.edgar"):
        user_agent()
        user_agent()
    warnings = [r for r in caplog.records if "EDGAR_USER_AGENT" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING


def test_sec_policy_constants() -> None:
    # Literal guards: the other tests use these names symbolically, so without this a
    # change to 0.05 s (20 requests/s, a breach of SEC's fair-access limit) would stay green.
    assert MIN_REQUEST_INTERVAL_S >= 0.1  # SEC: at most 10 requests per second
    assert MAX_RETRIES == 3


def test_client_sends_sec_headers(tmp_path: Path) -> None:
    client, fetch, _ = make_client(tmp_path, user_agent="Ada Lovelace ada@example.com")
    client.get_bytes(S1_URL)
    _, headers = fetch.calls[0]
    assert headers["User-Agent"] == "Ada Lovelace ada@example.com"
    assert headers["Accept-Encoding"] == "gzip, deflate"
    assert "Host" not in headers


# --- Rate limiter --------------------------------------------------------------------------


def test_rate_limiter_spaces_requests(tmp_path: Path) -> None:
    client, _, clock = make_client(tmp_path)
    for _ in range(3):
        client.get_bytes(S1_URL)
    assert sum(clock.sleeps) >= 2 * MIN_REQUEST_INTERVAL_S
    assert sum(clock.sleeps) >= 0.2  # absolute: three requests span at least two 0.1 s gaps
    assert all(s <= MIN_REQUEST_INTERVAL_S + 1e-9 for s in clock.sleeps)


def test_rate_limiter_does_not_sleep_when_enough_time_passed(tmp_path: Path) -> None:
    client, _, clock = make_client(tmp_path)
    client.get_bytes(S1_URL)
    clock.now += 1.0
    client.get_bytes(S1_URL)
    assert clock.sleeps == []


# --- Dated cache directory -----------------------------------------------------------------


def test_default_cache_date_is_the_utc_date(tmp_path: Path) -> None:
    new_york = dt.timezone(dt.timedelta(hours=-4))

    class EveningInNewYork(dt.datetime):
        """21:30 on 12 September in New York, which is already 13 September in UTC."""

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            local = cls(2026, 9, 12, 21, 30, tzinfo=new_york)
            return local.astimezone(tz) if tz is not None else local.replace(tzinfo=None)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(edgar.dt, "datetime", EveningInNewYork)
        client = EdgarClient(
            tmp_path / "raw", fetch=FakeFetch({}), user_agent="t t@example.invalid"
        )
    # the directory agrees with the UTC `pulled_at` / `fetched_at` stamps written inside it
    assert client.today == dt.date(2026, 9, 13)
    assert client.cache_dir("CRWV").name == "2026-09-13"


# --- Identifiers and URLs ------------------------------------------------------------------


def test_cik_padding_and_urls() -> None:
    assert cik10("1769628") == "0001769628"
    assert cik10(1769628) == "0001769628"
    assert cik10("0001769628") == "0001769628"
    assert accession_nodash(S1_ACCESSION) == "000119312525044231"
    assert submissions_url(1769628) == "https://data.sec.gov/submissions/CIK0001769628.json"
    assert company_facts_url("1769628") == (
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0001769628.json"
    )
    # Archives path uses the company CIK unpadded even though the accession is the agent's.
    assert filing_url(CRWV_CIK, S1_ACCESSION, S1_DOC) == S1_URL
    assert filing_index_url(CRWV_CIK, S1_ACCESSION) == (
        f"{ARCHIVES_BASE}/1769628/000119312525044231/{S1_ACCESSION}-index.htm"
    )
    assert all(len(info.cik) == 10 and info.cik.isdigit() for info in COMPANIES.values())
    assert set(COMPANIES) == {"CRWV", "NBIS", "NVDA", "MSFT", "GOOGL", "AMZN", "META"}


def test_unknown_ticker_raises_key_error(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    with pytest.raises(KeyError, match="CRWV"):
        client.filings("ZZZZ")


# --- Submissions and filings ---------------------------------------------------------------


def test_submissions_merges_recent_and_older_page(tmp_path: Path) -> None:
    client, fetch, _ = make_client(tmp_path)
    merged = client.submissions("CRWV")
    recent_fixture = fixture_json("crwv_submissions.json")["filings"]["recent"]
    older_fixture = fixture_json("crwv_submissions_001.json")
    expected = len(recent_fixture["accessionNumber"]) + len(older_fixture["accessionNumber"])
    recent = merged["filings"]["recent"]
    assert len(recent["accessionNumber"]) == expected
    assert all(len(column) == expected for column in recent.values())
    assert S1_ACCESSION in recent["accessionNumber"]  # only on the older page
    assert merged["name"] == "CoreWeave, Inc."
    assert [url for url, _ in fetch.calls] == [submissions_url(CRWV_CIK), OLDER_PAGE_URL]
    cache = client.cache_dir("CRWV")
    assert cache == tmp_path / "raw" / "CRWV" / "2026-09-12"
    assert (cache / "submissions.json").is_file()
    assert (cache / "submissions-001.json").read_bytes() == fixture_bytes(
        "crwv_submissions_001.json"
    )


def test_submissions_cache_holds_exactly_the_bytes_sec_served(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    client.submissions("CRWV")
    cache = client.cache_dir("CRWV")
    served = crwv_routes()
    # the merged view lives in memory only: a re-serialised file would match no URL's bytes
    assert sorted(p.name for p in cache.iterdir()) == ["submissions-001.json", "submissions.json"]
    assert (cache / "submissions.json").read_bytes() == served[submissions_url(CRWV_CIK)]
    manifest = json.loads(client.write_manifest("CRWV").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 2
    for entry in manifest["files"]:  # every hash can be re-checked against its source_url
        assert entry["sha256"] == hashlib.sha256(served[entry["source_url"]]).hexdigest()

    # a half-filled cache (main page kept, older page lost) fetches only what is missing
    (cache / "submissions-001.json").unlink()
    other, other_fetch, _ = make_client(tmp_path)
    assert S1_ACCESSION in other.submissions("CRWV")["filings"]["recent"]["accessionNumber"]
    assert [url for url, _ in other_fetch.calls] == [OLDER_PAGE_URL]


def test_submissions_does_not_cache_a_non_json_body(tmp_path: Path) -> None:
    routes = crwv_routes() | {OLDER_PAGE_URL: b"<html>an error page, not JSON</html>"}
    client, _, _ = make_client(tmp_path, routes)
    with pytest.raises(EdgarError, match="not JSON"):
        client.submissions("CRWV")
    assert not (client.cache_dir("CRWV") / "submissions-001.json").exists()


def test_submissions_without_older_pages(tmp_path: Path) -> None:
    routes = {submissions_url(COMPANIES["NBIS"].cik): fixture_bytes("nbis_submissions.json")}
    client, fetch, _ = make_client(tmp_path, routes)
    filings = client.filings("NBIS")
    assert len(fetch.calls) == 1
    assert {f.form for f in filings} <= {"20-F", "6-K"}
    assert any(f.form == "20-F" and f.report_date == "2025-12-31" for f in filings)


def test_merge_submissions_handles_missing_page_columns() -> None:
    main = {"filings": {"recent": {"accessionNumber": ["a"], "form": ["10-K"], "extra": [1]}}}
    page = {"accessionNumber": ["b"], "form": ["10-Q"]}
    merged = merge_submissions(main, [page])
    assert merged["filings"]["recent"]["accessionNumber"] == ["a", "b"]
    assert merged["filings"]["recent"]["extra"] == [1, None]
    assert main["filings"]["recent"]["accessionNumber"] == ["a"]  # input not mutated
    # a column that only an older page carries is back-filled for the rows already merged
    page_only = {"accessionNumber": ["c", "d"], "form": ["8-K", "4"], "onlyHere": ["x", "y"]}
    merged = merge_submissions(main, [page, page_only])
    assert merged["filings"]["recent"]["onlyHere"] == [None, None, "x", "y"]
    assert all(len(col) == 4 for col in merged["filings"]["recent"].values())


def test_filings_default_forms_sorted_and_filtered(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    filings = client.filings("CRWV")
    forms = {f.form for f in filings}
    assert forms <= set(COMPANIES["CRWV"].periodic_forms)
    assert "4" not in forms and "8-K" not in forms and "144" not in forms
    dates = [f.filing_date for f in filings]
    assert dates == sorted(dates, reverse=True)
    s1 = next(f for f in filings if f.accession == S1_ACCESSION)
    assert s1.form == "S-1"
    assert s1.primary_document == S1_DOC
    assert s1.report_date is None  # registration statements have no period
    assert s1.is_xbrl is False
    assert s1.url == S1_URL
    assert s1.cik == CRWV_CIK
    ten_k = next(f for f in filings if f.form == "10-K")
    assert ten_k.report_date == "2025-12-31" and ten_k.is_xbrl is True and ten_k.size > 0

    since = client.filings("CRWV", since="2026-01-01")
    assert since and all(f.filing_date >= "2026-01-01" for f in since)
    assert len(since) < len(filings)
    assert [f.form for f in client.filings("CRWV", forms=("10-K",))] == ["10-K"]
    assert [f.form for f in client.filings("CRWV", forms="424B4")] == ["424B4"]


def test_filing_to_row_has_csv_columns() -> None:
    filing = Filing(
        CRWV_CIK, S1_ACCESSION, "S-1", "2025-03-03", None, S1_DOC, "S-1", 17900177, False
    )
    row = filing.to_row()
    assert list(row) == [
        "accession",
        "form",
        "filing_date",
        "report_date",
        "primary_document",
        "url",
        "size",
        "is_xbrl",
    ]
    assert row["url"] == S1_URL


def test_cache_reuse_within_a_day(tmp_path: Path) -> None:
    client, fetch, _ = make_client(tmp_path)
    client.submissions("CRWV")
    client.company_facts("CRWV")
    calls_after_first = len(fetch.calls)
    assert client.submissions("CRWV")["cik"] == CRWV_CIK
    assert client.company_facts("CRWV")["entityName"] == "CoreWeave, Inc."
    assert len(fetch.calls) == calls_after_first
    # a second client instance on the same day also hits the cache, not the network
    other, other_fetch, _ = make_client(tmp_path)
    other.company_facts("CRWV")
    other.filings("CRWV")
    assert other_fetch.calls == []


# --- download_filing and manifest ----------------------------------------------------------


def test_download_filing_path_and_reuse_from_earlier_day(tmp_path: Path) -> None:
    client, fetch, _ = make_client(tmp_path)
    s1 = next(f for f in client.filings("CRWV") if f.accession == S1_ACCESSION)
    path = client.download_filing("CRWV", s1)
    assert path == tmp_path / "raw" / "CRWV" / "2026-09-12" / "filings" / S1_ACCESSION / S1_DOC
    assert path.read_bytes() == crwv_routes()[S1_URL]
    assert client.download_filing("CRWV", s1) == path
    assert sum(1 for url, _ in fetch.calls if url == S1_URL) == 1

    # next day: a client whose fetch cannot serve the document must reuse yesterday's copy
    later, later_fetch, _ = make_client(tmp_path, routes={}, today=TODAY + dt.timedelta(days=1))
    reused = later.download_filing("CRWV", s1)
    assert reused == tmp_path / "raw" / "CRWV" / "2026-09-13" / "filings" / S1_ACCESSION / S1_DOC
    assert reused.read_bytes() == path.read_bytes()
    assert later_fetch.calls == []


def test_cached_filing_path_finds_the_newest_copy_without_side_effects(tmp_path: Path) -> None:
    client, fetch, _ = make_client(tmp_path)
    s1 = next(f for f in client.filings("CRWV") if f.accession == S1_ACCESSION)
    calls_before = len(fetch.calls)
    assert client.cached_filing_path("CRWV", s1) is None
    assert client.cached_filing_path("NBIS", s1) is None
    assert not (tmp_path / "raw" / "NBIS").exists()  # asking must not create directories
    first = client.download_filing("CRWV", s1)
    assert client.cached_filing_path("CRWV", s1) == first  # today's directory counts
    assert client.cached_filing_path("crwv", s1) == first

    later, later_fetch, _ = make_client(tmp_path, routes={}, today=TODAY + dt.timedelta(days=1))
    assert later.cached_filing_path("CRWV", s1) == first  # yesterday's copy, nothing copied yet
    assert not (tmp_path / "raw" / "CRWV" / "2026-09-13").exists()
    second = later.download_filing("CRWV", s1)
    assert later.cached_filing_path("CRWV", s1) == second != first  # newest dated dir wins
    assert len(fetch.calls) == calls_before + 1 and later_fetch.calls == []


def test_copied_filing_keeps_its_original_fetch_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_path = f"filings/{S1_ACCESSION}/{S1_DOC}"

    def doc_entry(client: EdgarClient) -> dict:
        manifest = json.loads(client.write_manifest("CRWV").read_text(encoding="utf-8"))
        return next(e for e in manifest["files"] if e["path"] == doc_path)

    monkeypatch.setattr(edgar, "utc_now_iso", lambda: "2026-09-12T11:00:00Z")
    day1, _, _ = make_client(tmp_path)
    s1 = next(f for f in day1.filings("CRWV") if f.accession == S1_ACCESSION)
    day1.download_filing("CRWV", s1)
    assert doc_entry(day1)["fetched_at"] == "2026-09-12T11:00:00Z"

    # day 2 copies the document forward: it was not fetched today and must not say so
    monkeypatch.setattr(edgar, "utc_now_iso", lambda: "2026-09-13T11:00:00Z")
    day2, day2_fetch, _ = make_client(tmp_path, routes={}, today=TODAY + dt.timedelta(days=1))
    day2.download_filing("CRWV", s1)
    entry = doc_entry(day2)
    assert day2_fetch.calls == []
    assert entry["source_url"] == S1_URL
    assert entry["fetched_at"] == "2026-09-12T11:00:00Z"
    assert "copied_from" not in entry

    # day 3 finds no manifest entry vouching for the day-2 bytes (here: the manifest is gone),
    # so it names the file it copied instead of inventing a fetch time
    (day2.cache_dir("CRWV") / "manifest.json").unlink()
    monkeypatch.setattr(edgar, "utc_now_iso", lambda: "2026-09-14T11:00:00Z")
    day3, _, _ = make_client(tmp_path, routes={}, today=TODAY + dt.timedelta(days=2))
    day3.download_filing("CRWV", s1)
    entry = doc_entry(day3)
    assert entry["source_url"] == S1_URL
    assert "fetched_at" not in entry
    assert entry["copied_from"] == f"../2026-09-13/{doc_path}"
    assert (day3.cache_dir("CRWV") / entry["copied_from"]).resolve().is_file()
    # a later instance the same day carries that provenance forward unchanged
    again, _, _ = make_client(tmp_path, routes={}, today=TODAY + dt.timedelta(days=2))
    assert doc_entry(again) == entry


def test_copied_filing_ignores_a_manifest_entry_for_different_bytes(tmp_path: Path) -> None:
    day1, _, _ = make_client(tmp_path)
    s1 = next(f for f in day1.filings("CRWV") if f.accession == S1_ACCESSION)
    original = day1.download_filing("CRWV", s1)
    day1.write_manifest("CRWV")
    original.write_bytes(b"edited after the manifest was written")

    day2, _, _ = make_client(tmp_path, routes={}, today=TODAY + dt.timedelta(days=1))
    day2.download_filing("CRWV", s1)
    manifest = json.loads(day2.write_manifest("CRWV").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["files"] if e["path"].startswith("filings/"))
    assert "fetched_at" not in entry and entry["copied_from"].startswith("../2026-09-12/")


def test_write_manifest_lists_every_file_with_sha256(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    s1 = next(f for f in client.filings("CRWV") if f.accession == S1_ACCESSION)
    client.company_facts("CRWV")
    client.download_filing("CRWV", s1)
    manifest_path = client.write_manifest("CRWV")
    assert manifest_path == client.cache_dir("CRWV") / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ticker"] == "CRWV" and manifest["cik"] == CRWV_CIK
    assert manifest["pulled_at"].endswith("Z") and "T" in manifest["pulled_at"]

    root = client.cache_dir("CRWV")
    listed = {entry["path"]: entry for entry in manifest["files"]}
    on_disk = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert set(listed) == on_disk - {"manifest.json"}
    assert "manifest.json" not in listed
    for relative, entry in listed.items():
        data = (root / relative).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["bytes"] == len(data)
        assert "\\" not in relative
    assert listed["submissions.json"]["source_url"] == submissions_url(CRWV_CIK)
    assert listed["submissions-001.json"]["source_url"] == OLDER_PAGE_URL
    assert listed["companyfacts.json"]["source_url"] == company_facts_url(CRWV_CIK)
    doc_entry = listed[f"filings/{S1_ACCESSION}/{S1_DOC}"]
    assert doc_entry["source_url"] == S1_URL
    assert doc_entry["fetched_at"].endswith("Z")

    # a later instance the same day keeps the provenance of files it did not fetch itself
    again, _, _ = make_client(tmp_path)
    second = json.loads(again.write_manifest("CRWV").read_text(encoding="utf-8"))
    assert {e["path"]: e["source_url"] for e in second["files"]} == {
        p: e["source_url"] for p, e in listed.items()
    }


# --- facts_to_frame ------------------------------------------------------------------------


def test_facts_to_frame_columns_dtypes_and_candidates() -> None:
    frame = facts_to_frame(fixture_json("crwv_companyfacts.json"))
    assert list(frame.columns) == list(FACT_COLUMNS)
    assert not frame.empty
    assert frame["val"].dtype == "float64"
    assert str(frame["fy"].dtype) == "Int64"
    for column in FACT_COLUMNS:
        assert not frame[column].map(lambda v: isinstance(v, (dict, list))).any()
    by_concept = frame.groupby("concept")["tag"].first().to_dict()
    assert by_concept["revenue"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    # CostOfRevenue is absent for CoreWeave, so the second candidate is used
    assert by_concept["cost_of_revenue"] == "CostOfGoodsAndServicesSold"
    assert by_concept["interest_expense"] == "InterestExpenseDebt"
    assert by_concept["ppe_net"] == "PropertyPlantAndEquipmentNet"
    assert "shares_outstanding" not in by_concept  # not reported in the trimmed fixture
    assert (frame["taxonomy"] == "us-gaap").all()
    assert (frame["unit"] == "USD").all()
    # sorted by concept, end, filed
    key = list(zip(frame["concept"], frame["end"], frame["filed"], strict=True))
    assert key == sorted(key)
    # instants have no start; the DEF 14A net-income fact has fy = null
    ppe = frame[frame["concept"] == "ppe_net"]
    assert ppe["start"].isna().all()
    proxy = frame[(frame["concept"] == "net_income") & (frame["form"] == "DEF 14A")]
    assert len(proxy) == 1 and proxy["fy"].isna().all() and proxy["frame"].iloc[0] == "CY2025"


def test_facts_to_frame_prefers_usd_and_shares_for_foreign_filer() -> None:
    frame = facts_to_frame(fixture_json("nbis_companyfacts.json"))
    units = frame.groupby("concept")["unit"].unique().to_dict()
    assert list(units["revenue"]) == ["USD"]
    assert list(units["cash"]) == ["USD"]
    assert list(units["shares_outstanding"]) == ["shares"]
    assert set(frame["unit"]) == {"USD", "shares"}
    tags = frame.groupby("concept")["tag"].first().to_dict()
    assert tags["revenue"] == "Revenues"
    assert tags["net_income"] == "NetIncomeLoss"
    assert tags["shares_outstanding"] == "CommonStockSharesOutstanding"
    # Nebius left the first-listed D&A and interest tags behind (their USD data stops in 2014
    # and 2023); the later candidates carry the series through the latest 20-F
    assert tags["d_and_a"] == "DepreciationAndAmortization"
    assert tags["interest_expense"] == "InterestExpenseNonoperating"
    assert calendar_series(frame, "d_and_a", "A")["period"].iloc[-1] == "2025"
    assert calendar_series(frame, "interest_expense", "A")["period"].iloc[-1] == "2025"
    # the RUB rows for Revenues exist in the fixture but are not emitted
    raw = fixture_json("nbis_companyfacts.json")["facts"]["us-gaap"]["Revenues"]["units"]
    assert "RUB" in raw and len(frame[frame["concept"] == "revenue"]) == len(raw["USD"])


def test_resolve_concept_prefers_the_candidate_with_the_latest_data() -> None:
    def annual(year: int) -> dict:
        return {
            "start": f"{year}-01-01",
            "end": f"{year}-12-31",
            "val": year,
            "accn": f"acc-{year}",
            "fy": year,
            "fp": "FY",
            "form": "20-F",
            "filed": f"{year + 1}-04-30",
            "frame": f"CY{year}",
        }

    facts = {
        "facts": {
            "us-gaap": {
                # first in STANDARD_CONCEPTS["d_and_a"], but abandoned after 2014
                "DepreciationDepletionAndAmortization": {"units": {"USD": [annual(2014)]}},
                "DepreciationAndAmortization": {"units": {"USD": [annual(2014), annual(2025)]}},
                # only the unit that would be emitted counts: the 2026 rows are RUB, USD stops
                # in 2013
                "DepreciationAmortizationAndAccretionNet": {
                    "units": {"RUB": [annual(2026)], "USD": [annual(2013)]}
                },
            }
        }
    }
    found = resolve_concept(facts, STANDARD_CONCEPTS["d_and_a"])
    assert found is not None and found[:2] == ("us-gaap", "DepreciationAndAmortization")
    frame = facts_to_frame(facts)
    assert set(frame["tag"]) == {"DepreciationAndAmortization"}
    assert calendar_series(frame, "d_and_a", "A")["period"].tolist() == ["2014", "2025"]
    assert resolve_concept(facts, ("NotReported", "AlsoNotReported")) is None


def test_facts_to_frame_tie_keeps_declared_order_and_custom_taxonomy() -> None:
    entry = {
        "start": "2025-01-01",
        "end": "2025-12-31",
        "val": 10,
        "accn": "x",
        "fy": 2025,
        "fp": "FY",
        "form": "10-K",
        "filed": "2026-02-01",
        "frame": "CY2025",
    }
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [dict(entry, val=1)]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [dict(entry, val=2)]}
                },
                "CostOfRevenue": {"units": {}},  # present but empty -> skipped
                "CostOfGoodsAndServicesSold": {"units": {"EUR": [dict(entry, val=3)]}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2026-06-30",
                                "val": 5,
                                "accn": "y",
                                "fy": 2026,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2026-08-01",
                                "frame": "CY2026Q2I",
                            }
                        ]
                    }
                }
            },
        }
    }
    frame = facts_to_frame(facts)
    revenue = frame[frame["concept"] == "revenue"]
    # both revenue tags reach 2025-12-31, so the order in STANDARD_CONCEPTS decides
    assert revenue["tag"].iloc[0] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert revenue["val"].iloc[0] == 2.0
    cost = frame[frame["concept"] == "cost_of_revenue"]
    assert cost["tag"].iloc[0] == "CostOfGoodsAndServicesSold" and cost["unit"].iloc[0] == "EUR"
    shares = frame[frame["concept"] == "shares_outstanding"]
    assert shares["taxonomy"].iloc[0] == "dei" and shares["unit"].iloc[0] == "shares"
    assert shares["start"].isna().all()


def test_candidates_cover_the_elements_the_tracked_filers_actually_use() -> None:
    def instant(end: str, val: float) -> dict:
        return {
            "end": end,
            "val": val,
            "accn": "a",
            "fy": 2026,
            "fp": "Q2",
            "form": "10-Q",
            "filed": "2026-08-01",
            "frame": "CY2026Q2I",
        }

    def quarter(val: float) -> dict:
        return {
            "start": "2026-04-01",
            "end": "2026-06-30",
            "val": val,
            "accn": "a",
            "fy": 2026,
            "fp": "Q2",
            "form": "10-Q",
            "filed": "2026-08-01",
            "frame": "CY2026Q2",
        }

    lease_ppe = (
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAsset"
        "AfterAccumulatedDepreciationAndAmortization"
    )
    facts = {
        "facts": {
            "us-gaap": {
                "PaymentsToAcquireProductiveAssets": {"units": {"USD": [quarter(5.0)]}},
                lease_ppe: {"units": {"USD": [instant("2026-06-30", 9.0)]}},
                "Depreciation": {"units": {"USD": [quarter(2.0)]}},
            }
        }
    }
    frame = facts_to_frame(facts)
    chosen = dict(zip(frame["concept"], frame["tag"], strict=True))
    assert chosen == {
        "capex": "PaymentsToAcquireProductiveAssets",
        "ppe_net": lease_ppe,
        "d_and_a": "Depreciation",
    }

    # Depreciation alone is a narrower measure, so a combined D&A element with equally recent
    # data must keep winning the tie.
    facts["facts"]["us-gaap"]["DepreciationDepletionAndAmortization"] = {
        "units": {"USD": [quarter(3.0)]}
    }
    frame = facts_to_frame(facts)
    assert set(frame.loc[frame["concept"] == "d_and_a", "tag"]) == {
        "DepreciationDepletionAndAmortization"
    }


def test_facts_to_frame_empty_input() -> None:
    frame = facts_to_frame({})
    assert list(frame.columns) == list(FACT_COLUMNS) and frame.empty
    assert calendar_series(frame, "revenue").empty


def test_concept_sets_partition_standard_concepts() -> None:
    assert INSTANT_CONCEPTS == {
        "ppe_net",
        "long_term_debt",
        "cash",
        "shares_outstanding",
        "debt_principal",
        "deferred_revenue",
        "receivables",
    }
    assert INSTANT_CONCEPTS | FLOW_CONCEPTS == set(STANDARD_CONCEPTS)
    assert not INSTANT_CONCEPTS & FLOW_CONCEPTS


# --- calendar_series -----------------------------------------------------------------------


def test_parse_frame() -> None:
    assert parse_frame("CY2025Q2") == (2025, 2, False)
    assert parse_frame("CY2025") == (2025, None, False)
    assert parse_frame("CY2025Q2I") == (2025, 2, True)
    assert parse_frame(None) is None and parse_frame(float("nan")) is None
    assert parse_frame("CY2025Q5") is None


def test_calendar_series_quarterly_flow_with_derived_q4() -> None:
    facts = facts_to_frame(fixture_json("crwv_companyfacts.json"))
    series = calendar_series(facts, "revenue", "Q")
    assert list(series.columns) == list(SERIES_COLUMNS)
    assert series["val"].dtype == "float64" and series["derived"].dtype == "bool"
    points = series.set_index("period")
    assert list(points.index) == sorted(points.index)
    assert not any(len(p) == 4 for p in points.index)  # no annual periods in a Q series
    # framed quarterly value from the latest 10-Q, not the unframed original (1,212,788,000)
    assert points.loc["2025Q2", "val"] == 1_212_000_000
    assert points.loc["2025Q2", "end"] == "2025-06-30"
    assert bool(points.loc["2025Q2", "derived"]) is False
    # Q4 = FY - nine-month YTD, flagged derived, dated at the fiscal year end. For 2024 that
    # is the same number as FY - Q1 - Q2 - Q3.
    assert points.loc["2024Q4", "val"] == 1_915_000_000 - 1_167_996_000
    assert points.loc["2024Q4", "val"] == 1_915_000_000 - 188_684_000 - 395_371_000 - 583_941_000
    assert bool(points.loc["2024Q4", "derived"]) is True
    assert points.loc["2024Q4", "end"] == "2024-12-31"
    # For 2025 the two routes differ by rounding (Q1 and Q2 were restated to whole millions in
    # the 2026 10-Qs); the YTD route is preferred because it needs one input, not three.
    assert points.loc["2025Q4", "val"] == 5_131_000_000 - 3_559_096_000
    assert bool(points.loc["2025Q4", "derived"]) is True
    assert "2023Q4" not in points.index  # FY2023 exists but Q1..Q3 2023 do not
    assert "2026Q4" not in points.index
    assert points["derived"].sum() == 2


def test_calendar_series_annual_flow() -> None:
    facts = facts_to_frame(fixture_json("crwv_companyfacts.json"))
    series = calendar_series(facts, "revenue", "A")
    assert series["period"].tolist() == ["2023", "2024", "2025"]
    assert series["val"].tolist() == [229_000_000.0, 1_915_000_000.0, 5_131_000_000.0]
    assert series["end"].tolist() == ["2023-12-31", "2024-12-31", "2025-12-31"]
    assert not series["derived"].any()


def test_calendar_series_instants_use_trailing_i_frames() -> None:
    facts = facts_to_frame(fixture_json("crwv_companyfacts.json"))
    quarterly = calendar_series(facts, "ppe_net", "Q").set_index("period")
    assert quarterly.loc["2025Q2", "val"] == 16_631_510_000  # CY2025Q2I
    assert quarterly.loc["2025Q4", "val"] == 30_557_000_000  # CY2025Q4I, never derived
    assert quarterly.loc["2026Q2", "val"] == 46_736_000_000
    assert not quarterly["derived"].any()
    annual = calendar_series(facts, "ppe_net", "A").set_index("period")
    assert annual.loc["2025", "val"] == 30_557_000_000
    assert annual.loc["2024", "val"] == 11_915_000_000  # 10-K restated figure carries the frame
    assert list(annual.index) == ["2024", "2025"]
    # a flow concept never picks up instant frames and vice versa
    mixed = facts_to_frame(
        {
            "facts": {
                "us-gaap": {
                    CAPEX_TAG: {
                        "units": {"USD": [fact_entry(None, "2025-12-31", 1.0, "CY2025Q4I")]}
                    },
                    "CashAndCashEquivalentsAtCarryingValue": {
                        "units": {"USD": [fact_entry("2025-10-01", "2025-12-31", 2.0, "CY2025Q4")]}
                    },
                }
            }
        }
    )
    assert set(mixed["concept"]) == {"capex", "cash"}
    assert calendar_series(mixed, "capex", "Q").empty
    assert calendar_series(mixed, "cash", "Q").empty


# --- calendar_series: quarters recovered from year-to-date facts -----------------------------


def test_calendar_quarter_for_picks_the_nearest_calendar_quarter_end() -> None:
    assert calendar_quarter_for("2025-06-30") == (2025, 2)
    assert calendar_quarter_for("2025-12-31") == (2025, 4)
    # a fiscal year ending in late January: SEC frames these periods the same way
    assert calendar_quarter_for("2026-04-26") == (2026, 1)
    assert calendar_quarter_for("2026-01-25") == (2025, 4)
    assert calendar_quarter_for("2025-11-15") is None  # 46 days from both 09-30 and 12-31
    assert calendar_quarter_for("not a date") is None
    assert calendar_quarter_for(float("nan")) is None  # type: ignore[arg-type]


def test_calendar_series_fills_cash_flow_quarters_from_ytd_facts() -> None:
    # 10-Q cash-flow statements are year-to-date, so SEC frames only Q1 and the full year
    facts = facts_to_frame(fixture_json("crwv_companyfacts.json"))
    series = calendar_series(facts, "capex", "Q")
    assert list(series.columns) == list(SERIES_COLUMNS)
    assert series["period"].tolist() == [
        *(f"2024Q{q}" for q in (1, 2, 3, 4)),
        *(f"2025Q{q}" for q in (1, 2, 3, 4)),
        "2026Q1",
        "2026Q2",
    ]
    points = series.set_index("period")
    assert points.loc["2024Q1", "val"] == 1_741_935_000  # CY2024Q1, as framed
    assert points.loc["2024Q2", "val"] == 3_989_096_000 - 1_741_935_000
    assert points.loc["2024Q3", "val"] == 5_204_251_000 - 3_989_096_000
    assert points.loc["2024Q4", "val"] == 8_702_000_000 - 5_204_251_000
    # 2025: Q1 and the six-month figure were restated in the 2026 10-Qs; latest filed wins
    assert points.loc["2025Q1", "val"] == 1_407_000_000
    assert points.loc["2025Q2", "val"] == 3_860_000_000 - 1_407_000_000
    assert points.loc["2025Q3", "val"] == 6_249_239_000 - 3_860_000_000
    assert points.loc["2025Q4", "val"] == 10_309_000_000 - 6_249_239_000
    assert points.loc["2026Q2", "val"] == 14_117_000_000 - 7_695_000_000
    assert points["derived"].tolist() == [False, True, True, True] * 2 + [False, True]
    assert points.loc["2025Q2", "end"] == "2025-06-30"
    assert points.loc["2025Q4", "end"] == "2025-12-31"
    # the annual series is untouched by the quarterly fill
    annual = calendar_series(facts, "capex", "A")
    assert annual["period"].tolist() == ["2023", "2024", "2025"]
    assert not annual["derived"].any()


def test_calendar_series_ytd_uses_the_latest_filed_value_of_a_restated_period() -> None:
    facts = capex_facts(
        fact_entry("2025-01-01", "2025-06-30", 250.0, filed="2026-08-01"),  # restated a year on
        fact_entry("2025-01-01", "2025-06-30", 251.0, filed="2025-08-01"),  # as first filed
        fact_entry("2025-01-01", "2025-03-31", 100.0, "CY2025Q1", filed="2025-05-01"),
    )
    points = calendar_series(facts, "capex", "Q").set_index("period")
    assert points.loc["2025Q2", "val"] == 150.0
    assert bool(points.loc["2025Q2", "derived"]) is True


def test_calendar_series_ytd_does_not_guess_across_a_missing_quarter() -> None:
    facts = capex_facts(
        fact_entry("2025-01-01", "2025-03-31", 100.0, "CY2025Q1"),
        fact_entry("2025-01-01", "2025-06-30", 250.0),
        # no nine-month row: FY - 6M spans two quarters, so neither Q3 nor Q4 is filled
        fact_entry("2025-01-01", "2025-12-31", 1000.0, "CY2025"),
    )
    series = calendar_series(facts, "capex", "Q")
    assert series["period"].tolist() == ["2025Q1", "2025Q2"]
    assert series["val"].tolist() == [100.0, 150.0]
    # a full year standing alone is not a quarter either
    alone = capex_facts(fact_entry("2024-01-01", "2024-12-31", 900.0, "CY2024"))
    assert calendar_series(alone, "capex", "Q").empty


def test_calendar_series_ytd_maps_a_non_calendar_fiscal_quarter() -> None:
    facts = capex_facts(
        fact_entry("2026-01-26", "2026-04-26", 70.0),
        fact_entry("2026-01-26", "2026-07-26", 150.0),
    )
    series = calendar_series(facts, "capex", "Q")
    assert series["period"].tolist() == ["2026Q1", "2026Q2"]
    assert series["end"].tolist() == ["2026-04-26", "2026-07-26"]
    assert series["val"].tolist() == [70.0, 80.0]
    assert series["derived"].tolist() == [True, True]  # placed on the calendar here, not by SEC


def test_calendar_series_never_replaces_a_framed_quarter_with_a_derived_one() -> None:
    facts = capex_facts(
        fact_entry("2025-01-01", "2025-03-31", 100.0, "CY2025Q1"),
        fact_entry("2025-04-01", "2025-06-30", 140.0, "CY2025Q2"),  # SEC-framed 3-month fact
        fact_entry("2025-01-01", "2025-06-30", 250.0),  # the YTD difference would say 150
        fact_entry("2025-07-01", "2025-09-30", 160.0),  # reported on its own, but unframed
        fact_entry("2025-01-01", "2025-09-30", 420.0),  # the YTD difference would say 170
    )
    points = calendar_series(facts, "capex", "Q").set_index("period")
    assert points.loc["2025Q2", "val"] == 140.0
    assert bool(points.loc["2025Q2", "derived"]) is False
    # among unframed candidates, the quarter the filer reported beats the subtraction
    assert points.loc["2025Q3", "val"] == 160.0
    assert bool(points.loc["2025Q3", "derived"]) is True


def test_calendar_series_q4_falls_back_to_fy_minus_three_quarters() -> None:
    # a filer that tags only 3-month values leaves no nine-month figure to subtract
    facts = capex_facts(
        fact_entry("2025-01-01", "2025-03-31", 100.0, "CY2025Q1"),
        fact_entry("2025-04-01", "2025-06-30", 200.0, "CY2025Q2"),
        fact_entry("2025-07-01", "2025-09-30", 300.0, "CY2025Q3"),
        fact_entry("2025-01-01", "2025-12-31", 1000.0, "CY2025"),
    )
    points = calendar_series(facts, "capex", "Q").set_index("period")
    assert points.loc["2025Q4", "val"] == 400.0
    assert points.loc["2025Q4", "end"] == "2025-12-31"
    assert points["derived"].tolist() == [False, False, False, True]

    # A July-June fiscal year is not the sum of one calendar year's quarters, so the same
    # subtraction would mix two fiscal years; Q4 stays empty until it is reported.
    june_year_end = capex_facts(
        fact_entry("2024-07-01", "2025-06-30", 1000.0, "CY2025"),
        fact_entry("2025-01-01", "2025-03-31", 100.0, "CY2025Q1"),
        fact_entry("2025-04-01", "2025-06-30", 200.0, "CY2025Q2"),
        fact_entry("2025-07-01", "2025-09-30", 300.0, "CY2025Q3"),
    )
    assert calendar_series(june_year_end, "capex", "Q")["period"].tolist() == [
        "2025Q1",
        "2025Q2",
        "2025Q3",
    ]


def test_calendar_series_unknown_concept_and_bad_freq() -> None:
    facts = facts_to_frame(fixture_json("crwv_companyfacts.json"))
    empty = calendar_series(facts, "not_a_concept")
    assert list(empty.columns) == list(SERIES_COLUMNS) and empty.empty
    with pytest.raises(ValueError, match="freq"):
        calendar_series(facts, "revenue", "M")


def test_calendar_series_after_csv_round_trip(tmp_path: Path) -> None:
    facts = facts_to_frame(fixture_json("crwv_companyfacts.json"))
    target = tmp_path / "processed" / "CRWV" / "reported.csv"
    target.parent.mkdir(parents=True)
    facts.to_csv(target, index=False, encoding="utf-8", lineterminator="\n")
    loaded = load_processed_facts("crwv", tmp_path / "processed")
    assert loaded is not None
    assert list(loaded.columns) == list(FACT_COLUMNS)
    assert loaded["val"].dtype == "float64" and str(loaded["fy"].dtype) == "Int64"
    pd.testing.assert_frame_equal(loaded, facts)
    pd.testing.assert_frame_equal(
        calendar_series(loaded, "revenue"), calendar_series(facts, "revenue")
    )
    assert load_processed_facts("NBIS", tmp_path / "processed") is None


# --- Full-text search ----------------------------------------------------------------------


def test_full_text_search_url_and_parsing(tmp_path: Path) -> None:
    url = full_text_search_url(
        "GPU hours", forms=["S-1", "10-K"], ciks=["CRWV"], start="2025-01-01", end="2025-12-31"
    )
    assert url.startswith("https://efts.sec.gov/LATEST/search-index?q=GPU%20hours")
    assert "&forms=S-1,10-K" in url
    assert "&ciks=0001769628" in url
    assert "&dateRange=custom&startdt=2025-01-01&enddt=2025-12-31" in url
    assert "dateRange" not in full_text_search_url("GPU", ciks=[1769628])

    client, _, _ = make_client(tmp_path, routes={url: fixture_bytes("fts.json")})
    hits = client.full_text_search(
        "GPU hours", forms=["S-1", "10-K"], ciks=["CRWV"], start="2025-01-01", end="2025-12-31"
    )
    raw_hits = fixture_json("fts.json")["hits"]["hits"]
    assert len(hits) == len(raw_hits) > 0
    assert all("id" in hit and "adsh" in hit and "form" in hit for hit in hits)
    s1 = next(hit for hit in hits if hit["id"] == f"{S1_ACCESSION}:{S1_DOC}")
    assert s1["adsh"] == S1_ACCESSION and s1["file_date"] == "2025-03-03"
    assert s1["ciks"] == [CRWV_CIK] and s1["form"] == "S-1"


# --- Default fetch: gzip, retries, 404 -----------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes, content_encoding: str | None = None) -> None:
        self._body = body
        self.headers = {"Content-Encoding": content_encoding} if content_encoding else {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def http_error(url: str, code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, code, "error", headers, None)


def test_default_fetch_decodes_gzip() -> None:
    payload = json.dumps({"ok": True}).encode("utf-8")
    seen: list[object] = []

    def urlopen(request, timeout):  # noqa: ANN001 - mimics urllib.request.urlopen
        seen.append(request)
        return FakeResponse(gzip.compress(payload), "gzip")

    fetch = build_default_fetch(sleep=lambda s: None, urlopen=urlopen)
    body = fetch("https://data.sec.gov/x.json", {"User-Agent": "t t@example.invalid"})
    assert json.loads(body) == {"ok": True}
    assert seen[0].get_header("User-agent") == "t t@example.invalid"


def test_default_fetch_retries_on_503_then_succeeds() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def urlopen(request, timeout):  # noqa: ANN001
        attempts.append(1)
        if len(attempts) <= 2:
            raise http_error(request.full_url, 503)
        return FakeResponse(b"done")

    fetch = build_default_fetch(sleep=sleeps.append, urlopen=urlopen)
    assert fetch("https://data.sec.gov/x.json", {}) == b"done"
    assert len(attempts) == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0  # backoff grows


def test_default_fetch_gives_up_after_max_retries() -> None:
    sleeps: list[float] = []

    def urlopen(request, timeout):  # noqa: ANN001
        raise http_error(request.full_url, 429, retry_after="1")

    fetch = build_default_fetch(sleep=sleeps.append, urlopen=urlopen)
    with pytest.raises(EdgarError) as info:
        fetch("https://data.sec.gov/x.json", {})
    assert info.value.status == 429 and info.value.url == "https://data.sec.gov/x.json"
    assert sleeps == [1.0] * MAX_RETRIES  # Retry-After honoured


def test_default_fetch_never_retries_faster_than_the_rate_limit() -> None:
    # retries run inside one throttled request, so "Retry-After: 0" must not mean "at once"
    attempts: list[int] = []
    sleeps: list[float] = []

    def urlopen(request, timeout):  # noqa: ANN001
        attempts.append(1)
        if len(attempts) <= 2:
            raise http_error(request.full_url, 429, retry_after="0")
        return FakeResponse(b"done")

    fetch = build_default_fetch(sleep=sleeps.append, urlopen=urlopen)
    assert fetch("https://data.sec.gov/x.json", {}) == b"done"
    assert sleeps == [MIN_REQUEST_INTERVAL_S] * 2
    assert all(s >= 0.1 for s in sleeps)


class BrokenBodyResponse(FakeResponse):
    """Connects fine, then fails while the body is read (where urllib adds no URLError)."""

    def __init__(self, error: Exception) -> None:
        super().__init__(b"")
        self._error = error

    def read(self) -> bytes:
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("The read operation timed out"),
        ConnectionResetError(104, "Connection reset by peer"),
        http.client.IncompleteRead(b"partial body"),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_default_fetch_retries_failures_while_reading_the_body(error: Exception) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def urlopen(request, timeout):  # noqa: ANN001
        attempts.append(1)
        return BrokenBodyResponse(error) if len(attempts) <= 2 else FakeResponse(b"done")

    fetch = build_default_fetch(sleep=sleeps.append, urlopen=urlopen)
    assert fetch("https://data.sec.gov/x.json", {}) == b"done"
    assert len(attempts) == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0


def test_default_fetch_wraps_a_persistent_timeout_in_edgar_error() -> None:
    sleeps: list[float] = []

    def urlopen(request, timeout):  # noqa: ANN001
        raise TimeoutError("timed out")

    fetch = build_default_fetch(sleep=sleeps.append, urlopen=urlopen)
    with pytest.raises(EdgarError, match="network error") as info:
        fetch("https://data.sec.gov/x.json", {})
    assert info.value.status is None and info.value.url == "https://data.sec.gov/x.json"
    assert isinstance(info.value.__cause__, TimeoutError)
    assert len(sleeps) == MAX_RETRIES


def test_default_fetch_404_raises_without_retry() -> None:
    sleeps: list[float] = []

    def urlopen(request, timeout):  # noqa: ANN001
        raise http_error(request.full_url, 404)

    fetch = build_default_fetch(sleep=sleeps.append, urlopen=urlopen)
    with pytest.raises(EdgarError) as info:
        fetch("https://data.sec.gov/missing.json", {})
    assert info.value.status == 404
    assert info.value.url == "https://data.sec.gov/missing.json"
    assert sleeps == []


def test_get_json_rejects_non_json(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path, routes={"https://x.invalid/a": b"<html>oops</html>"})
    with pytest.raises(EdgarError, match="not JSON"):
        client.get_json("https://x.invalid/a")


# --- Stubs: transcripts and GPU prices -----------------------------------------------------


def test_transcript_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIPT_PROVIDER", raising=False)
    source = get_transcript_source()
    assert isinstance(source, TranscriptSource) and source.name == "none"
    assert isinstance(source, UnconfiguredTranscriptSource)
    with pytest.raises(SourceNotConfigured, match="TRANSCRIPT_PROVIDER"):
        source.list_transcripts("CRWV")
    ref = TranscriptRef("CRWV", "FY2026Q2", "2026-08-12", "Q2 2026 call", "https://example.invalid")
    with pytest.raises(SourceNotConfigured):
        source.get_transcript(ref)
    monkeypatch.setenv("TRANSCRIPT_PROVIDER", "acme")
    with pytest.raises(SourceNotConfigured, match="acme"):
        get_transcript_source()


def test_gpu_price_csv_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = CSVGPUPriceSource(tmp_path / "gpu_prices.csv")
    empty = missing.get_prices()
    assert list(empty.columns) == list(PRICE_COLUMNS) and empty.empty
    assert isinstance(missing, GPUPriceSource)

    csv_path = tmp_path / "gpu_prices.csv"
    csv_path.write_text(
        "date,gpu_model,provider,region,price_per_gpu_hour,term,source_url\n"
        "2026-09-01,H100,Beta,us-east,2.49,on-demand,https://example.invalid/b\n"
        "2026-08-01,h100,Alpha,us-west,2.99,on-demand,https://example.invalid/a\n"
        "2026-09-01,GB200,Alpha,us-west,7.50,on-demand,https://example.invalid/a\n",
        encoding="utf-8",
    )
    source = CSVGPUPriceSource(csv_path)
    prices = source.get_prices()
    assert list(prices.columns) == list(PRICE_COLUMNS)
    assert prices["date"].tolist() == ["2026-08-01", "2026-09-01", "2026-09-01"]
    assert prices["price_per_gpu_hour"].dtype == "float64"
    h100 = source.get_prices("H100")
    assert h100["provider"].tolist() == ["Alpha", "Beta"]  # case-insensitive model match
    assert source.get_prices("H100", start="2026-09-01")["provider"].tolist() == ["Beta"]
    assert source.get_prices(end="2026-08-31")["gpu_model"].tolist() == ["h100"]

    monkeypatch.delenv("GPU_PRICE_PROVIDER", raising=False)
    assert get_gpu_price_source().name == "csv"
    monkeypatch.setenv("GPU_PRICE_PROVIDER", "live")
    with pytest.raises(ValueError, match="GPU_PRICE_PROVIDER"):
        get_gpu_price_source()


def test_items_are_parsed_and_stay_out_of_filings_csv() -> None:
    block = {
        "accessionNumber": ["0001769628-26-000362", "0001769628-26-000366"],
        "form": ["8-K", "10-Q"],
        "filingDate": ["2026-08-11", "2026-08-12"],
        "primaryDocument": ["crwv-20260811.htm", "crwv-20260630.htm"],
        "items": ["2.02,9.01", ""],
    }
    earnings, quarterly = list(iter_filings("1769628", block))
    assert earnings.items == "2.02,9.01" and quarterly.items is None
    assert "items" not in earnings.to_row(), "filings.csv keeps its eight columns"


def test_filing_exhibits_and_download_document(tmp_path: Path) -> None:
    filing = Filing(
        cik="0001769628",
        accession="0001769628-26-000362",
        form="8-K",
        filing_date="2026-08-11",
        report_date="2026-08-11",
        primary_document="crwv-20260811.htm",
        description=None,
        size=None,
        is_xbrl=True,
        items="2.02,9.01",
    )
    index = {
        "directory": {
            "item": [
                {"name": "0001769628-26-000362-index.html"},
                {"name": "coreweave2q26earningspress.htm"},
                {"name": "crwv-20260811.htm"},
                {"name": "R1.htm"},
                {"name": "crwv-20260811_lab.xml"},
                {"name": "Show.js"},
            ]
        }
    }
    calls: list[str] = []

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        calls.append(url)
        if url.endswith("index.json"):
            return json.dumps(index).encode()
        return b"<html>press release</html>"

    client = EdgarClient(tmp_path, fetch=fetch, sleep=lambda s: None, today=dt.date(2026, 9, 21))
    assert client.filing_exhibits(filing) == ["coreweave2q26earningspress.htm"]
    assert calls == [
        "https://www.sec.gov/Archives/edgar/data/1769628/000176962826000362/index.json"
    ]

    path = client.download_document("CRWV", filing, "coreweave2q26earningspress.htm")
    assert path.read_bytes() == b"<html>press release</html>"
    assert path.parent.name == filing.accession and calls[-1].endswith(
        "/000176962826000362/coreweave2q26earningspress.htm"
    )
    before = len(calls)
    assert client.download_document("CRWV", filing, "coreweave2q26earningspress.htm") == path
    assert len(calls) == before, "second request is served from the cache"
