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
every sheet with ``data.eia.process_workbook`` and writes one CSV per sheet into
``data/processed/eia860m/`` (``operating.csv``, ``planned.csv``, ``retired.csv`` and one per
extra sheet, such as ``canceled_or_postponed.csv``), (4) writes the four summaries into
``summaries/`` and (5) records where it all came from in ``source.json`` (URL and fetch time from
the raw manifest, file name, sha256, period, rows per sheet, the files written, sheets skipped).
CSVs that an earlier run listed in ``source.json`` and this run did not write are removed, because
the processed folder is rebuilt by code and must not keep stale tables; nothing else in the folder
is touched.

Design notes
------------
* Orchestration only. Header detection, validation, the summaries, the period check and the
  writing of the processed folder live in ``data/eia.py`` (``process_workbook``), shared with
  the daily refresh; this script moves files and prints a short report.
* Byte-stable output: UTF-8, LF line endings, ``index=False``, rows sorted by the tidy functions,
  so a Windows laptop and the Linux runner commit the same CSVs. ``source.json`` carries no run
  time: its provenance comes from the raw folder's manifest, so the same workbook gives the same
  folder, run after run, and the daily refresh commits only when EIA published something new.
* Everything is computed before anything is written, and ``source.json`` last, so a failure
  part-way leaves the previous ``source.json`` describing what is on disk.
* Exit code 0 on success, 1 when the pull or the tidy failed (the error is printed), 2 for bad
  arguments.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping
from pathlib import Path

import pandas as pd

from data import EIA_PROCESSED_DIR, EIA_RAW_DIR
from data.eia import EIA860MClient, planned_in_window, planned_outputs, process_workbook

log = logging.getLogger(__name__)

TOP_N = 3  # technologies listed in the console report
PLANNED_WINDOW_YEARS = 3  # the report's planned window: the file's year and the two after it


def report(
    frames: Mapping[str, pd.DataFrame], summaries: Mapping[str, pd.DataFrame], period: str | None
) -> str:
    """A few lines for the console: period, rows per sheet, the leading technologies."""
    lines = [
        f"EIA-860M {period or 'unknown period'}: "
        + ", ".join(f"{key} {len(frame)} rows" for key, frame in sorted(frames.items()))
    ]
    capacity = summaries["capacity_by_fuel"].head(TOP_N)
    lines.append(
        "  operating nameplate MW by technology: "
        + "; ".join(f"{r.technology} {r.nameplate_mw:,.0f}" for r in capacity.itertuples())
    )
    if period is not None:
        first = int(period[:4])
        last = first + PLANNED_WINDOW_YEARS - 1
        window = planned_in_window(
            summaries["planned_by_year_and_fuel"], first, PLANNED_WINDOW_YEARS
        )
        lines.append(
            f"  planned {first}-{last} nameplate MW by technology: "
            + "; ".join(
                f"{r.technology} {r.nameplate_mw:,.0f}" for r in window.head(TOP_N).itertuples()
            )
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

    processed = process_workbook(workbook, out_dir)
    print(report(processed.tidied.frames, processed.summaries, processed.period))
    print(f"wrote {len(processed.written)} files under {out_dir}")
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
