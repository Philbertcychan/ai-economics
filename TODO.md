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

- [x] Read `docs/modeling-approach.md` (2026-09-21).
- [ ] Open `assumptions/CRWV.csv` in Excel. For each row set `status` to `confirmed`, or change the
      value and set `overridden`. The Sensitivities sheet in `models/CRWV.xlsx` ranks what
      matters: for the cash floor, `debt_share_of_capex`, `mw_added_per_quarter`,
      `capex_per_mw_usd_m` and `receivables_share_of_quarterly_revenue`; for payback per GPU,
      `revenue_per_mw_year_usd_m`, `adjusted_ebitda_margin` and `prepayment_share_of_tcv`.
      Two rows where the evidence disagrees with itself and the choice is yours:
      `prepayment_share_of_tcv` (S-1 says 15-25%, cash since the IPO says about 8%; set to 10%)
      and `cost_of_debt` (expensed 7.4% versus contractual 9-10%; set to 7.5%).
- [ ] Which debt measure the CoreWeave model uses: the Q2 2026 10-Q has no `us-gaap:LongTermDebt`
      fact (the reported series stops at 2026Q1); the only current element is
      `DebtInstrumentCarryingAmount`, which is gross of discounts and issuance costs. The model
      now uses the gross principal (`debt_principal`), which is what the maturity ladder and
      the interest cost are measured on; the site's long-term debt tile still shows the
      carrying amount. Confirm or change.

Claude's build list, in order:

- [x] Quarterly history for CoreWeave (`companies/coreweave.py` v1): active power, contracted
      power, backlog and adjusted EBITDA read from the earnings releases into
      `data/disclosed/CRWV.csv`; revenue and capex from XBRL; per-MW and per-GPU-hour lines
      for six quarters. Result: payback on the company's own cash flows is 4 to 5 years per MW
      against the 2.5 it discloses per GPU; the guide explains the gap.
- [x] Customer prepayments in the payback line: three definitions side by side (gross, the
      company's net-of-prepayment definition, strict cash timing) against the disclosed 2.5.
- [x] Forecast columns (`E`), ten quarters: contracts signed and capacity going live, revenue per
      MW, EBITDA margin, capex per MW, prepayments and deferred revenue, receivables, net debt
      against capex with the 10-Q maturity ladder refinanced, interest, cash, and the external
      funding required to hold a minimum balance. Reviewed by three independent readers
      (finance logic, code, docs) and revised; base case needs no outside money, debt reaches
      $97bn by 2028 at 3x EBITDA.
- [x] Sensitivities: every register row with a range at its low and high, one at a time, on the
      workbook's Sensitivities sheet (funding required, minimum cash, leverage, payback,
      final-year revenue). Not yet on the site.
- [ ] Sensitivity table on the site (the workbook has it).
- [ ] Prepaid revenue recognised by contract vintage instead of a flat share of the balance
      (contracts signed in 2024 unwind in 2027-28, inside the forecast window).
- [ ] A spend-to-live lag for capex (spending lands one or two quarters before capacity
      goes live) once the ramp is not held flat.
- [ ] Signals ledger v0: contracts, build-outs, energy, statements, rental prices, each entry
      sourced, dated, rated for confidence and mapped to an assumption.
- [ ] External sources for the inputs the filings cannot give: electricity price, data-centre
      lease cost per kW, rental prices by GPU generation, token throughput and prices.
- [ ] Public dashboard with Philbert's signed-off assumptions as the default and toggles for
      readers to change them (Philbert's request, 2026-09-21). Needs the engine mirrored in the
      browser and checked against Python with shared test cases; after the forecast exists.
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
