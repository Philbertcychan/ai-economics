"""Pull the newest EIA-860M workbook, tidy every sheet and write the processed tables.

Purpose
-------
The US generator inventory for the power stage, in one command::

    uv run scripts/pull_eia860m.py                 # newest workbook: download, tidy, write
    uv run scripts/pull_eia860m.py --dry-run       # print the URL and the files it would write
    uv run scripts/pull_eia860m.py --file PATH     # tidy a workbook already on disk (offline)
    uv run scripts/pull_eia860m.py --out DIR       # write somewhere other than data/processed

It (1) reads the EIA-860M index page for the newest ``<month>_generator<year>.xlsx``, (2) stores
the workbook under ``data/raw/EIA860M/<UTC date>/`` with a manifest (sha256 per file), (3) tidies
every sheet with ``data.eia.tidy_all`` and writes one CSV per sheet into
``data/processed/eia860m/`` (``operating.csv``, ``planned.csv``, ``retired.csv`` and one per
extra sheet, such as ``canceled_or_postponed.csv``), (4) writes the four summaries into
``summaries/`` and (5) records where it all came from in ``source.json`` (URL, file name, sha256,
period, pull time, rows per sheet). CSVs no longer produced by the current workbook are removed,
because the processed folder is rebuilt by code and must not keep stale tables.

Design notes
------------
* Orchestration only. Header detection, validation and the summaries live in ``data/eia.py``;
  this script moves files and prints a short report.
* Byte-stable output: UTF-8, LF line endings, ``index=False``, rows sorted by the tidy functions,
  so a Windows laptop and the Linux runner commit the same CSVs. ``source.json`` carries the pull
  time and therefore differs between runs; the CSVs do not unless the workbook changed.
* Exit code 0 on success, 1 when the pull or the tidy failed (the error is printed), 2 for bad
  arguments.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pandas as pd

from data import EIA_PROCESSED_DIR, EIA_RAW_DIR
from data.edgar import sha256_file, utc_now_iso
from data.eia import (
    CORE_SHEETS,
    SOURCE,
    EIA860MClient,
    capacity_by_fuel,
    period_from_name,
    planned_by_year_and_fuel,
    retired_by_year_and_fuel,
    state_summary,
    tidy_all,
)

log = logging.getLogger(__name__)

SUMMARIES_DIR = "summaries"
SOURCE_JSON = "source.json"
# summary file name -> how it is computed from the tidied sheets
SUMMARIES: dict[str, Callable[[Mapping[str, pd.DataFrame]], pd.DataFrame]] = {
    "capacity_by_fuel": lambda frames: capacity_by_fuel(frames["operating"]),
    "planned_by_year_and_fuel": lambda frames: planned_by_year_and_fuel(frames["planned"]),
    "retired_by_year_and_fuel": lambda frames: retired_by_year_and_fuel(frames["retired"]),
    "state_summary": lambda frames: state_summary(frames["operating"], frames["planned"]),
}
TOP_N = 3  # technologies listed in the console report
PLANNED_WINDOW_YEARS = 3  # the report's planned window: the file's year and the two after it


def _write_csv(table: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def planned_outputs(out_dir: Path, sheet_keys: list[str] | None = None) -> list[Path]:
    """The files a run writes into ``out_dir``; the core sheets only when the keys are unknown."""
    keys = list(sheet_keys) if sheet_keys is not None else list(CORE_SHEETS)
    paths = [out_dir / f"{key}.csv" for key in keys]
    paths += [out_dir / SUMMARIES_DIR / f"{name}.csv" for name in SUMMARIES]
    return paths + [out_dir / SOURCE_JSON]


def write_processed(
    frames: Mapping[str, pd.DataFrame], out_dir: Path, source: dict[str, object]
) -> list[Path]:
    """Write one CSV per sheet, the summaries and ``source.json``; drop CSVs not written now."""
    written: list[Path] = []
    for key, frame in sorted(frames.items()):
        path = out_dir / f"{key}.csv"
        _write_csv(frame, path)
        written.append(path)
    for name, compute in SUMMARIES.items():
        path = out_dir / SUMMARIES_DIR / f"{name}.csv"
        _write_csv(compute(frames), path)
        written.append(path)
    source_path = out_dir / SOURCE_JSON
    _write_json(
        source_path, {**source, "rows": {k: int(len(v)) for k, v in sorted(frames.items())}}
    )
    written.append(source_path)

    keep = {p.resolve() for p in written}
    for folder in (out_dir, out_dir / SUMMARIES_DIR):
        for stale in folder.glob("*.csv") if folder.is_dir() else ():
            if stale.resolve() not in keep:
                stale.unlink()
                log.info("removed stale %s", stale)
    return written


def report(frames: Mapping[str, pd.DataFrame], period: str | None) -> str:
    """A few lines for the console: period, rows per sheet, the leading technologies."""
    lines = [
        f"EIA-860M {period or 'unknown period'}: "
        + ", ".join(f"{key} {len(frame)} rows" for key, frame in sorted(frames.items()))
    ]
    capacity = capacity_by_fuel(frames["operating"]).head(TOP_N)
    lines.append(
        "  operating nameplate MW by technology: "
        + "; ".join(f"{r.technology} {r.nameplate_mw:,.0f}" for r in capacity.itertuples())
    )
    planned = planned_by_year_and_fuel(frames["planned"])
    if period is not None:
        first = int(period[:4])
        last = first + PLANNED_WINDOW_YEARS - 1
        window = planned[planned["year"].between(first, last)]
        by_fuel = window.groupby("technology")["nameplate_mw"].sum().sort_values(ascending=False)
        lines.append(
            f"  planned {first}-{last} nameplate MW by technology: "
            + "; ".join(f"{tech} {mw:,.0f}" for tech, mw in by_fuel.head(TOP_N).items())
        )
    return "\n".join(lines)


def run(
    *,
    file: Path | None,
    out_dir: Path,
    raw_dir: Path,
    dry_run: bool,
    client: EIA860MClient | None = None,
) -> int:
    """Do the pull (or tidy ``file``), write everything, print the report. Returns an exit code."""
    url: str | None = None
    if file is None:
        client = client or EIA860MClient(raw_dir)
        url = client.latest_file_url()
        log.info("newest workbook: %s", url)
    if dry_run:
        source = url or str(file)
        print(f"Dry run: would tidy {source} and write")
        for path in planned_outputs(out_dir):
            print(f"  {path}")
        if file is None:
            print(f"  (raw copy and manifest under {raw_dir})")
            print("  plus one CSV per extra sheet in the workbook")
        return 0

    if file is None:
        assert client is not None and url is not None  # set above; for the type checker
        workbook = client.download(url)
        client.write_manifest()
    else:
        workbook = Path(file)
        if not workbook.is_file():
            print(f"error: {workbook} is not a file", file=sys.stderr)
            return 1

    frames = tidy_all(workbook)
    period = period_from_name(workbook.name)
    source = {
        "source": SOURCE,
        "url": url,
        "file": workbook.name,
        "period": period,
        "sha256": sha256_file(workbook),
        "bytes": workbook.stat().st_size,
        "pulled_at": utc_now_iso(),
    }
    written = write_processed(frames, out_dir, source)
    print(report(frames, period))
    print(f"wrote {len(written)} files under {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; see the module docstring for the commands."""
    parser = argparse.ArgumentParser(
        prog="pull_eia860m",
        description="Download the newest EIA-860M generator inventory and tidy it into CSVs.",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        metavar="PATH",
        help="tidy this workbook instead of downloading the newest one (offline use)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=EIA_PROCESSED_DIR,
        metavar="DIR",
        help=f"where the CSVs and source.json go (default {EIA_PROCESSED_DIR})",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=EIA_RAW_DIR,
        metavar="DIR",
        help=f"where the downloaded workbook and its manifest go (default {EIA_RAW_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the URL and the output paths; write nothing"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return run(file=args.file, out_dir=args.out, raw_dir=args.raw_dir, dry_run=args.dry_run)
    except Exception as exc:  # one command, one failure: say what broke and exit 1
        log.debug("pull failed", exc_info=True)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
