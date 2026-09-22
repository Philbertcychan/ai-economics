# How this model thinks

A guide to the financial reasoning in this repository, written so the same approach can be
reused on another industry. The companion guide, [code-tour.md](code-tour.md), explains how the
program is organised.

## 1. What a model is

A financial model is arithmetic applied to assumptions.

- The **arithmetic** is mechanical and anyone can check it: revenue is volume times price,
  cash falls when capex exceeds what the business earns.
- The **assumptions** are where judgement lives: what price, how much volume, how long an
  asset lasts. A model is only as good as these, so they are kept in one place
  (`assumptions/`), each with a source, a range and a sign-off status.

A good model is an argument that can be checked. It says: if these few things are true, this is
what the company earns. The useful outputs are rarely the forecast itself. They are which
assumptions matter most, and what would have to be true for the market's view to be right.

Every model here answers three questions in order:

1. What drives revenue? Find the physical thing being sold and count it.
2. What does it cost to supply? Separate what is paid every period from what is paid once.
3. Where does the cash go? Growth businesses consume cash before they earn it, so financing
   decides who survives.

## 2. The shape of this industry

Four kinds of company share each dollar spent on AI compute:

| Layer | Who | What they sell |
|---|---|---|
| Chip makers | Nvidia (made by TSMC, with memory from the HBM suppliers) | the GPU |
| GPU owners: neoclouds | CoreWeave, Nebius | GPU-hours, rented under contract |
| GPU owners: hyperscalers | Microsoft, Alphabet, Amazon, Meta | the same, plus their own use |
| AI labs and applications | OpenAI, Anthropic and their customers | tokens, which need GPU-hours to make |

The unit that connects them is the **GPU-hour**, one graphics processor running for one hour. It is the owner's product, the lab's raw
material, and the thing a chip exists to produce. So the model is built on it.

## 3. Layer 1: the economics of one GPU

Code: [engine/unit_economics.py](../engine/unit_economics.py). Before modelling a company,
model a single GPU. A company is then a fleet of these.

### The cost of a GPU-hour

An hour means a wall-clock hour of an installed GPU, 8,760 a year, busy or idle. Costs do not
stop when the GPU is idle, so idle time shows up as lower revenue, never as fewer hours.

| Layer of cost | What it is | How it is computed |
|---|---|---|
| Capital | the purchase price, spread over the GPU's life, including the cost of money | price x capital recovery factor / 8,760 |
| Energy | electricity for the GPU and its cooling | kW x PUE x price per kWh x load factor |
| Facility | data-centre space, rented by the kW | price per kW-month x kW x 12 / 8,760 |
| Other | staff, maintenance, software, network | an amount per GPU-hour |

- **Cash cost** is energy + facility + other: what goes out of the door every hour.
- **Fully loaded cost** adds capital.

These answer different questions. "Does it cover its running costs?" is the cash view, and it
decides whether an existing GPU keeps running. "Was it worth buying?" is the fully loaded view,
and it decides whether the next GPU gets bought.

### Why capital is charged as an annuity

Spreading a purchase over its life is the most consequential convention in the engine. Two
options, for a $32,000 GPU, 5 years, 8% cost of money:

| Convention | Annual charge | Logic |
|---|---|---|
| Depreciation + interest on the full price | 20% + 8% = 28.0% | interest never falls |
| Annuity (used here) | 25.0% | a level payment that repays the price with interest, like a mortgage |

The annuity is right because interest is owed only on what is still outstanding, and that falls
as the GPU earns its cost back. The share of the price due each year is the *capital recovery
factor*: `rate / (1 - (1 + rate) ** -years)`. At a rate of zero it becomes plain straight-line
depreciation. This is the same method used to compare power plants ("levelized cost").

Two inputs dominate this charge: the **life** of the GPU and the **cost of money**. Both are
contested for GPUs. Accounting life is six years at CoreWeave; whether a GPU still earns a good
rent in year five, after two newer generations, is an open question. That is why
`depreciation_years` carries a low end of 4 in the register.

### Two ways to earn revenue from the same hour

- **Rental** (`mode="rental"`): the owner sells the hour. Revenue per installed hour is the
  hourly price times the share of hours rented.
- **Tokens** (`mode="tokens"`): the lab sells what the hour produces. Revenue is tokens per
  hour times the price per million.

Same cost stack under both. Comparing them shows how the dollar splits between the owner and
the lab.

### Payback

Months until the GPU's cash margin has returned its purchase price. It is defined the way
operators quote it, before financing and depreciation, so that it can be compared with
disclosed figures. CoreWeave's S-1 (p.97) gives about 2.5 years, measured through adjusted
EBITDA per GPU and net of customer prepayments.

A note on rates: the capital charge above uses the register's `financing_rate` (11%, what the
GPUs already bought cost to finance), while the forecast's interest line uses `cost_of_debt`
(7.5%, what the next dollar of debt costs as expensed). The register explains both.

### What CoreWeave's own numbers say, quarter by quarter

Code: [companies/coreweave.py](../companies/coreweave.py). The model's actual columns are a
history built from three sources. Revenue, capex, cash from operations, cash, receivables,
debt, interest and deferred revenue come from SEC's structured data. Active power, contracted
power, revenue backlog and adjusted EBITDA come from the company's own press releases, read
into [data/disclosed/CRWV.csv](../data/disclosed/CRWV.csv) with a link to each source and
shown on the workbook's Disclosed sheet. The assumptions register supplies what neither gives.

Glossary for these tables: *adjusted EBITDA* is profit before interest, tax, depreciation and
stock compensation, the company's own measure of cash operating profit; *annualised* means a
quarter's figure times four; *take-or-pay* means the customer pays for the committed capacity
whether or not it uses it; *DDTL* is a delayed-draw term loan, a loan drawn in pieces as GPUs
are bought.

The model measures the business two ways.

**Per megawatt of active power** uses no assumptions at all. Capacity is averaged over the
quarter (opening and closing MW), because revenue is earned on the fleet that was running, not
the fleet at quarter end:

| Annualised, per MW of average active power | 2025Q1 | 2025Q2 | 2025Q3 | 2025Q4 | 2026Q1 | 2026Q2 |
|---|---|---|---|---|---|---|
| Revenue, $m | 10.1 | 10.9 | 10.3 | 8.7 | 9.0 | 8.2 |
| Adjusted EBITDA, $m | 6.2 | 6.8 | 6.3 | 5.0 | 5.0 | 4.8 |
| Capex per MW added, cumulative, $m | 23.4 | 35.1 | 27.2 | 21.0 | 28.1 | 21.4 |
| Payback per MW, years | 3.8 | 5.2 | 4.3 | 4.2 | 5.6 | 4.4 |

**Per installed GPU-hour** converts MW to GPUs with one assumption (1.44 kW per GPU) and runs
the shared engine, so CoreWeave can be compared with anything else built on it. Revenue per
installed GPU-hour has drifted from about $1.70 to about $1.36, and the fully loaded margin
turned negative in the second half of 2025.

Three things a reader should take from this.

1. **Revenue per MW is falling, from about $10m to about $8m a year.** Either newer capacity
   earns less per MW (denser GPUs need more power per dollar of rent, or new sites take a
   quarter to fill), or the fleet is filling more slowly than it is being built. The backlog,
   at 10 years of current revenue, says the demand is contracted. The gap is timing.
2. **Payback on the company's own cash flows is 3.8 to 5.6 years per MW, against the 2.5
   years it discloses per GPU.** Three definitions of payback per GPU sit side by side on the
   Outputs sheet:

   | Payback per GPU, years | 2025Q1 | 2026Q2 | What it assumes |
   |---|---|---|---|
   | Gross of prepayment | 4.0 | 5.2 | price / annual cash margin |
   | Net of prepayment, the company's definition | 3.4 | 4.6 | (price - prepayment) / annual cash margin |
   | Strict cash timing | 3.4 | 5.2 | prepayment at signing, credited against the final months of the contract |
   | Disclosed | 2.5 | 2.5 | committed contracts only, at signing |

   The S-1 says prepayments are generally credited against the final months of a contract,
   so until that window the GPU bills its full revenue. The company's definition is therefore
   the true cash payback whenever the GPU pays back before the window, as it did on the early
   2025 fleet; it flatters only when payback runs past the window, as it does on 2026's
   figures, where the prepayment has fully unwound by the time the GPU pays back. At the
   register's 10% prepayment share (the cash evidence since the IPO) the prepayment closes a
   quarter of the gap to the disclosed figure; at the S-1's 20% it closes half. The rest is
   mix and cost base: committed contracts on new GPUs earn more per hour than the fleet
   average, and the $36,586 per GPU of technology equipment includes networking and storage.
   Capex per MW added, the same idea measured from cash flows, is $31k per GPU.
3. **Capex per MW added swings between $13m and $51m a quarter** because capex lands before
   the MW it buys goes live. Cumulative figures smooth this, but a forecast needs an explicit
   lag between spending and capacity, which this model does not yet have.

The habit to take from this: compute the same quantity two ways, from the bottom up and from
what the company discloses. Where they disagree, there is something to learn.

## 4. Layer 2: a company is a fleet over time

Code: `companies/`. One module per company, all with the same four steps: load data, build,
produce tables, export to Excel. The CoreWeave model has six actual quarters (``A`` columns)
and ten forecast quarters (``E`` columns) on the same sheets, laid out the way finance models
are: reported facts hard-coded in grey, everything else computed in black, assumptions in blue.

A company model is a chain of **drivers**, each computed from the one before, one column per
quarter. CoreWeave's chain:

```
contracted MW signed (assumption)  -> contracts signed (rate x term) -> backlog
                                   -> customer prepayments (a share of contracts signed)
MW going live (assumption)         -> active power, averaged over the quarter
                                   -> revenue (revenue per MW-year, assumption)
                                   -> adjusted EBITDA (margin, assumption)
MW going live x capex per MW       -> capex -> net debt raised (a share of capex); maturities refinanced
EBITDA - interest + change in deferred revenue - receivables build + other -> cash from operations
cash + CFO - capex + net debt raised                                       -> cash
shortfall below the minimum cash balance                                   -> external funding
```

Things worth knowing about this kind of business, and where each shows up in the model:

- **Capacity leads revenue.** Power and GPUs are paid for before they earn. The model
  averages capacity over the quarter, and its capex-per-MW figure is cumulative because single
  quarters swing from $13m to $51m per MW as spending lands ahead of go-live.
- **Contracts, not spot prices, set revenue.** 96% of 2024 revenue came from committed
  take-or-pay contracts averaging four years. The backlog ($104bn at June 2026) is 10 years
  of current revenue. Signings, not go-lives, drive the backlog: 300, 600, 700, 200, 400 and
  200 MW of contracted power were added in the six quarters to June 2026, and the forecast
  assumes 400 MW a quarter, each at the prevailing rate for four years. The Drivers sheet
  shows the pipeline of contracted-but-not-active power so a plan that activates more than it
  signed is visible.
- **Customers prepay.** Deferred revenue was $9.7bn at June 2026, more than a quarter of the
  debt, and it is generally credited only in the final months of each contract. Prepayments
  are a real funding source in the forecast: 10% of the value of contracts signed, with 3% of
  the balance recognised as revenue each quarter. Both figures are contested and the register
  says why; the sensitivity sheet shows what each is worth.
- **Financing is part of the product.** GPUs are bought with loans drawn against specific
  customer contracts. Debt raised net of repayments was 82% of capex in both 2025 and the
  first half of 2026, and the model applies that share net: maturities from the 10-Q's
  ladder are refinanced, and principal grows by 80% of capex. Interest follows the balance at
  the rate the company expenses (7.5%); the higher contractual rates on the newest facilities
  are partly capitalised into construction and so sit inside capex per MW.
- **Cash conversion is receivables.** Over six quarters, cash from operations was $2.4bn
  below what EBITDA, interest and prepayments explain, and $2.1bn of that was growth in
  receivables: customers invoiced but not yet paid, prepayments included. The model carries
  receivables at one quarter of revenue, so the drag grows with revenue and fades as growth
  slows, and a residual 3% of revenue covers taxes and the rest.

**Actuals and estimates.** Reported quarters come from filings and are marked `A`. Future
quarters come from the drivers and are marked `E`. In actual columns the cash, debt and
deferred-revenue balances are reported facts, and the flows on the sheet do not fully explain
their movements (non-cash debt additions, rounding of disclosed balances, acquisitions). Three
"other movements" rows carry the difference so that every roll-forward on the sheet ties, and
the size of those rows is itself information: $7.6bn of debt movements over six quarters
that the cash-flow statement does not show as proceeds.

### The base case, and what to read from it

With the proposed assumptions (400 MW signed and 350 MW going live a quarter, $9m revenue per
MW-year, a 58% EBITDA margin, $22m capex per MW, 80% of capex funded by net new debt at 7.5%,
prepayments at 10% of contracts signed), the model gives:

| | 2025A | 2026E (H1 actual) | 2027E | 2028E |
|---|---|---|---|---|
| Revenue, $bn | 5.1 | 13.0 | 26.1 | 38.8 |
| Adjusted EBITDA, $bn | 3.1 | 7.5 | 15.2 | 22.5 |
| Capex, $bn | 10.3 | 29.5 | 30.8 | 30.8 |
| Prepayments received, $bn | 5.0 | 4.8 | 5.8 | 5.8 |
| Cash from operations, $bn | 3.1 | 7.0 | 10.8 | 15.5 |
| Free cash flow, $bn | -7.3 | -22.6 | -20.0 | -15.3 |
| Debt raised net of repaid, $bn | 8.4 | 23.8 | 24.6 | 24.6 |
| Active power at year end, GW | 0.85 | 2.2 | 3.6 | 5.0 |
| Contracted power at year end, GW | 3.1 | 4.5 | 6.1 | 7.7 |
| Debt principal at year end, $bn | 21.6 | 47.9 | 72.5 | 97.2 |
| Cash at year end, $bn | 3.1 | 5.7 | 10.4 | 19.7 |
| Net debt / annualised EBITDA at year end | 5.1x | 4.0x | 3.4x | 3.0x |

Four readings:

1. **The business never funds itself in this plan.** Free cash flow stays between -$15bn and
   -$23bn a year because every dollar of EBITDA and more goes into the next tranche of
   capacity. That is a choice, not a flaw, as long as the contracts behind the capacity are real.
2. **Debt is the plan.** Principal grows from $36bn to $97bn by the end of 2028 while
   leverage falls from 5x to 3x, because EBITDA grows faster than debt. The cash line
   accumulates to $20bn because the model draws 80% of capex mechanically; a real treasurer
   would draw less. The plan needs no outside money in the base case.
3. **What the cash floor actually depends on.** The Sensitivities sheet moves each assumption
   to the edge of its range with the rest held. Ranked by how low cash goes: the debt share
   of capex (50% needs $4.8bn of outside money), building faster (600 MW a quarter), capex
   per MW ($35m), receivables at two quarters of revenue, then signings and contract terms.
   The cost of debt and the EBITDA margin barely move it. Payback per GPU moves with only
   the revenue rate, the margin and the contract terms.
4. **Prepayments matter less than the S-1 suggested.** At 10% of contracts signed they bring
   $5.8bn a year in 2027, a fifth of the net debt raised; at the S-1's 20% they would bring
   twice that and take a year off the per-GPU payback.

The way to use this is not to believe the base case. It is to change one assumption in the
register and see what breaks: the cash floor, the leverage, or the payback.

## 5. Layer 3: does supply match demand?

Add up what the owners say they will spend (capex guidance), convert it to GPUs and megawatts,
and compare it with what the supply chain can deliver (chip output, advanced packaging, memory,
power connections) and with the revenue the labs would need to earn to justify it. The gap, in
either direction, is the analysis. This layer comes after two company models exist.

## 6. Assumptions are yours

Each row in `assumptions/<TICKER>.csv` has:

- a **basis**: `disclosed` (in a filing), `derived` (computed from disclosed figures, arithmetic
  shown), `external` (third-party source, linked) or `judgment` (a reasoned choice);
- a **range** (`low`, `high`) where the value is uncertain;
- a **status**: `proposed` until you change it to `confirmed` or `overridden`.

The model runs on proposed values so that work is never blocked, and the workbook prints each
status so a reader can see what has been signed off. To take ownership of a number, open the
CSV in Excel, change the value if you disagree, and set the status.

The way to find out which assumptions deserve your time is **sensitivity**: move one input
across its range, hold the rest, and see how far the answer moves. Most inputs barely matter.
Two or three decide everything, and those are the ones to research and to write about.

## 7. Evidence beyond filings

Filings are complete and reliable but slow and backward-looking. The things that move these
companies between filings are contracts, construction, power and policy. The plan is a
**signals ledger**: a dated, sourced record of such events, each mapped to the assumption it
bears on.

| Kind of signal | Example | Assumption it bears on |
|---|---|---|
| Contract | a lab commits $X bn to an owner over N years | backlog, revenue per year, customer concentration |
| Build-out | a site goes from announcement to power in M months | how fast capacity comes online |
| Energy | a utility's connection queue, a power-purchase price | energy cost, timing of capacity |
| Statement | a chief executive or a government on supply, demand, tariffs or export rules | GPU price and availability, demand |
| Price | quoted rental prices per GPU-hour | price per GPU-hour |

Rules for the ledger: every entry has a source link, a date, and a confidence level
(`confirmed`, `reported`, `speculated`). A signal never changes the model directly. It prompts
a change to an assumption, which is logged with the reason. That keeps rumours from leaking
into numbers unexamined.

## 8. Calls

A writeup ends with a position and the number that would prove it wrong by a stated date, and
both go in `calls.md`. A view that cannot be wrong is not a view.

## 9. Reusing this on another industry

1. Find the **unit** the industry sells and that its assets produce: a GPU-hour, a seat-mile,
   a barrel, a subscriber-month.
2. Build the **unit economics**: cost stack, price, margin, payback. Keep it to arithmetic.
3. Model a company as **capacity over time** times the unit economics, with financing.
4. **Calibrate** to reported history before forecasting.
5. Keep assumptions in a **register** with sources and ranges, and find the few that matter.
6. Track the **evidence between filings** and map it to assumptions.
7. Commit to **falsifiable calls**.
