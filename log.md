# Log

One line per working session: date, what landed, what is next. Newest at the bottom.

2026-09-12 — scaffold: layout, engine stubs + tests, EDGAR client, xlsx exporter, site builder, refresh pipeline, CI. Next: engine logic.
2026-09-17 — scaffold reviewed and hardened (quarterly cash-flow series from YTD facts, tag selection by recency, exporter and site guards); first live refresh: 7 companies, 726 filings indexed, 90 documents. Next: setup list in TODO.md, then engine logic.
2026-09-19 — setup finished: repo public, Pages live, EDGAR_USER_AGENT set locally and as a secret, first scheduled-style refresh deployed; CoreWeave S-1 notes filled with page references. Next: read the S-1 against the notes, then engine logic.
2026-09-21 — new split: Claude builds and explains the model, Philbert owns assumptions and calls. Engine v1 (annuity capital charge, cost stack, rental and token modes), assumptions register, CoreWeave register v0 from the S-1, two guides in docs/, executive site redesign. First result: fleet-average payback 4.7 years against about 2.5 disclosed. Next: quarterly KPIs from the 10-Qs, then the CoreWeave operating model.
2026-09-21 (2) — CoreWeave quarterly history: earnings-release KPIs (active power, contracted power, backlog, adjusted EBITDA) pulled from 8-K exhibits into data/disclosed/, six quarters per MW and per GPU-hour, workbook formulas re-checked against Python for every column. Payback per MW 4 to 5 years vs 2.5 disclosed per GPU. Next: prepayments in payback, then forecast columns.

