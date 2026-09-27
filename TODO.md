# TODO

The only tracker. Tick items as they land and add a line to `log.md` per session.

## The spine and the three tracks (agreed 2026-09-22)

The project is built around one spine: the AI compute value chain, bottom-up, from power
generation and fuel to the applications that consume tokens. Each stage is described the same
way (unit, cost, price, capacity, lead time, players, bottleneck) in `stack/`. Three products
sit on that spine:

- **Fluency** (recruiting): one studied page per stage with the numbers people who follow the
  space know, each with its source.
- **Bottlenecks** (investing): demand versus supply per stage in physical units with lead
  times; prices as confirmation; listed players as the watch list; `calls.md` as the record.
  The standard: the 2025 NAND move (Sandisk) should have been spottable from the memory page.
- **Understanding** (where to build): how each stage works, who does what, and where a
  finance-and-code skill set can contribute.

The company models (`companies/`) are the top-down calibration of the GPU-hour and token
stages and continue in parallel.

How the work is split: Claude builds the model and explains it; Philbert owns every assumption,
every bottleneck score and every call.

## Setup (done)

- [x] Repo, CI, daily refresh, Pages, `EDGAR_USER_AGENT`, CoreWeave S-1 notes.

## Stage 1 (done 2026-09-22): engine, register, CoreWeave

- [x] Unit-economics engine; assumptions register; CoreWeave history and ten-quarter forecast
      with prepayments, three payback definitions and a sensitivity sheet; reviewed and revised.

Philbert's open decisions from Stage 1:

- [ ] `assumptions/CRWV.csv`: set each row's `status`. The Sensitivities sheet in
      `models/CRWV.xlsx` ranks what matters. Two rows where the evidence disagrees with itself:
      `prepayment_share_of_tcv` (S-1 says 15-25%, cash since the IPO says about 8%; set to 10%)
      and `cost_of_debt` (expensed 7.4% versus contractual 9-10%; set to 7.5%).
- [ ] Debt measure: the model uses gross principal (`debt_principal`); the site tile shows the
      carrying amount. Confirm or change.

## Stage 2 — Sept 28 to Oct 4, 2026: the stack skeleton, and Nebius

Track: all three (the skeleton serves fluency first).

- [x] Stack skeleton across all nine stages: `stack/stages.csv` (units, what each sells, lead
      time), `stack/metrics.csv` (6 to 12 sourced figures per stage), `stack/players.csv`,
      `stack/conversions.csv` (how one stage's unit becomes the next), `stack/consumption_tiers.csv`
      (applications split by tokens consumed), one primer per stage in `stack/primers/`, and the
      site pages `stack/index.html` and `stack/<stage>.html`. Done 2026-09-27: all nine stages have
      sourced figures, players and conversions; the researchers' open questions (what no public
      source states: CoWoS capacity, GPU list prices, tokens per user) are in the session notes.
- [ ] Philbert: score each stage's `bottleneck_score` (1 to 5) in `stack/stages.csv` from the
      evidence on its page, and write the one-line `bottleneck_note`. These are calls.
- [x] Nebius model in `companies/nebius.py` on the CoreWeave pattern (foreign private issuer:
      20-F and 6-K cadence; every quarterly figure from the 6-K results exhibits, quote-checked
      into `data/disclosed/NBIS.csv`; ARR-driven forecast; prepayments as a share of capex).
- [ ] Philbert: review `assumptions/NBIS.csv` (17 proposed rows); the two that matter most are
      `active_power_mw_2026q2` (inferred, not disclosed) and `arr_per_new_mw_year_usd_m`.
- [ ] Nebius: a back-loaded 2026 ramp (or a go-live lag) so the model can hold both the ARR
      and the revenue guidance at once; per-GPU lines once kW per GPU or GPU counts are disclosed.
- [ ] A spend-to-live lag for capex in the CoreWeave forecast.
- [ ] Prepaid revenue recognised by contract vintage in the CoreWeave forecast.

## Stage 3 — Oct 5 to 11, 2026: power, deep

Track: bottlenecks, with fluency as the by-product. US first.

- [x] Power supply dataset: EIA-860M (operating, planned and retired generators by fuel, status
      and date), `data/eia.py` and `scripts/pull_eia860m.py`, cached and tidied under `data/`
      with summaries by fuel, year and state (2026-09-27). Still to do: run it from
      `scripts/refresh.py` on a monthly cadence and show the planned-additions table on the
      power stage page.
- [ ] Interconnection queues: ERCOT, PJM, MISO, SPP, CAISO public queue files; years-to-connect
      by ISO; large-load requests where published.
- [ ] Demand side: announced data-centre campuses in the US with MW, sponsor, power source and
      status, each with a source; start from the nine campuses the data-centre research recorded
      on 2026-09-26 (Stargate Abilene, Hyperion, Colossus, Fairwater, Rainier and others; in the
      session scratchpad `stack/out/datacenter.json` under `campuses`, to be moved into `stack/`); the GW pipeline against firm supply additions by year.
- [ ] Site map with a likelihood rubric per campus (power source secured, queue position, gas
      access, water, permits, sponsor balance sheet), rendered on the site. What SemiAnalysis
      adds beyond this (satellite imagery, permit scraping) is out of reach; say so on the page.
- [ ] Gas turbine order books and lead times; nuclear restarts; the price of firm power.
- [ ] Bottleneck verdict on power and grid, written by Philbert, with a falsifier in `calls.md`.

## Stage 4 — Oct 12 to 18, 2026: memory, deep; first calls

Track: bottlenecks.

- [ ] Memory dataset: HBM capacity by supplier, HBM stacks and GB per GPU by generation, DRAM
      and NAND contract prices (TrendForce releases), capex by supplier, enterprise SSD demand.
- [ ] Bottom-up demand for HBM and NAND from GPU shipments and server content; supply from
      announced capacity; the Sandisk test written up: what was visible when.
- [ ] Every company as a source of tokens or capacity: extend `companies/` beyond the neoclouds
      to the hyperscalers (capex, GPU-hours implied) and the labs (tokens, revenue run-rate).
- [ ] First writeup with a Position and a falsifier; first rows in `calls.md`.

## Later

- [ ] Signals ledger: contracts, build-outs, energy, statements, rental prices; each entry
      sourced, dated, rated for confidence and mapped to a stage or an assumption.
- [ ] Sensitivity table on the site (the workbook has it).
- [ ] Public dashboard with Philbert's signed-off assumptions as the default and toggles for
      readers (needs the engine mirrored in the browser and checked against Python).
- [ ] Data integrations beyond public sources, kept behind the `data/` package pattern so a
      provider can be added without touching the models: earnings-call transcripts, GPU rental
      price trackers, and near the end Philbert's Questrade account (its MCP: positions, prices,
      a watch list of the listed players per stage).
