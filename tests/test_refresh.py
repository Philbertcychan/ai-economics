"""Tests for scripts/refresh.py: the daily refresh pipeline, fully offline.

A fake EDGAR client serves small hand-made responses from memory and fake model classes return
constant frames (placeholder numbers with no financial meaning). Every run writes into
``tmp_path``: nothing here touches the network or the repository's own data/, models/ or
site/data directories. The one integration test that builds the site reads the real templates
from ``site/templates`` and writes into a temporary output directory.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from companies.base import FILINGS_CSV_COLUMNS, BaseCompanyModel
from data import REPO_ROOT, SITE_DATA_DIR
from data.edgar import FACT_COLUMNS, CompanyInfo, Filing, facts_to_frame
from scripts import refresh
from scripts.build_site import BuildReport
from scripts.export_xlsx import export_workbook
from scripts.refresh import (
    FILINGS_COLUMNS,
    CompanyResult,
    RefreshConfig,
    RefreshPaths,
    RefreshResult,
    changed_line_items,
    read_filings_csv,
    render_diff,
    reported_payload,
    run,
    site_series,
    write_diff,
)

FIXED_NOW = datetime(2026, 9, 12, 11, 0, 0, tzinfo=UTC)
FIXED_NOW_ISO = "2026-09-12T11:00:00Z"


def fixed_now() -> datetime:
    return FIXED_NOW


# --------------------------------------------------------------------------------------------
# Fixture data: three tracked companies, two with a model class, one data-only
# --------------------------------------------------------------------------------------------

AAA = CompanyInfo("AAA", "0000000001", "Alpha Cloud", "neocloud", "domestic", ("10-K", "10-Q"))
BBB = CompanyInfo("BBB", "0000000002", "Beta Group N.V.", "neocloud", "foreign", ("20-F", "6-K"))
CCC = CompanyInfo("CCC", "0000000003", "Gamma Chips", "chip", "domestic", ("10-K", "10-Q"))
COMPANIES = {"AAA": AAA, "BBB": BBB, "CCC": CCC}


def make_filing(
    info: CompanyInfo,
    accession: str,
    form: str,
    filing_date: str,
    report_date: str | None,
    document: str,
    size: int | None = None,
    is_xbrl: bool = True,
) -> Filing:
    return Filing(
        cik=info.cik,
        accession=accession,
        form=form,
        filing_date=filing_date,
        report_date=report_date,
        primary_document=document,
        description=None,
        size=size,
        is_xbrl=is_xbrl,
    )


AAA_FILINGS = [
    make_filing(
        AAA, "0000000001-25-000002", "10-Q", "2025-08-14", "2025-06-30", "aaa-10q.htm", 1234
    ),
    make_filing(
        AAA, "0000000001-25-000001", "10-K", "2025-02-28", "2024-12-31", "aaa-10k.htm", 5678
    ),
    # Older than the default --since, so it is listed as new on the first run but not downloaded.
    make_filing(
        AAA, "0000000001-24-000001", "10-Q", "2024-11-05", None, "aaa-10q-old.htm", None, False
    ),
]
BBB_FILINGS = [
    make_filing(
        BBB, "0000000002-25-000001", "20-F", "2025-04-30", "2024-12-31", "bbb-20f.htm", 999
    ),
]


def fact(
    start: str | None,
    end: str,
    val: float,
    frame: str,
    *,
    form: str = "10-Q",
    fp: str = "Q1",
    fy: int = 2024,
    filed: str = "2024-05-01",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "end": end,
        "val": val,
        "accn": f"acc-{end}",
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
        "frame": frame,
    }
    if start is not None:
        entry["start"] = start
    return entry


# Quarterly filer: Q1-Q3 plus the full year 2024, so Q4 2024 must be derived (1000 - 600 = 400).
AAA_FACTS = {
    "cik": 1,
    "entityName": "Alpha Cloud",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        fact("2024-01-01", "2024-03-31", 100.0, "CY2024Q1"),
                        fact("2024-04-01", "2024-06-30", 200.0, "CY2024Q2", fp="Q2"),
                        fact("2024-07-01", "2024-09-30", 300.0, "CY2024Q3", fp="Q3"),
                        fact("2024-01-01", "2024-12-31", 1000.0, "CY2024", form="10-K", fp="FY"),
                        fact("2025-01-01", "2025-03-31", 500.0, "CY2025Q1", fy=2025),
                        fact("2025-04-01", "2025-06-30", 600.0, "CY2025Q2", fy=2025, fp="Q2"),
                    ]
                }
            },
            "CashAndCashEquivalentsAtCarryingValue": {
                "units": {
                    "USD": [
                        fact(None, "2024-12-31", 50.0, "CY2024Q4I", form="10-K", fp="FY"),
                        fact(None, "2025-06-30", 80.0, "CY2025Q2I", fy=2025, fp="Q2"),
                    ]
                }
            },
        }
    },
}
# 20-F filer: annual frames only, so the site series must fall back to freq "A". SEC still puts
# a quarter on instant frames (CY2024Q4I), so the balance sheet needs the same fallback.
BBB_FACTS = {
    "cik": 2,
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        fact("2023-01-01", "2023-12-31", 700.0, "CY2023", form="20-F", fy=2023),
                        fact("2024-01-01", "2024-12-31", 900.0, "CY2024", form="20-F", fy=2024),
                    ]
                }
            },
            "CashAndCashEquivalentsAtCarryingValue": {
                "units": {
                    "USD": [
                        fact(None, "2023-12-31", 30.0, "CY2023Q4I", form="20-F", fy=2023),
                        fact(None, "2024-12-31", 40.0, "CY2024Q4I", form="20-F", fy=2024),
                    ]
                }
            },
        }
    },
}
CCC_FACTS: dict[str, Any] = {"cik": 3, "facts": {}}  # tracked, nothing tagged yet


class FakeEdgarClient:
    """Duck-typed stand-in for ``data.edgar.EdgarClient`` serving canned responses.

    ``fail`` maps ``(method, ticker)`` to an exception to raise, so a test can break one step
    of one company and check that the rest of the run is unaffected.
    """

    def __init__(self, raw_dir: Path, *, fail: dict[tuple[str, str], Exception] | None = None):
        self.raw_dir = raw_dir
        self.fail = fail or {}
        self.calls: Counter[str] = Counter()
        self.filings_by_ticker = {"AAA": AAA_FILINGS, "BBB": BBB_FILINGS, "CCC": []}
        self.facts_by_ticker = {"AAA": AAA_FACTS, "BBB": BBB_FACTS, "CCC": CCC_FACTS}

    def _record(self, method: str, ticker: str) -> None:
        self.calls[method] += 1
        exc = self.fail.get((method, ticker))
        if exc is not None:
            raise exc

    def filings(self, ticker: str, forms: Any = None, since: str | None = None) -> list[Filing]:
        self._record("filings", ticker)
        # Oldest first on purpose: the pipeline, not the client, must sort newest first.
        return list(reversed(self.filings_by_ticker[ticker]))

    def company_facts(self, ticker: str) -> dict[str, Any]:
        self._record("company_facts", ticker)
        return self.facts_by_ticker[ticker]

    def _document_path(self, ticker: str, filing: Filing) -> Path:
        return self.raw_dir / ticker / "filings" / filing.accession / filing.primary_document

    def cached_filing_path(self, ticker: str, filing: Filing) -> Path | None:
        self._record("cached_filing_path", ticker)
        path = self._document_path(ticker, filing)
        return path if path.is_file() else None

    def download_filing(self, ticker: str, filing: Filing) -> Path:
        self._record("download_filing", ticker)
        path = self._document_path(ticker, filing)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<html></html>", encoding="utf-8")
        return path

    def write_manifest(self, ticker: str) -> Path:
        self._record("write_manifest", ticker)
        path = self.raw_dir / ticker / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path


class ExplodingClient:
    """Any method call is a test failure: used to prove --dry-run fetches nothing."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dry run must not call client.{name}")


# --------------------------------------------------------------------------------------------
# Fake models: constant frames, no logic
# --------------------------------------------------------------------------------------------


def constant_frames(revenue_2025: float) -> dict[str, pd.DataFrame]:
    """Toy frames in the shapes ``scripts/export_xlsx.py`` documents. Placeholders, no logic."""
    inputs = pd.DataFrame(
        {
            "name": ["chip_cost"],
            "value": [1.0],
            "unit": ["USD"],
            "source": ["test"],
            "note": ["placeholder"],
        }
    )
    drivers = pd.DataFrame([[1.0, 2.0]], index=["gpus"], columns=["2024A", "2025E"])
    outputs = pd.DataFrame(
        [[5.0, revenue_2025], [0.5, float("nan")]],
        index=["revenue", "margin"],
        columns=["2024A", "2025E"],
    )
    outputs.attrs = {"labels": {"revenue": "Revenue"}, "units": {"revenue": "USD m", "margin": "%"}}
    return {"inputs": inputs, "drivers": drivers, "outputs": outputs}


class FakeBuilt:
    """A model whose ``build()`` works: constant frames, exported with the real exporter."""

    ticker = "AAA"
    name = "Alpha Cloud"
    cik = "0000000001"
    layer = "neocloud"
    revenue_2025 = 10.0  # a subclass changes this to simulate a revised model

    def __init__(self, edgar: Any = None, **_: Any) -> None:
        self.edgar = edgar
        self.as_of: str | None = None
        self.built = False

    def load_data(self) -> None:
        self.as_of = "2025-08-14"

    def build(self) -> None:
        self.built = True

    def to_frames(self) -> dict[str, pd.DataFrame]:
        assert self.built, "to_frames() before build()"
        return constant_frames(self.revenue_2025)

    def to_xlsx(self, path: Path | str) -> Path:
        return export_workbook(self.to_frames(), path, title="Alpha Cloud (AAA) test model")


class FakeBuiltRevised(FakeBuilt):
    revenue_2025 = 11.0


class FakePending(FakeBuilt):
    """The state every real model starts in: ``build()`` is still a TODO."""

    ticker = "BBB"
    name = "Beta Group N.V."
    cik = "0000000002"

    def build(self) -> None:
        raise NotImplementedError("Beta drivers not written yet - TODO.md")


class FakeBroken(FakeBuilt):
    def to_frames(self) -> dict[str, pd.DataFrame]:
        raise ValueError("inputs frame is malformed")


class RealPendingAAA(BaseCompanyModel):
    """The real base class with no ``build()``: what every company model is until it is written.

    AAA is not in ``data.edgar.COMPANIES``, so all four identity attributes are set here.
    """

    ticker = "AAA"
    name = "Alpha Cloud"
    cik = "0000000001"
    layer = "neocloud"


REGISTRY: dict[str, type] = {"AAA": FakeBuilt, "BBB": FakePending}


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def make_paths(tmp_path: Path) -> RefreshPaths:
    site = tmp_path / "site"
    return RefreshPaths(
        processed_dir=tmp_path / "processed",
        models_dir=tmp_path / "models",
        site_dir=site,
        site_build_dir=site / "build",
        diff_path=tmp_path / "last_refresh_diff.md",
        calls_md=tmp_path / "calls.md",
    )


def do_run(
    tmp_path: Path,
    *,
    config: RefreshConfig | None = None,
    client: Any = None,
    registry: dict[str, type] | None = None,
) -> tuple[RefreshResult, RefreshPaths, Any]:
    """Run the pipeline into ``tmp_path`` with the fakes; site build off unless asked."""
    paths = make_paths(tmp_path)
    client = client or FakeEdgarClient(tmp_path / "raw")
    result = run(
        config or RefreshConfig(build_site=False),
        client=client,
        registry=REGISTRY if registry is None else registry,
        companies=COMPANIES,
        paths=paths,
        now=fixed_now,
    )
    return result, paths, client


def by_ticker(result: RefreshResult) -> dict[str, CompanyResult]:
    return {company.ticker: company for company in result.companies}


def snapshot(root: Path, *, skip: Path | None = None) -> dict[str, bytes]:
    """Every file under ``root`` as ``{relative posix path: bytes}``."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != skip
    }


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------
# First run: processed CSVs, downloads, model export, site JSON, diff
# --------------------------------------------------------------------------------------------


def test_first_run_writes_processed_csvs(tmp_path: Path) -> None:
    result, paths, _ = do_run(tmp_path)
    assert result.errors == []

    filings_csv = paths.processed_dir / "AAA" / "filings.csv"
    raw = filings_csv.read_bytes()
    assert b"\r\n" not in raw, "filings.csv must use LF line endings on every platform"
    table = pd.read_csv(filings_csv, dtype=str, keep_default_na=False)
    assert list(table.columns) == list(FILINGS_COLUMNS)
    # Newest first, regardless of the order the client returned.
    assert table["accession"].tolist() == [f.accession for f in AAA_FILINGS]
    assert table["url"].iloc[0] == AAA_FILINGS[0].url
    assert table["size"].tolist() == ["1234", "5678", ""], "missing size stays blank, not 'nan'"
    assert table["report_date"].tolist()[-1] == ""
    assert table["is_xbrl"].tolist() == ["True", "True", "False"]

    reported_csv = paths.processed_dir / "AAA" / "reported.csv"
    assert b"\r\n" not in reported_csv.read_bytes()
    reported = pd.read_csv(reported_csv)
    assert list(reported.columns) == list(FACT_COLUMNS)
    assert len(reported) == by_ticker(result)["AAA"].facts_rows == 8

    # A company with nothing tagged still gets an (empty) reported.csv with the header.
    empty = pd.read_csv(paths.processed_dir / "CCC" / "reported.csv")
    assert list(empty.columns) == list(FACT_COLUMNS) and empty.empty


def test_first_run_lists_new_filings_and_downloads_since(tmp_path: Path) -> None:
    result, _, client = do_run(tmp_path)
    aaa = by_ticker(result)["AAA"]
    assert [f.accession for f in aaa.new_filings] == [f.accession for f in AAA_FILINGS]
    assert aaa.total_filings == 3
    # Default --since 2025-01-01 excludes the 2024 filing from the download.
    assert [p.name for p in aaa.downloaded] == ["aaa-10q.htm", "aaa-10k.htm"]
    assert all(p.is_file() for p in aaa.downloaded)
    assert client.calls["download_filing"] == 3  # 2 for AAA + 1 for BBB, none for CCC
    assert client.calls["write_manifest"] == 3
    assert aaa.as_of == "2025-08-14"
    assert aaa.latest_filing == {"form": "10-Q", "date": "2025-08-14", "url": AAA_FILINGS[0].url}


def test_no_download_skips_documents(tmp_path: Path) -> None:
    result, _, client = do_run(tmp_path, config=RefreshConfig(download=False, build_site=False))
    assert client.calls["download_filing"] == 0
    assert all(company.downloaded == [] for company in result.companies)
    assert len(by_ticker(result)["AAA"].new_filings) == 3  # still reported as new


def test_model_statuses_and_export(tmp_path: Path) -> None:
    result, paths, _ = do_run(tmp_path)
    statuses = {t: c.model_status for t, c in by_ticker(result).items()}
    assert statuses == {"AAA": "changed", "BBB": "pending", "CCC": "no-model"}

    aaa = by_ticker(result)["AAA"]
    assert aaa.changed_items == ["revenue", "margin"], "first export lists every line item"
    assert (paths.models_dir / "AAA.xlsx").is_file()
    assert not (paths.models_dir / "BBB.xlsx").exists()

    manifest = read_json(paths.models_dir / "manifest.json")
    assert set(manifest) == {"AAA"}
    assert set(manifest["AAA"]) == {"fingerprint", "exported_at", "as_of"}
    assert manifest["AAA"]["exported_at"] == FIXED_NOW_ISO
    assert manifest["AAA"]["as_of"] == "2025-08-14"

    outputs_csv = paths.processed_dir / "AAA" / "outputs.csv"
    assert outputs_csv.read_text(encoding="utf-8").splitlines()[0] == "item,2024A,2025E"
    assert b"\r\n" not in outputs_csv.read_bytes()
    assert not (paths.processed_dir / "BBB" / "outputs.csv").exists()


def test_real_pending_model_reads_back_what_refresh_wrote(tmp_path: Path) -> None:
    """The seam behind ``uv run scripts/export_xlsx.py <TICKER>`` after a refresh: the pipeline
    writes processed/<TICKER>/, the real base class loads it offline. Fakes cannot cover it."""
    result, paths, _ = do_run(tmp_path, registry={"AAA": RealPendingAAA, "BBB": FakePending})
    assert result.errors == []
    assert by_ticker(result)["AAA"].model_status == "pending"
    assert not (paths.models_dir / "AAA.xlsx").exists()

    offline = RealPendingAAA(processed_dir=paths.processed_dir)  # no client: CSVs only
    offline.load_data()
    assert offline.filings == AAA_FILINGS, "every field survives filings.csv, blanks as None"
    assert offline.filings[-1].size is None and offline.filings[-1].is_xbrl is False
    assert offline.as_of == "2025-08-14"
    assert offline.reported is not None
    pd.testing.assert_frame_equal(offline.reported, facts_to_frame(AAA_FACTS))


def test_filings_csv_columns_agree_everywhere() -> None:
    """The header is spelled in three places (writer here, offline reader in companies.base,
    ``Filing.to_row``); a rename or reorder in one of them would otherwise go unnoticed."""
    assert FILINGS_COLUMNS == FILINGS_CSV_COLUMNS == tuple(AAA_FILINGS[0].to_row())


def test_site_json_shapes(tmp_path: Path) -> None:
    result, paths, _ = do_run(tmp_path)

    aaa = read_json(paths.site_data_dir / "AAA.json")
    assert {k: aaa[k] for k in ("ticker", "name", "layer", "cik", "as_of")} == {
        "ticker": "AAA",
        "name": "Alpha Cloud",
        "layer": "neocloud",
        "cik": "0000000001",
        "as_of": "2025-08-14",
    }
    revenue = aaa["reported"]["revenue"]
    assert revenue["unit"] == "USD" and revenue["freq"] == "Q"
    assert revenue["points"] == [
        ["2024Q1", 100.0],
        ["2024Q2", 200.0],
        ["2024Q3", 300.0],
        ["2024Q4", 400.0],  # derived: FY 1000 - (100 + 200 + 300)
        ["2025Q1", 500.0],
        ["2025Q2", 600.0],
    ]
    assert aaa["reported"]["cash"]["freq"] == "Q", "a 10-Q balance makes the series quarterly"
    assert aaa["reported"]["cash"]["points"] == [["2024Q4", 50.0], ["2025Q2", 80.0]]
    assert list(aaa["reported"]) == ["revenue", "cash"], "STANDARD_CONCEPTS order, data only"
    assert aaa["outputs"] == {
        "revenue": {
            "label": "Revenue",
            "unit": "USD m",
            "points": [["2024A", 5.0], ["2025E", 10.0]],
        },
        "margin": {"label": "margin", "unit": "%", "points": [["2024A", 0.5], ["2025E", None]]},
    }
    assert aaa["inputs"] == [
        {"name": "chip_cost", "value": 1.0, "unit": "USD", "source": "test", "note": "placeholder"}
    ]

    bbb = read_json(paths.site_data_dir / "BBB.json")
    assert bbb["reported"]["revenue"] == {
        "unit": "USD",
        "freq": "A",
        "tag": "us-gaap:Revenues",
        "points": [["2023", 700.0], ["2024", 900.0]],
    }
    # Year-end balances only: annual like the revenue next to it, not "2023Q4", "2024Q4".
    assert bbb["reported"]["cash"] == {
        "unit": "USD",
        "freq": "A",
        "tag": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "points": [["2023", 30.0], ["2024", 40.0]],
    }
    assert bbb["outputs"] is None and bbb["inputs"] is None

    ccc = read_json(paths.site_data_dir / "CCC.json")
    assert ccc["reported"] == {} and ccc["outputs"] is None and ccc["as_of"] is None

    companies = read_json(paths.site_data_dir / "companies.json")
    assert companies["as_of"] == FIXED_NOW_ISO
    assert [c["ticker"] for c in companies["companies"]] == ["AAA", "BBB", "CCC"]
    assert [c["model_status"] for c in companies["companies"]] == ["built", "pending", "no-model"]
    assert [c["has_data"] for c in companies["companies"]] == [True, True, False]
    assert companies["companies"][0]["latest_filing"] == {
        "form": "10-Q",
        "date": "2025-08-14",
        "url": AAA_FILINGS[0].url,
    }
    assert companies["companies"][2]["latest_filing"] is None
    assert set(companies["companies"][0]) == {
        "ticker",
        "name",
        "layer",
        "cik",
        "model_status",
        "latest_filing",
        "has_data",
    }
    for name in ("AAA.json", "companies.json"):
        assert b"\r\n" not in (paths.site_data_dir / name).read_bytes()


def test_diff_file_contents(tmp_path: Path) -> None:
    result, paths, _ = do_run(tmp_path)
    text = paths.diff_path.read_text(encoding="utf-8")
    assert b"\r\n" not in paths.diff_path.read_bytes()
    assert text.startswith(f"# Refresh {FIXED_NOW_ISO}\n")
    assert "3 companies: 4 new filings, 3 documents downloaded" in text
    assert "models: 1 no-model, 1 pending, 1 changed, 0 errors" in text
    assert "## AAA" in text and "## BBB" in text and "## CCC" in text
    assert f"- 10-Q 2025-08-14 [{AAA_FILINGS[0].accession}]({AAA_FILINGS[0].url})" in text
    assert "- Model: changed - changed line items: revenue, margin" in text
    assert "- Model: pending" in text and "- Model: no-model" in text
    assert "- Downloaded: 2 documents" in text
    assert text.rstrip().endswith("## Errors\n\nNone.")
    assert result.site is None and "## Site" not in text


# --------------------------------------------------------------------------------------------
# Second run: idempotence and change detection
# --------------------------------------------------------------------------------------------


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    _, paths, _ = do_run(tmp_path)
    before = snapshot(tmp_path)

    result, _, client = do_run(tmp_path)
    after = snapshot(tmp_path)

    assert result.errors == []
    assert all(company.new_filings == [] for company in result.companies)
    assert by_ticker(result)["AAA"].model_status == "unchanged"
    assert by_ticker(result)["AAA"].changed_items == []
    assert client.calls["download_filing"] == 0, "nothing new, nothing to download"

    diff_rel = paths.diff_path.relative_to(tmp_path).as_posix()
    changed = sorted(name for name in before | after if before.get(name) != after.get(name))
    # With an injected clock even companies.json is byte-identical; only the diff differs.
    assert changed == [diff_rel]
    assert "no new filings" in paths.diff_path.read_text(encoding="utf-8")


def test_revised_model_reports_changed_line_items(tmp_path: Path) -> None:
    _, paths, _ = do_run(tmp_path)
    workbook_before = (paths.models_dir / "AAA.xlsx").read_bytes()
    fingerprint_before = read_json(paths.models_dir / "manifest.json")["AAA"]["fingerprint"]

    result, _, _ = do_run(tmp_path, registry={"AAA": FakeBuiltRevised, "BBB": FakePending})
    aaa = by_ticker(result)["AAA"]
    assert aaa.model_status == "changed"
    assert aaa.changed_items == ["revenue"], "only the line item whose value moved"
    assert (paths.models_dir / "AAA.xlsx").read_bytes() != workbook_before
    assert read_json(paths.models_dir / "manifest.json")["AAA"]["fingerprint"] != fingerprint_before
    assert "changed line items: revenue" in paths.diff_path.read_text(encoding="utf-8")
    outputs = read_json(paths.site_data_dir / "AAA.json")["outputs"]
    assert outputs["revenue"]["points"] == [["2024A", 5.0], ["2025E", 11.0]]


def test_missing_workbook_is_re_exported(tmp_path: Path) -> None:
    """A deleted models/<T>.xlsx is rebuilt even though the fingerprint did not change."""
    _, paths, _ = do_run(tmp_path)
    (paths.models_dir / "AAA.xlsx").unlink()
    result, _, _ = do_run(tmp_path)
    assert by_ticker(result)["AAA"].model_status == "changed"
    assert by_ticker(result)["AAA"].changed_items == [], "values did not move"
    assert (paths.models_dir / "AAA.xlsx").is_file()


# --------------------------------------------------------------------------------------------
# Failures are isolated, recorded and never fatal
# --------------------------------------------------------------------------------------------


def test_client_failure_is_isolated_and_reported(tmp_path: Path) -> None:
    client = FakeEdgarClient(
        tmp_path / "raw", fail={("filings", "AAA"): RuntimeError("HTTP 503 for submissions")}
    )
    result, paths, _ = do_run(tmp_path, client=client)

    aaa = by_ticker(result)["AAA"]
    assert aaa.error == "filings: RuntimeError: HTTP 503 for submissions"
    assert aaa.new_filings == [] and aaa.total_filings == 0 and aaa.latest_filing is None
    # The other steps of AAA and the other companies still ran.
    assert aaa.facts_rows == 8 and aaa.model_status == "changed"
    assert by_ticker(result)["BBB"].error is None
    assert by_ticker(result)["CCC"].error is None
    assert result.errors == ["AAA: filings: RuntimeError: HTTP 503 for submissions"]

    assert read_json(paths.site_data_dir / "AAA.json")["reported"]["revenue"]["points"]
    companies = read_json(paths.site_data_dir / "companies.json")
    assert [c["ticker"] for c in companies["companies"]] == ["AAA", "BBB", "CCC"]

    text = paths.diff_path.read_text(encoding="utf-8")
    assert "- Error: filings: RuntimeError: HTTP 503 for submissions" in text
    assert text.rstrip().endswith(
        "## Errors\n\n- AAA: filings: RuntimeError: HTTP 503 for submissions"
    )
    assert "1 error." in text


def test_failed_fetch_falls_back_to_previous_csvs(tmp_path: Path) -> None:
    """A transient outage must not blank the dashboard: yesterday's CSVs feed the site JSON."""
    _, paths, _ = do_run(tmp_path)
    filings_before = (paths.processed_dir / "AAA" / "filings.csv").read_bytes()
    reported_before = (paths.processed_dir / "AAA" / "reported.csv").read_bytes()

    client = FakeEdgarClient(
        tmp_path / "raw",
        fail={
            ("filings", "AAA"): RuntimeError("submissions down"),
            ("company_facts", "AAA"): RuntimeError("facts down"),
        },
    )
    result, _, _ = do_run(tmp_path, client=client)
    aaa = by_ticker(result)["AAA"]
    assert aaa.error == ("filings: RuntimeError: submissions down; facts: RuntimeError: facts down")
    assert aaa.total_filings == 3 and aaa.as_of == "2025-08-14"
    assert aaa.latest_filing == {"form": "10-Q", "date": "2025-08-14", "url": AAA_FILINGS[0].url}
    assert (paths.processed_dir / "AAA" / "filings.csv").read_bytes() == filings_before
    assert (paths.processed_dir / "AAA" / "reported.csv").read_bytes() == reported_before

    site = read_json(paths.site_data_dir / "AAA.json")
    assert site["as_of"] == "2025-08-14"
    assert len(site["reported"]["revenue"]["points"]) == 6
    assert read_json(paths.site_data_dir / "companies.json")["companies"][0]["has_data"] is True


def test_download_failure_records_error_but_continues(tmp_path: Path) -> None:
    client = FakeEdgarClient(
        tmp_path / "raw", fail={("download_filing", "AAA"): OSError("disk full")}
    )
    result, _, _ = do_run(tmp_path, client=client)
    aaa = by_ticker(result)["AAA"]
    assert aaa.downloaded == []
    assert aaa.error == (
        f"download {AAA_FILINGS[0].accession}: OSError: disk full; "
        f"download {AAA_FILINGS[1].accession}: OSError: disk full"
    )
    assert aaa.model_status == "changed", "later steps still ran"
    assert by_ticker(result)["BBB"].downloaded != []


def test_failed_download_is_retried_by_the_next_run(tmp_path: Path) -> None:
    """After a failed download the filing is already in filings.csv, so it is no longer "new":
    the next run must pick it up because its document is missing from the raw cache."""
    broken = FakeEdgarClient(
        tmp_path / "raw", fail={("download_filing", "AAA"): OSError("disk full")}
    )
    first, _, _ = do_run(tmp_path, client=broken)
    assert by_ticker(first)["AAA"].downloaded == []

    second, paths, client = do_run(tmp_path)
    aaa = by_ticker(second)["AAA"]
    assert aaa.new_filings == [] and aaa.error is None
    assert [p.name for p in aaa.downloaded] == ["aaa-10q.htm", "aaa-10k.htm"]
    # BBB's document is cached from the first run and AAA's 2024 filing predates --since.
    assert client.calls["download_filing"] == 2
    text = paths.diff_path.read_text(encoding="utf-8")
    assert "- New filings: none (3 tracked)" in text and "- Downloaded: 2 documents" in text

    third, _, client = do_run(tmp_path)
    assert client.calls["download_filing"] == 0 and by_ticker(third)["AAA"].downloaded == []


def test_cache_lookup_failure_is_recorded_like_a_download_failure(tmp_path: Path) -> None:
    client = FakeEdgarClient(
        tmp_path / "raw", fail={("cached_filing_path", "AAA"): OSError("raw dir unreadable")}
    )
    result, _, _ = do_run(tmp_path, client=client)
    aaa = by_ticker(result)["AAA"]
    assert aaa.downloaded == [] and aaa.error is not None
    assert aaa.error.startswith(f"download {AAA_FILINGS[0].accession}: OSError: raw dir unreadable")
    assert aaa.model_status == "changed", "later steps still ran"


def test_model_error_is_recorded(tmp_path: Path) -> None:
    result, paths, _ = do_run(tmp_path, registry={"AAA": FakeBroken, "BBB": FakePending})
    aaa = by_ticker(result)["AAA"]
    assert aaa.model_status == "error"
    assert aaa.error == "model: ValueError: inputs frame is malformed"
    assert not (paths.models_dir / "AAA.xlsx").exists()
    assert not (paths.models_dir / "manifest.json").exists()
    assert read_json(paths.site_data_dir / "AAA.json")["outputs"] is None
    companies = read_json(paths.site_data_dir / "companies.json")["companies"]
    assert companies[0]["model_status"] == "pending", "no trustworthy output to show"
    assert result.errors == ["AAA: model: ValueError: inputs frame is malformed"]


def test_site_build_failure_is_recorded_and_diff_still_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_build(**_: Any) -> BuildReport:
        raise FileNotFoundError("site template missing: base.html")

    monkeypatch.setattr(refresh, "build_site_pages", broken_build)
    result, paths, _ = do_run(tmp_path, config=RefreshConfig(build_site=True))
    assert result.site is None
    assert result.errors == ["site: FileNotFoundError: site template missing: base.html"]
    assert all(company.error is None for company in result.companies)
    assert paths.diff_path.is_file()
    assert "- site: FileNotFoundError" in paths.diff_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Dry run, selection, site integration
# --------------------------------------------------------------------------------------------


def test_dry_run_prints_plan_and_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = make_paths(tmp_path)
    config = RefreshConfig(tickers=("aaa", "CCC"), since="2025-06-01", download=False, dry_run=True)
    result = run(
        config,
        client=ExplodingClient(),
        registry=REGISTRY,
        companies=COMPANIES,
        paths=paths,
        now=fixed_now,
    )
    assert result.companies == [] and result.errors == [] and result.site is None
    assert result.started_at == result.finished_at == FIXED_NOW_ISO
    assert snapshot(tmp_path) == {}, "a dry run writes nothing"

    out = capsys.readouterr().out
    assert "Dry run" in out
    assert "AAA (model), CCC (data only)" in out
    assert "2025-06-01" in out
    assert "download documents: no" in out and "build site:         yes" in out
    for path in (paths.processed_dir, paths.models_dir, paths.site_data_dir, paths.diff_path):
        assert str(path) in out


def test_tickers_filter_and_unknown_ticker(tmp_path: Path) -> None:
    result, paths, client = do_run(
        tmp_path, config=RefreshConfig(tickers=("bbb",), build_site=False)
    )
    assert [c.ticker for c in result.companies] == ["BBB"]
    assert client.calls["filings"] == 1
    companies = read_json(paths.site_data_dir / "companies.json")["companies"]
    assert [c["ticker"] for c in companies] == ["BBB"]

    with pytest.raises(KeyError, match="unknown ticker 'ZZZ'; known: AAA, BBB, CCC"):
        run(
            RefreshConfig(tickers=("ZZZ",)),
            client=ExplodingClient(),
            registry=REGISTRY,
            companies=COMPANIES,
            paths=paths,
            now=fixed_now,
        )


def test_build_site_integration(tmp_path: Path) -> None:
    """The real site builder runs on the JSON this pipeline writes (templates from the repo)."""
    result, paths, _ = do_run(tmp_path, config=RefreshConfig(build_site=True))
    assert result.errors == []
    assert isinstance(result.site, BuildReport)
    assert result.site.companies == 3
    for page in ("index.html", "companies/AAA.html", "companies/CCC.html", "writeups/index.html"):
        assert page in result.site.pages
        assert (paths.site_build_dir / page).is_file()
    assert (paths.site_build_dir / "data" / "companies.json").is_file()
    assert (paths.site_build_dir / "data" / "calls.json").is_file()
    company_page = (paths.site_build_dir / "companies" / "AAA.html").read_text(encoding="utf-8")
    assert "Alpha Cloud" in company_page
    text = paths.diff_path.read_text(encoding="utf-8")
    assert "## Site" in text and "3 companies" in text
    assert any("calls.md" in warning for warning in result.site.warnings)


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


def test_changed_line_items() -> None:
    current = pd.DataFrame(
        [[1.0, float("nan")], [2.0, 3.0]], index=["a", "b"], columns=["2024A", "2025E"]
    )
    assert changed_line_items(None, current) == ["a", "b"]
    assert changed_line_items(current, current) == [], "NaN equals NaN"

    # Different dtype and column order but the same values: unchanged.
    reordered = current[["2025E", "2024A"]].astype("Float64")
    assert changed_line_items(reordered, current) == []

    previous = pd.DataFrame(
        [[1.0, float("nan")], [2.0, 4.0], [9.0, 9.0]],
        index=["a", "b", "gone"],
        columns=["2024A", "2025E"],
    )
    assert changed_line_items(previous, current) == ["b", "gone"]


def annual_fact(year: int, val: float) -> dict[str, Any]:
    return fact(f"{year}-01-01", f"{year}-12-31", val, f"CY{year}", form="10-K", fp="FY", fy=year)


def quarter_fact(year: int, quarter: int, val: float) -> dict[str, Any]:
    start = f"{year}-{3 * quarter - 2:02d}-01"
    end = f"{year}-{('03-31', '06-30', '09-30', '12-31')[quarter - 1]}"
    return fact(start, end, val, f"CY{year}Q{quarter}", fp=f"Q{quarter}", fy=year)


def revenue_facts(entries: list[dict[str, Any]]) -> pd.DataFrame:
    return facts_to_frame({"facts": {"us-gaap": {"Revenues": {"units": {"USD": entries}}}}})


def test_site_series_keeps_quarterly_despite_older_annual_comparatives() -> None:
    """Just after a 10-K the annual series spans more years than the quarterly one, because
    10-K comparatives reach further back. That alone must not turn the chart annual."""
    quarters = [quarter_fact(2024, q, 100.0 * q) for q in (1, 2, 3)]
    facts = revenue_facts([annual_fact(2023, 650.0), annual_fact(2024, 1000.0), *quarters])
    freq, series = site_series(facts, "revenue")
    assert freq == "Q"
    assert series["period"].tolist() == ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]


def test_site_series_flow_falls_back_to_annual_when_quarterly_is_thin() -> None:
    # Stops short: no Q3, so Q4 cannot be derived and the year's total exists only annually.
    stale = revenue_facts(
        [annual_fact(2024, 1000.0), quarter_fact(2024, 1, 100.0), quarter_fact(2024, 2, 200.0)]
    )
    freq, series = site_series(stale, "revenue")
    assert (freq, series["period"].tolist()) == ("A", ["2024"])

    # Recent enough, but two calendar years against three over the same span.
    sparse = revenue_facts(
        [
            *(annual_fact(year, 1000.0) for year in (2022, 2023, 2024)),
            quarter_fact(2022, 1, 100.0),
            quarter_fact(2025, 1, 300.0),
        ]
    )
    freq, series = site_series(sparse, "revenue")
    assert (freq, series["period"].tolist()) == ("A", ["2022", "2023", "2024"])
    assert reported_payload(sparse)["revenue"]["freq"] == "A"


def test_site_series_without_an_annual_figure_is_quarterly() -> None:
    new_filer = revenue_facts([quarter_fact(2025, 1, 100.0), quarter_fact(2025, 2, 200.0)])
    assert site_series(new_filer, "revenue")[0] == "Q"
    assert reported_payload(None) == {} and reported_payload(revenue_facts([])) == {}


def test_site_data_dir_follows_site_dir(tmp_path: Path) -> None:
    """The site builder reads <site_dir>/data; the pipeline must write exactly there."""
    assert make_paths(tmp_path).site_data_dir == tmp_path / "site" / "data"
    assert RefreshPaths().site_data_dir == SITE_DATA_DIR


def test_read_filings_csv_round_trip(tmp_path: Path) -> None:
    _, paths, _ = do_run(tmp_path)
    filings = read_filings_csv(paths.processed_dir / "AAA" / "filings.csv", AAA.cik)
    assert [f.accession for f in filings] == [f.accession for f in AAA_FILINGS]
    assert filings[0].url == AAA_FILINGS[0].url
    assert filings[0].size == 1234 and filings[0].is_xbrl is True
    assert filings[-1].size is None and filings[-1].report_date is None
    assert filings[-1].is_xbrl is False
    assert read_filings_csv(tmp_path / "missing.csv", AAA.cik) == []


def test_render_diff_no_new_filings_and_no_companies() -> None:
    result = RefreshResult(FIXED_NOW_ISO, FIXED_NOW_ISO, companies=[], site=None, errors=[])
    text = render_diff(result)
    assert text.startswith(f"# Refresh {FIXED_NOW_ISO}\n\n0 companies: no new filings, ")
    assert "models: none" in text
    assert text.endswith("## Errors\n\nNone.\n")

    one = CompanyResult(ticker="AAA", new_filings=[AAA_FILINGS[0]], total_filings=1)
    result = RefreshResult(FIXED_NOW_ISO, FIXED_NOW_ISO, [one], site=None, errors=[])
    assert "1 company: 1 new filing, 0 documents downloaded" in render_diff(result)


def test_render_diff_caps_long_filing_lists() -> None:
    many = [
        make_filing(AAA, f"0000000001-25-{n:06d}", "10-Q", "2025-01-01", None, f"doc{n}.htm")
        for n in range(refresh.MAX_LISTED_FILINGS + 5)
    ]
    company = CompanyResult(ticker="AAA", new_filings=many, total_filings=len(many))
    text = render_diff(RefreshResult(FIXED_NOW_ISO, FIXED_NOW_ISO, [company], None, []))
    assert text.count("  - 10-Q 2025-01-01") == refresh.MAX_LISTED_FILINGS
    assert "... and 5 more (see data/processed/AAA/filings.csv)" in text


def test_write_diff_creates_parents_and_uses_lf(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "diff.md"
    write_diff(RefreshResult(FIXED_NOW_ISO, FIXED_NOW_ISO, [], None, []), path)
    raw = path.read_bytes()
    assert raw.startswith(b"# Refresh ") and b"\r\n" not in raw


def test_utc_iso_formats() -> None:
    assert refresh.utc_iso(FIXED_NOW) == FIXED_NOW_ISO
    naive = datetime(2026, 9, 12, 11, 0, 0, 123456)
    assert refresh.utc_iso(naive) == FIXED_NOW_ISO, "naive is treated as UTC; microseconds dropped"


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def test_main_flags_and_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, RefreshConfig] = {}
    ok = RefreshResult(FIXED_NOW_ISO, FIXED_NOW_ISO, [], None, errors=[])

    def fake_run(config: RefreshConfig, **_: Any) -> RefreshResult:
        seen["config"] = config
        return ok

    monkeypatch.setattr(refresh, "run", fake_run)
    argv = ["--tickers", "crwv,nbis", "--since", "2025-06-01", "--no-download", "--skip-site"]
    assert refresh.main(argv) == 0
    assert seen["config"] == RefreshConfig(
        tickers=("CRWV", "NBIS"), since="2025-06-01", download=False, build_site=False
    )
    assert refresh.main([]) == 0
    assert seen["config"] == RefreshConfig()

    failed = RefreshResult(FIXED_NOW_ISO, FIXED_NOW_ISO, [], None, errors=["NBIS: facts: boom"])
    monkeypatch.setattr(refresh, "run", lambda config, **_: failed)
    assert refresh.main(["-v"]) == 1


def test_main_dry_run_uses_no_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(refresh, "_load_registry", lambda: REGISTRY)
    monkeypatch.setattr(refresh, "EdgarClient", ExplodingClient)
    assert refresh.main(["--dry-run", "--tickers", "CRWV"]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_main_rejects_bad_arguments() -> None:
    with pytest.raises(SystemExit) as bad_ticker:
        refresh.main(["--dry-run", "--tickers", "ZZZ"])
    assert bad_ticker.value.code == 2
    with pytest.raises(SystemExit) as bad_date:
        refresh.main(["--dry-run", "--since", "2025/06/01"])
    assert bad_date.value.code == 2


# --------------------------------------------------------------------------------------------
# Workflows: parser-free structural checks (PyYAML is not a dependency)
# --------------------------------------------------------------------------------------------

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _workflow_text(name: str) -> str:
    path = WORKFLOWS / name
    raw = path.read_bytes()
    assert b"\t" not in raw, f"{name}: YAML must not contain tabs"
    assert b"\r\n" not in raw, f"{name}: LF line endings expected"
    text = raw.decode("utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        assert line == line.rstrip(), f"{name}:{number}: trailing whitespace"
        indent = len(line) - len(line.lstrip(" "))
        assert indent % 2 == 0, f"{name}:{number}: indentation is not a multiple of two"
    return text


def test_ci_workflow_structure() -> None:
    text = _workflow_text("ci.yml")
    for needle in (
        "name: CI",
        "  push:",
        "  pull_request:",
        "permissions:",
        "runs-on: ubuntu-latest",
        "uses: actions/checkout@v4",
        "uses: astral-sh/setup-uv@v6",
        "enable-cache: true",
        "run: uv sync --locked",
        "run: uv run ruff check .",
        "run: uv run ruff format --check .",
        "run: uv run pytest",
    ):
        assert needle in text, needle
    assert "secrets." not in text, "CI needs no secret"


def test_refresh_workflow_structure() -> None:
    text = _workflow_text("refresh.yml")
    for needle in (
        "name: Refresh",
        '- cron: "17 11 * * *"',
        "workflow_dispatch:",
        "concurrency:",
        "group: refresh",
        "fetch-depth: 0",
        "uses: astral-sh/setup-uv@v6",
        "run: uv sync --locked",
        "EDGAR_USER_AGENT: ${{ secrets.EDGAR_USER_AGENT }}",
        "run: uv run scripts/refresh.py --no-download",
        'cat data/last_refresh_diff.md >> "$GITHUB_STEP_SUMMARY"',
        'git config user.name "github-actions[bot]"',
        # Commit only on a data or model change; the timestamped files alone never count.
        'if [ -z "$(git status --porcelain -- data/processed models)" ]; then',
        # data/raw is in the list for the manifests; .gitignore keeps everything else out.
        "for path in data/processed models site/data data/raw data/last_refresh_diff.md; do",
        'if [ -e "$path" ]; then git add -- "$path"; fi',
        "if: ${{ !cancelled() && hashFiles('site/build/index.html') != '' }}",
        "uses: actions/upload-pages-artifact@v3",
        "path: site/build",
        "uses: actions/deploy-pages@v4",
        "name: github-pages",
        "needs: refresh",
        "contents: write",
        "pages: write",
        "id-token: write",
    ):
        assert needle in text, needle
    assert 'Source: "GitHub Actions"' in text, "setup comment for Pages"
    assert text.lstrip().startswith("#"), "starts with the one-time setup comment"

    # A scheduled job must never reach SEC with the placeholder contact address.
    assert text.index("::error::EDGAR_USER_AGENT secret") < text.index("uses: actions/checkout@")
    # `git add` with a pathspec that matches nothing stages nothing at all and fails the step,
    # so the existence-checked loop has to be the only place anything is staged.
    staging = [
        line.strip()
        for line in text.splitlines()
        if "git add" in line and not line.lstrip().startswith("#")
    ]
    assert staging == ['if [ -e "$path" ]; then git add -- "$path"; fi']
    # Staging data/raw is only safe while .gitignore lets nothing but the manifests through.
    ignore_rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for rule in ("data/raw/**", "!data/raw/**/", "!data/raw/**/manifest.json"):
        assert rule in ignore_rules, rule
    for action in (
        "actions/checkout@",
        "astral-sh/setup-uv@",
        "upload-pages-artifact@",
        "deploy-pages@",
    ):
        for line in text.splitlines():
            if action in line:
                version = line.split("@", 1)[1].strip()
                assert version.startswith("v") and version[1:].isdigit(), f"pin a major: {line}"
