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

The unit that connects them is the **GPU-hour**. It is the owner's product, the lab's raw
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

### What CoreWeave's own numbers say, quarter by quarter

Code: [companies/coreweave.py](../companies/coreweave.py). The first version of the company
model is a HISTORY, not a forecast: one column per reported quarter, built from three sources
that are all visible on the workbook. Revenue and capex come from SEC's structured data.
Active power, contracted power, revenue backlog and adjusted EBITDA come from the company's
own press releases, read into [data/disclosed/CRWV.csv](../data/disclosed/CRWV.csv) with a
link to each source. The assumptions register supplies what neither gives.

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

**Per installed GPU-hour** converts MW to GPUs with one assumption (kW per GPU) and runs the
shared engine, so CoreWeave can be compared with anything else built on it. Revenue per
installed GPU-hour has drifted from about $1.70 to about $1.36, and the fully loaded margin
turned negative in the second half of 2025.

Three things a reader should take from this table.

1. **Revenue per MW is falling, from about $10m to about $8m a year.** Either newer capacity
   earns less per MW (denser GPUs need more power per dollar of rent, or new sites take a
   quarter to fill), or the fleet is filling more slowly than it is being built. The backlog,
   at 10 to 12 years of current revenue, says the demand is contracted. The gap is timing.
2. **Payback on the company's own cash flows is 4 to 5 years, against the 2.5 it discloses.**
   The disclosed figure is per GPU, on committed contracts, net of customer prepayments of 15%
   to 25% of contract value. The model's figure is per MW of everything: GPUs, networking,
   storage, and the capex for capacity that is not yet earning. Both are right about different
   things. The prepayment effect alone explains a large part of the gap, and adding it to the
   model is the next refinement.
3. **Capex per MW added swings between $21m and $35m** because capex lands before the MW it
   buys goes live. Cumulative figures smooth this, but a forecast needs an explicit lag between
   spending and capacity, which is why the forecast version starts from the capacity ramp.

The habit to take from this: compute the same quantity two ways, from the bottom up and from
what the company discloses. Where they disagree, there is something to learn.

## 4. Layer 2: a company is a fleet over time

Code: `companies/`. One module per company, all with the same four steps: load data, build,
produce tables, export to Excel.

A company model is a chain of **drivers**, each computed from the one before, one column per
period:

```
power online (MW)  ->  GPUs installed  ->  GPU-hours available
        x share rented, x price per hour          ->  revenue
        less energy, leases, staff                ->  cash profit (EBITDA)
        less depreciation, interest               ->  profit
capex for next period's GPUs                      ->  cash needed
debt drawn against signed contracts, repayments   ->  cash balance and debt
```

Things worth knowing about this kind of business:

- **Capacity leads revenue.** Power and GPUs are paid for before they earn. In a fast-growing
  fleet, this quarter's costs include GPUs that will only earn next quarter.
- **Contracts, not spot prices, set revenue.** 96% of CoreWeave's 2024 revenue came from
  committed take-or-pay contracts averaging four years. The backlog of signed contracts
  (remaining performance obligations, $15.1bn at year-end 2024) says more about the next two
  years than any price forecast.
- **Financing is part of the product.** GPUs are bought with loans drawn against specific
  customer contracts, at rates set by the customer's credit quality. A model that ignores the
  debt schedule misses the main way this business can fail.
- **Concentration.** One customer was 62% of 2024 revenue. A driver-based model lets that
  customer be switched off to see what breaks.

**Actuals and estimates.** Periods that have been reported are marked `A` and come from filings.
Future periods are marked `E` and come from drivers. The first test of any model is
**calibration**: run the drivers over the reported periods and check they reproduce reported
revenue, capex and cash within a small error. A model that cannot explain the past has no
business forecasting.

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
