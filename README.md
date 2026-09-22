# ai-economics

An open financial model of the AI build-out. The question it tries to answer: what does a dollar
of GPU compute actually earn, who along the chain captures that dollar, and does the capacity
being built (capex, megawatts, GPUs) match the demand being guided to in public filings?
Everything here is reproducible from SEC EDGAR data plus a list of explicit assumptions, and
every published view ends with a position and the number that would prove it wrong.

## The three layers

Each tracked company is tagged with one layer (`COMPANIES` in `data/edgar.py`):

| Layer | Companies | Role in the model |
|---|---|---|
| chip | NVIDIA (NVDA) | supplies the accelerators the other two layers buy |
| neocloud | CoreWeave (CRWV), Nebius (NBIS) | buys GPUs and rents them by the hour; the most direct read on GPU unit economics |
| hyperscaler | Microsoft (MSFT), Alphabet (GOOGL), Amazon (AMZN), Meta (META) | builds and operates most of the capacity; tracked for reported capex and cash flow |

Operating models (`companies/`) exist for the two neoclouds. The refresh pipeline pulls reported
data for all seven.

## Repository map

```
engine/       the economics of one GPU: cost stack, rental and token revenue, margin, payback
assumptions/  one CSV per company: every non-reported number with its source, range and sign-off status
companies/    one model class per company on shared plumbing; loads the assumptions register
data/         EDGAR client, interfaces for transcripts and GPU prices; raw/ (dated pulls, gitignored
              except manifests), processed/ (tidy CSVs, committed), last_refresh_diff.md
scripts/      export_xlsx.py (workbook), build_site.py (static site), refresh.py (daily pipeline)
models/       generated <TICKER>.xlsx per company plus manifest.json (export fingerprints)
site/         content/ (writeups), data/ (JSON for the dashboard), templates/, static/, build/ (generated)
docs/         modeling-approach.md (the financial reasoning) and code-tour.md (how the program is organised)
tests/        offline pytest suite; tests/fixtures/edgar/ holds trimmed real EDGAR responses
notes/        reading notes on filings (notes/coreweave-s1.md)
.github/      workflows/ci.yml (lint + tests) and workflows/refresh.yml (daily data pull + Pages deploy)
calls.md      every position taken in a writeup, with the number that would falsify it
TODO.md       staged plan, one checklist per stage
log.md        one line per working session
```

### Tooling and placeholder files

Files that hold no model content but that the toolchain or the pipeline depends on:

- `uv.lock`: the exact resolved package versions, so `uv sync` installs the same environment on a
  laptop and in CI.
- `.python-version`: pins the interpreter (`3.12`) that uv selects.
- `.gitattributes`: forces LF line endings on every text file and marks `*.xlsx` and `*.png` as
  binary, so a Windows laptop and the Linux runner commit byte-identical CSV, JSON and workbooks.
- `.gitignore`: keeps the virtualenv, tool caches, raw EDGAR documents (everything under
  `data/raw/` except the manifests) and the generated `site/build/` out of git.
- `.gitkeep` (in `data/raw/`, `data/processed/`, `models/`, `site/data/`, `site/build/`): empty
  placeholders, because git does not track empty directories and the pipeline writes into these.
- `site/templates/`: the `string.Template` HTML files `scripts/build_site.py` fills in.
- `site/static/`: the stylesheet and dashboard script, copied verbatim into `site/build/static/`.
- `tests/fixtures/edgar/`: trimmed real EDGAR responses, so the test suite never needs the network.

## Run locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). Run everything from the repo root.

```
uv sync                                # create .venv, install the packages (editable) and dev tools
uv run pytest                          # offline test suite (there are no live EDGAR tests yet)
uv run scripts/refresh.py              # pull EDGAR data, tidy it, export built models, build the site
uv run scripts/export_xlsx.py CRWV     # write models/CRWV.xlsx for one company
uv run scripts/build_site.py           # render site/build/ from site/ and calls.md
```

Options:

- `scripts/refresh.py --tickers CRWV,NBIS --since YYYY-MM-DD --no-download --skip-site --dry-run -v`.
  `--tickers` limits the run (default: all seven); `--since` is the earliest filing date whose
  documents are downloaded (default `2025-01-01`); `--no-download` skips the documents themselves
  (this is what CI uses); `--skip-site` stops after the data step; `--dry-run` fetches and writes
  nothing and prints the plan. Results are summarised in `data/last_refresh_diff.md`.
- `scripts/export_xlsx.py CRWV --out models/CRWV.xlsx`. Reads `data/processed/CRWV/` offline and
  never contacts EDGAR; run a refresh first for current data. Until a company's `build()` is
  written the command prints a "model pending" message and exits with code 2.
- `scripts/build_site.py --out DIR`. Works with no data present (it renders an empty state with a
  note). A build first clears its output subfolders, so `--out` is refused when it is the repo
  root, `site/` or anything else that overlaps the site sources. Links are relative and each page
  carries an inline copy of its data, so `site/build/index.html` opens straight from disk. Charts
  load Chart.js from a CDN; offline you get the server-rendered tables only. To preview exactly
  what GitHub Pages serves: `uv run python -m http.server 8000 --directory site/build`.
  Each company page lists the XBRL element behind every reported series, because filers tag the
  same line differently (capex is three different elements across the seven companies).

### SEC User-Agent

The SEC asks automated clients to identify themselves with a name and a contact email in the
`User-Agent` header and to stay under 10 requests per second; the client in `data/edgar.py` spaces
requests to respect the limit and reads the header from `EDGAR_USER_AGENT`:

```
export EDGAR_USER_AGENT="Your Name you@example.com"            # bash / zsh
$env:EDGAR_USER_AGENT = "Your Name you@example.com"            # PowerShell
```

Set it locally and as a repository secret of the same name for the scheduled refresh. If it is
unset the client falls back to a built-in placeholder and logs a warning once per process. The
placeholder carries no real contact address, which is what the SEC asks for, so it is there only
to keep a one-off local run from crashing; do not rely on it for repeated pulls. The scheduled
workflow refuses to run without the `EDGAR_USER_AGENT` secret.

### Tests

`uv run pytest` runs offline; fixtures are trimmed real EDGAR responses. No test touches the
network: the `network` marker registered in `pyproject.toml` is reserved for future live EDGAR
tests, and none exist yet.

The engine's expected numbers are worked out by hand, outside the engine, and pinned as literals:
a test that recomputes the formula it is testing can never fail. Behaviour is tested as well
(more utilisation never raises unit cost; a GPU that loses cash never pays back).

Lint and formatting: `uv run ruff check .`
and `uv run ruff format --check .` (both run in CI).

## How to read the model

This section is for the reader who opens `models/<TICKER>.xlsx` rather than the code.

### The workbook

Sheets, in order:

1. **README**: title, as-of date, CIK, layer, this legend, and the sheet list.
2. **Inputs**: one row per assumption with `Name | Value | Unit | Source | Note`. This is the only
   place assumptions live. Each value cell has a workbook-defined name `in_<name>` (for example
   `in_utilization`), so formulas elsewhere read `in_utilization` rather than `Inputs!B7`.
3. **Drivers**: the operating build, one line item per row, one period per column
   (`2024A`, `2025A`, `2026E`, or quarterly `2025Q1A`). Column A is the label, column B the unit.
4. **Outputs**: the headline lines derived from Drivers, same layout.
5. **Reported**: SEC XBRL facts for the company, tidied by `data/edgar.py` (one row per
   concept, period and filing, with the tag, taxonomy, unit and accession). Plain values, not
   modelled. Use it to check the drivers against what the company actually reported.

Colour convention (the standard one in finance):

- **Blue** font: a hard-coded input. Change these on the Inputs sheet, nowhere else.
- **Black** font: a formula on the same sheet.
- **Green** font: a formula that links to another sheet.
- **Grey** font (an addition to the convention): a value the Python model computed and pasted. It
  does not recalculate in Excel; the company module under `companies/` shows how it was computed.

The workbook is generated by code (`scripts/export_xlsx.py`, openpyxl), which writes formulas but
cannot compute their results. It is flagged for full recalculation on open, so Excel fills in the
numbers when you open it and may offer to save. A viewer that does not recalculate will show
formula cells empty. Files are written deterministically (fixed metadata timestamp), so a re-export
with unchanged inputs is byte-identical and does not churn the repository.

### Per GPU-hour

"Per GPU-hour" always means per wall-clock hour of an installed GPU: 8,760 hours a year, whether
or not the GPU is busy. Idle time lowers revenue; it never shrinks the hour count, so idle capacity
shows up as cost instead of disappearing. The engine (`engine/unit_economics.py`) builds the cost
of that hour in four layers and offers two ways to earn revenue from it:

- **Capital**: purchase price x capital recovery factor / 8,760. The factor is the mortgage
  formula, `rate / (1 - (1 + rate) ** -years)`: a level annual charge that repays the price with
  interest on the declining balance.
- **Energy**: kW x PUE x price per kWh x load factor. **Facility**: data-centre space per
  kW-month. **Other**: staff, maintenance, software, network.
- **Cash cost** is energy + facility + other. **Fully loaded cost** adds capital.
- **Revenue**: rental (price per GPU-hour x share of hours rented) or tokens (tokens per hour x
  price per million).
- **Payback**: purchase price / monthly cash margin, before financing, the way operators quote it.

The reasoning behind each choice is in [docs/modeling-approach.md](docs/modeling-approach.md).

### Assumptions

Every number that is not a reported fact lives in `assumptions/<TICKER>.csv`, never in code. Each
row carries a basis (`disclosed`, `derived`, `external` or `judgment`), a source, an optional range
and a status. Values start as `proposed`; the owner of the model sets `confirmed` or `overridden`.
The workbook's Inputs sheet prints basis, status and range next to each value.

### Positions and calls

Every writeup under `site/content/` follows `site/content/_template.md` and ends with two required
sections: **Position** and **What would prove this wrong**, the latter a table of
`claim | falsifying number | deadline`. Those rows are copied into `calls.md` at the repo root,
which the site builder renders on the index page. Outcomes are graded `open`, `right`, `wrong` or
`partial`, and rows are never deleted: misses stay visible next to hits.

## Status

As of 2026-09-22:

- Data pipeline, workbook exporter, site and daily refresh are live for seven companies.
- The unit-economics engine is implemented and tested.
- CoreWeave has a full model: six reported quarters and a ten-quarter forecast of contracts,
  capacity, revenue, EBITDA, capex, prepayments, receivables, debt, interest and cash, with
  three definitions of payback per GPU and a one-at-a-time sensitivity sheet. Operating KPIs
  come from the earnings releases; assumptions from the register built from the S-1 and 10-Qs.

Next, in order (see `TODO.md`): a spend-to-live lag for capex, the signals ledger of evidence
from outside filings, the first writeup and call; then Nebius in Stage 2.

Division of labour: the modelling and code are built with Claude; every assumption and every call
is reviewed and owned by the author.

## Data sources

- **SEC EDGAR** (free, no key). Submissions (the filing index, including the older paged
  history), company facts (XBRL financial data), full-text search, and the primary documents of
  filings. Pulls are cached under `data/raw/<TICKER>/<YYYY-MM-DD>/` with a `manifest.json`
  (paths, sizes, sha256, source URLs). Raw files are gitignored because they are large and can be
  pulled again; the manifests are versioned, so a processed number can be traced to the bytes SEC
  served on a given day. The scheduled refresh commits the manifests together with the rest of its
  output on days when the processed data or the models changed. Tidy output goes to
  `data/processed/<TICKER>/` as `filings.csv` and `reported.csv`, plus `outputs.csv` once the
  company's model builds: the values of the last exported Outputs sheet, which the next refresh
  compares against to name the changed line items in `data/last_refresh_diff.md`.
  `reported.csv` maps a dozen standard concepts (revenue, cost of revenue, operating income, net
  income, D&A, capex, PP&E, long-term debt, cash, operating cash flow, interest expense, shares
  outstanding) onto the candidate XBRL tag whose data is most recent (filers change tags, and a
  retired tag stays in the feed forever), keeps the tag, taxonomy and unit as columns, and prefers
  USD where several currencies are reported (Nebius files in USD and RUB). Series by period come
  from `calendar_series` in `data/edgar.py` and follow the calendar periods SEC assigns. SEC
  leaves a quarter out when the filer reported only a cumulative figure (a 10-Q gives cash-flow
  items year-to-date, and a 10-K gives the full year but no fourth quarter); those quarters are
  filled by subtracting consecutive year-to-date figures, or as FY minus Q1-Q3, and carry
  `derived=True`. A gap that is not one quarter long stays a gap.
- **Transcripts**: `data/transcripts.py` is an interface only. `TRANSCRIPT_PROVIDER` selects a
  provider; only `none` exists and it raises `SourceNotConfigured`.
- **GPU rental prices**: `data/gpu_prices.py` reads a hand-maintained CSV of public quotes at
  `data/processed/gpu_prices.csv` (columns: date, gpu_model, provider, region, price_per_gpu_hour,
  term, source_url). `GPU_PRICE_PROVIDER` defaults to `csv`.

The refresh passes all of this to the site as JSON under `site/data/`: one `<TICKER>.json` per
company and an index, `companies.json`, shaped
`{as_of, companies: [{ticker, name, layer, cik, model_status, latest_filing, has_data}]}`.
`model_status` is one of three values:

| `model_status` | Meaning |
|---|---|
| `built` | the company's model built and its outputs are in `<TICKER>.json` |
| `pending` | a model class is registered, but `build()` is unwritten or its last run failed |
| `no-model` | tracked for reported data only (no class in `companies/`); shown as "data only" |

## Automation

- `ci.yml`: on every push and pull request, `ruff check`, `ruff format --check`, `pytest`.
- `refresh.yml`: daily at 11:17 UTC and on manual dispatch, runs `scripts/refresh.py --no-download`,
  commits on days when `data/processed` or `models` changed (the commit also carries `site/data`,
  `data/last_refresh_diff.md` and that day's `data/raw` manifests; on other days the run report is
  in the job summary only), and on every run deploys `site/build/` to GitHub Pages at
  `https://philbertcychan.github.io/ai-economics/`. One-time setup: Settings -> Pages -> Source: GitHub
  Actions, and the `EDGAR_USER_AGENT` secret, without which the workflow stops before it contacts
  the SEC.

Repository: <https://github.com/Philbertcychan/ai-economics>.
