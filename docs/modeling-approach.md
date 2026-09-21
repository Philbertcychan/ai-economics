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

### First run on CoreWeave, and the first puzzle

Every input below comes from the S-1 and sits in
[assumptions/CRWV.csv](../assumptions/CRWV.csv) with its page and arithmetic.

| Per installed GPU-hour, Q4 2024 | USD | Where it comes from |
|---|---|---|
| Revenue | 1.35 | Q4 revenue / (250,000 GPUs x 2,208 hours) |
| Cash cost | 0.47 | revenue less adjusted EBITDA, same basis |
| Cash margin | 0.88 | a 65% adjusted EBITDA margin |
| Capital charge | 0.99 | $36,586 per GPU, 6 years, 11% |
| **Fully loaded margin** | **-0.11** | |
| Payback | 4.7 years | against about 2.5 disclosed |

On fleet averages, the GPUs do not quite earn an 11% cost of capital, and payback is almost
twice what the company reports. Both cannot be the whole truth. That gap is the first real
analytical question in this project, and working it out is how the model gets better.
Candidate explanations, to be tested against later filings:

1. **Timing.** 250,000 is the year-end count. GPUs installed late in Q4 earned little or no
   revenue in the quarter, so revenue per GPU is understated. The 10-Qs give quarterly
   capacity, which allows average-fleet figures.
2. **Mix.** The fleet average includes older, cheaper-to-rent GPUs. The 2.5 years describes
   committed contracts, which are mostly on newer GPUs at higher prices.
3. **Prepayments.** Customers pay 15% to 25% of contract value up front (p.96). The company
   nets this off the investment; the engine does not yet.
4. **Cost base.** Technology equipment includes networking and storage, not only GPUs, so
   cost per GPU is overstated as a measure of what a GPU contract has to repay.
5. **The company's figure is forward-looking** and rests on its own assumptions.

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
