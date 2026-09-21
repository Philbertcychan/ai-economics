# TODO

Staged plan. The scaffold landed on 2026-09-12; each stage is one working week. Tick items as
they land and add a line to `log.md` per session.

## Setup (before Sept 21)

- [x] Set `EDGAR_USER_AGENT` locally (`Your Name you@example.com`) and as a repository secret of
      the same name. The SEC asks for a real contact, and the scheduled refresh refuses to run
      without the secret.
- [x] GitHub: Settings -> Pages -> Source: GitHub Actions (`refresh.yml` deploys `site/build/`).
      Live at <https://philbertcychan.github.io/ai-economics/>.
- [x] Replace the `<owner>` placeholders with the real GitHub handle: `README.md`,
      `REPO_OWNER` in `scripts/build_site.py`, `REPO_URL` in `scripts/export_xlsx.py`, and
      `site/content/_template.md`.
- [x] Run `uv run scripts/refresh.py --dry-run`, then a real refresh; read
      `data/last_refresh_diff.md` and open `site/build/index.html`.
- [x] Fill the tables in `notes/coreweave-s1.md` from the filing, page numbers included (done
      2026-09-19: every figure was machine-checked against a verbatim snippet on its cited page).
- [ ] Read the CoreWeave S-1 yourself with `notes/coreweave-s1.md` open. Start with its "Read this
      first" section, and confirm the figures the model will rely on against the page.

## Stage 1 — Sept 21–27, 2026: unit economics and CoreWeave

How the work is split: Claude builds the model and explains it; Philbert owns every assumption and
every call. Start with `docs/modeling-approach.md`, then `docs/code-tour.md`.

Done:

- [x] Unit-economics engine (`engine/unit_economics.py`): cost stack, rental and token revenue,
      margin, payback, with tests.
- [x] Assumptions register (`assumptions/`, `companies/assumptions.py`) and CoreWeave's first
      register from the S-1.

Philbert's decisions:

- [ ] Read `docs/modeling-approach.md`. Ask about anything that does not make sense.
- [ ] Open `assumptions/CRWV.csv` in Excel. For each row set `status` to `confirmed`, or change the
      value and set `overridden`. The two that matter most: `depreciation_years` (6, accounting
      life, against a plausible economic life of 4 to 5) and `financing_rate` (11%, debt only).
- [ ] Which debt measure the CoreWeave model uses: the Q2 2026 10-Q has no `us-gaap:LongTermDebt`
      fact (the reported series stops at 2026Q1); the only current element is
      `DebtInstrumentCarryingAmount`, which is gross of discounts and issuance costs.

Claude's build list, in order:

- [ ] Explain the payback gap: the engine gives 4.7 years on fleet averages against about 2.5
      disclosed (see "First run on CoreWeave" in `docs/modeling-approach.md`). Needs quarterly
      capacity figures from the 10-Qs and 10-K.
- [ ] Extract quarterly KPIs from CoreWeave's 10-Qs and 10-K (active power, contracted power,
      backlog, GPUs, capex guidance), quote-verified like the S-1 notes.
- [ ] CoreWeave operating model v0 in `companies/coreweave.py`: capacity, revenue, cost, capex,
      financing, cash, calibrated to reported history; workbook in `models/CRWV.xlsx`.
- [ ] Signals ledger v0: contracts, build-outs, energy, statements, rental prices, each entry
      sourced, dated, rated for confidence and mapped to an assumption.
- [ ] External sources for the inputs the S-1 cannot give: electricity price, data-centre lease
      cost per kW, rental prices by GPU generation, token throughput and prices.
- [ ] First writeup with a Position and a falsifier; first row in `calls.md`.

## Stage 2 — Sept 28 – Oct 4, 2026: second neocloud and the supply side

- [ ] Nebius drivers in `companies/nebius.py` (foreign private issuer: 20-F / 6-K cadence, USD and
      RUB units).
- [ ] Hyperscaler capex lines (MSFT, GOOGL, AMZN, META) from reported facts.
- [ ] Dashboard: side-by-side company comparison.

## Stage 3 — Oct 5–11, 2026

- [ ] Supply against demand: reconcile the capacity being built across the three layers with the
      revenue being guided to; industry writeup with a call in `calls.md`.

## Stage 4 — Oct 12–18, 2026

- [ ] Close the loop: grade the open rows in `calls.md` against reported numbers, revise drivers
      with any new filings, and write the synthesis.
