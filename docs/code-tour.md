# How the program is organised

A tour of the code for someone who wants to understand how a model like this is built as
software, and reuse the structure elsewhere. The financial reasoning is in
[modeling-approach.md](modeling-approach.md).

## 1. The pipeline

Data flows one way, left to right. Each stage reads only from the stage before it and writes to
its own folder, so any stage can be re-run, inspected or replaced without touching the others.

```
 SEC EDGAR            data/raw/            data/processed/        companies/          models/*.xlsx
 (the internet)  ->   exact copies    ->   tidy tables       ->   the models     ->   site/data/*.json
                      of what was          one row per fact       drivers and             |
                      downloaded                                  outputs                 v
                                                ^                     ^               site/build/
                                                |                     |               (the website)
                                        data/edgar.py         assumptions/*.csv
                                                              engine/ (one GPU)
```

One command runs the whole chain: `uv run scripts/refresh.py`. GitHub runs it every day.

## 2. Folder by folder

| Folder | What lives there | Rule |
|---|---|---|
| `data/raw/` | files exactly as downloaded, in a folder per company per day | never edited by hand; too big for git, so only a manifest (a list of files with fingerprints) is committed |
| `data/processed/` | tidy CSVs built from raw: `filings.csv`, `reported.csv` | always rebuilt by code, so it can be deleted and regenerated |
| `data/edgar.py` | the SEC client and the functions that tidy reported facts | knows about the SEC; knows nothing about models |
| `engine/` | the economics of one GPU | pure arithmetic: numbers in, numbers out, no files, no internet |
| `assumptions/` | one CSV per company of every non-reported number, with source and status | the only place assumptions live; edited by hand, in Excel if you like |
| `companies/` | one module per company, all with the same four steps | reads processed data and assumptions; produces tables |
| `models/` | the Excel workbook per company | generated; never edited by hand |
| `scripts/` | the commands: refresh, export a workbook, build the site | orchestration only; the thinking is in `engine/` and `companies/` |
| `site/` | templates, styles, writeups, and the JSON the charts read | `site/build/` is generated |
| `notes/` | reading notes per filing, with page references | the human-readable evidence behind assumptions |
| `tests/` | checks that run on every change | see section 5 |
| `docs/` | these two guides | |

## 3. The design rules, and why

**One direction, clear boundaries.** The SEC client does not know what a model is. The engine
does not know what a company is. The site does not compute anything financial. When something
is wrong, this tells you where to look, and lets a piece be swapped (a new data source, a new
company) without rewriting the rest.

**Raw data is sacred.** What was downloaded is stored untouched with a fingerprint of each
file. If a number on the site looks wrong, it can be traced back to the exact bytes the SEC
served on a given day.

**Assumptions never live in code.** A number typed into a Python file is invisible to a finance
reader and easy to forget. Every such number sits in `assumptions/<TICKER>.csv` with where it
came from. The code only does arithmetic on them.

**Pure functions for the maths.** Each function in the engine takes inputs and returns a number,
and does nothing else. That makes each one a sentence you can read, a formula you can put on a
spreadsheet, and a thing you can test in isolation.

**Everything is reproducible from one command.** No manual steps, no notebook state. A fresh
copy of the repository plus `uv sync` plus one command gives the same outputs.

**The workbook is an output, not the model.** The model is the code and the assumptions. Excel
is how a finance reader inspects it: inputs in blue, formulas in black, links in green, values
computed in Python in grey. It is regenerated, never edited.

## 4. How a number travels

**A reported number: Microsoft's capex for a quarter.**

1. `data/edgar.py` downloads Microsoft's "company facts" file from the SEC and stores it under
   `data/raw/MSFT/<date>/`.
2. `facts_to_frame` picks, for each concept such as capex, the accounting tag the company
   actually uses (companies label the same line differently) and writes one row per fact to
   `data/processed/MSFT/reported.csv`.
3. `calendar_series` turns those facts into a clean quarterly series. Cash-flow items are filed
   as year-to-date totals, so the second quarter is "six months minus three months"; such
   values are flagged as derived.
4. `scripts/refresh.py` writes the series to `site/data/MSFT.json`, and the site draws it.

**An assumption: the cost of a CoreWeave GPU.**

1. The S-1 gives technology equipment of $9.1bn (p.F-29) and more than 250,000 GPUs (p.2).
   Both are in `notes/coreweave-s1.md` with their pages.
2. `assumptions/CRWV.csv` has a row `chip_cost = 36586`, basis `derived`, with the arithmetic
   in the note and status `proposed` until it is signed off.
3. `companies/assumptions.py` loads the file, checks every row (a missing source or a value
   outside its own range is an error, not a warning) and hands the engine its inputs.
4. `engine/unit_economics.py` turns it into a capital charge per GPU-hour.
5. The workbook's Inputs sheet shows the value in blue with its source, basis and status.

## 5. Tests

Tests are small programs that check the code still does what it should. They run automatically
on every change pushed to GitHub, and locally with `uv run pytest`. Two ideas matter:

- **Expected numbers are computed independently.** The engine's tests compare against figures
  worked out by hand, outside the engine. A test that recomputes the formula it is testing
  can never fail, so it proves nothing.
- **Behaviour is tested as well as numbers.** "More utilisation never raises unit cost." "A GPU
  that loses cash never pays back." These protect the meaning of the model when formulas are
  later refined.

No test touches the internet; they use small saved copies of real SEC responses.

## 6. Everyday commands

Run from the `ai-economics` folder.

```
uv run scripts/refresh.py              # pull filings, rebuild data, models and site
uv run scripts/refresh.py --dry-run    # show what would happen, do nothing
uv run scripts/export_xlsx.py CRWV     # write models/CRWV.xlsx
uv run scripts/build_site.py           # rebuild the website into site/build/
uv run pytest                          # run every test
```

## 7. Extending it

- **Change an assumption:** edit the CSV, set the status, run `uv run pytest`, commit.
- **Add a company that only needs reported data:** add it to `COMPANIES` in `data/edgar.py`.
- **Add a company model:** copy `companies/coreweave.py`, add `assumptions/<TICKER>.csv`,
  register the class in `companies/__init__.py`.
- **Add a data source:** give it its own module under `data/`, have it write to `data/raw/` and
  `data/processed/`, and keep it ignorant of the models.

## 8. Porting the structure to another industry

Keep: the one-way pipeline, raw/processed separation, the assumptions register, pure-function
unit economics, generated workbook and site, tests with independently computed numbers.

Replace: the data client (another regulator or data vendor), the unit-economics formulas (the
cost stack of an aircraft seat or an oil well instead of a GPU), and the company driver chains.
The folder layout and the rules in section 3 carry over unchanged.
