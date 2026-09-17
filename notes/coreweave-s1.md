# CoreWeave S-1: reading notes

Working notes for `companies/coreweave.py`. Fill every cell from the filing itself and give a page
reference so it can be checked; leave a cell empty rather than paste a number from a secondary
source. Page numbers refer to the S-1 unless marked `424B4 p.`.

## Sources

- S-1, filed 2025-03-03, accession 0001193125-25-044231:
  https://www.sec.gov/Archives/edgar/data/1769628/000119312525044231/d899798ds1.htm
- Final prospectus (424B4), filed 2025-03-31, accession 0001193125-25-067651:
  https://www.sec.gov/Archives/edgar/data/1769628/000119312525067651/
- Everything filed since (S-1/A, 10-K, 10-Q): `uv run scripts/refresh.py --tickers CRWV --no-download`
  lists them in `data/processed/CRWV/filings.csv` with EDGAR links.

## Business model

Questions to answer from the text: what is sold and on what terms (committed contracts versus
on-demand, contract length, prepayments, how revenue is recognised); who the customers are, as far
as the filing discloses concentration; how capacity is sourced (owned versus leased data centres,
power); how GPU purchases are financed; and what unit of capacity the company itself reports in.

Notes:

-

## KPIs disclosed

Only KPIs the filing itself defines. Quote the definition as stated; do not paraphrase.

| KPI | definition as stated | value | period | page |
|---|---|---|---|---|
|  |  |  |  |  |
|  |  |  |  |  |
|  |  |  |  |  |
|  |  |  |  |  |

## Capacity figures

| item | value | as of | page |
|---|---|---|---|
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |

## Financing structure

One row per facility or instrument as described in the filing.

| facility | size | rate | collateral | maturity | page |
|---|---|---|---|---|---|
|  |  |  |  |  |  |
|  |  |  |  |  |  |
|  |  |  |  |  |  |
|  |  |  |  |  |  |

## Risk factors worth modelling

Keep only risks that change a driver, a rate or a timing in the model; skip boilerplate.

| risk | how it enters the model | page |
|---|---|---|
|  |  |  |
|  |  |  |
|  |  |  |
|  |  |  |

## Open questions

What the filing does not answer, to be resolved from later 10-Q/10-K filings or earnings calls
before the corresponding driver is treated as more than a placeholder.

-
