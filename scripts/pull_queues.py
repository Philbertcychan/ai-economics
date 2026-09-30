"""Pull LBNL's "Queued Up" interconnection-queue workbook, tidy it and write the processed tables.

Purpose
-------
The project-level interconnection queues for the grid stage, in one command::

    uv run scripts/pull_queues.py                  # download the yearly workbook, tidy, write
    uv run scripts/pull_queues.py --dry-run        # print the URL and the files it would write
    uv run scripts/pull_queues.py --file PATH      # tidy a workbook already on disk (offline)
    uv run scripts/pull_queues.py --url URL        # a newer edition than the one coded in
    uv run scripts/pull_queues.py --out DIR        # write somewhere other than data/processed

It (1) downloads the workbook named in ``data.queues.FILE_URL`` (or ``--url``) into
``data/raw/LBNL_QUEUES/<UTC date>/`` with a manifest (sha256 per file), (2) tidies the sheet
``03. Complete Queue Data`` with ``data.queues.process_workbook`` and writes ``requests.csv``
(one row per request, without the free-text columns) into ``data/processed/queues/``, (3) writes
the six summaries into ``summaries/`` and (4) records where it all came from in ``source.json``.
The file is published once a year, so this command is run by hand when a new edition appears;
the index page at https://emp.lbl.gov/queues refuses non-browser clients, which is why the URL is
a constant rather than scraped (see ``data/queues.py``).

Design notes
------------
* Orchestration only: validation, the summaries and the writing of the processed folder live in
  ``data/queues.py``; this script moves files and prints a short report.
* Byte-stable output (UTF-8, LF, no index, sorted rows) and a ``source.json`` without a run
  time, so the same workbook gives the same folder on any machine.
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

from data import QUEUES_PROCESSED_DIR, QUEUES_RAW_DIR
from data.queues import FILE_URL, TOTAL, LBNLQueuesClient, planned_outputs, process_workbook

log = logging.getLogger(__name__)


def report(summaries: Mapping[str, pd.DataFrame], through: int, rows: int) -> str:
    """A few lines for the console: the data year, the row count and the headline totals."""
    active = summaries["active_by_region_and_type"]
    total = active[active["region"] == TOTAL]
    lines = [f"LBNL queues, data through {through}: {rows:,} requests"]
    if not total.empty:
        row = total.iloc[0]
        gas = f"{row['Gas']:,.0f} GW gas" if "Gas" in total.columns else ""
        lines.append(
            f"  active capacity {row['total']:,.0f} GW" + (f", of which {gas}" if gas else "")
        )
    ia = summaries["ia_executed_not_operational"]
    ia_total = ia[ia["region"] == TOTAL]
    if not ia_total.empty:
        lines.append(
            f"  interconnection agreement executed, not yet operating: "
            f"{ia_total.iloc[0]['total']:,.0f} GW"
        )
    months = summaries["median_months_to_operation"]
    last = months[months["year"].astype(str) == str(through)]
    if not last.empty and "overall" in last.columns and pd.notna(last.iloc[0]["overall"]):
        lines.append(
            f"  median months from request to operation, projects completed in {through}: "
            f"{last.iloc[0]['overall']:.0f}"
        )
    return "\n".join(lines)


def run(
    *,
    file: Path | None,
    url: str,
    out_dir: Path,
    raw_dir: Path,
    dry_run: bool,
    client: LBNLQueuesClient | None = None,
) -> int:
    """Do the pull (or tidy ``file``), write everything, print the report. Returns an exit code."""
    if dry_run:
        print(f"Dry run: would tidy {file or url} and write")
        for path in planned_outputs(out_dir):
            print(f"  {path}")
        if file is None:
            print(f"  (raw copy and manifest under {raw_dir})")
        return 0
    if file is None:
        client = client or LBNLQueuesClient(raw_dir)
        workbook = client.download(url)
        client.write_manifest()
    else:
        workbook = Path(file)
        if not workbook.is_file():
            print(f"error: {workbook} is not a file", file=sys.stderr)
            return 1
    processed = process_workbook(workbook, out_dir)
    print(report(processed.summaries, processed.through, len(processed.requests)))
    print(f"wrote {len(processed.written)} files under {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; see the module docstring for the commands."""
    parser = argparse.ArgumentParser(
        prog="pull_queues",
        description="Download LBNL's Queued Up workbook and tidy the interconnection queues.",
    )
    parser.add_argument(
        "--file", type=Path, default=None, metavar="PATH", help="tidy this workbook (offline)"
    )
    parser.add_argument(
        "--url",
        default=FILE_URL,
        metavar="URL",
        help=f"the workbook to download (default {FILE_URL})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=QUEUES_PROCESSED_DIR,
        metavar="DIR",
        help=f"where the CSVs and source.json go (default {QUEUES_PROCESSED_DIR})",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=QUEUES_RAW_DIR,
        metavar="DIR",
        help=f"where the downloaded workbook and its manifest go (default {QUEUES_RAW_DIR})",
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
        return run(
            file=args.file,
            url=args.url,
            out_dir=args.out,
            raw_dir=args.raw_dir,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # one command, one failure: say what broke and exit 1
        log.debug("pull failed", exc_info=True)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
