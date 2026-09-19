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

- [ ] Engine logic in `engine/unit_economics.py` (`cost_per_m_tokens`, `margin_per_gpu_hour`,
      `payback_months`) until `uv run pytest tests/test_unit_economics.py --runxfail` passes. The
      functions can land one at a time: each test's `xfail` switches itself off once the functions
      it calls are written, so CI stays green in between.
- [ ] Housekeeping once all three are written: delete the `pending` / `_is_stub` helper and its
      decorators in `tests/test_unit_economics.py` (dead code by then, nothing turns red if it
      stays).
- [ ] Decide which debt measure the CoreWeave model uses: the Q2 2026 10-Q has no
      `us-gaap:LongTermDebt` fact (the reported series stops at 2026Q1); the only current element
      is `DebtInstrumentCarryingAmount`, which is gross of discounts and issuance costs.
- [ ] CoreWeave drivers in `companies/coreweave.py`: `build()` fills `self.drivers` and
      `self.outputs` in finance layout (optional `attrs["formulas"]`); `engine_defaults` with
      sourced values.
- [ ] First export: `uv run scripts/export_xlsx.py CRWV`; open `models/CRWV.xlsx` and check the
      blue/black/green/grey convention and the `in_<name>` defined names.
- [ ] First writeup from `site/content/_template.md` (`status: published`) with a Position and a
      falsifier; copy the falsifier row into `calls.md`.

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
