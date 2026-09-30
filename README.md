# South Florida Acquisition Screener

A screening tool for Miami-Dade single-family homes, built entirely on Florida
Department of Revenue (DOR) public tax-roll data. It estimates an after-repair
value (ARV) for each parcel from comparable sales, compares that to the
county's assessed value, and ranks parcels by the gap. It runs locally as a
Streamlit app.

The main work is the back-test. I tested whether the ranking and three common
"motivated seller" signals (absentee owner, long ownership tenure, equity)
predict what actually happened to these homes in 2018–2023, using only data
that existed at each valuation date. The short version:

- The ARV engine lands within about 10% median error of actual sale prices
  for 2018–2020, and 15.7% for 2021.
- The spread ranking predicts which sales close well below the estimated ARV,
  with a caveat about self-consistency (below). It does not predict whether a
  home sells at all.
- Absentee ownership predicts nothing usable. Homes with no recorded sale since
  2015 were **less** likely to sell, not more.
- A look-ahead leak in the first version of the ARV engine overstated accuracy.
  It was found, fixed, and verified with a delete-the-future test.

Every number below comes from `backtest_discount.py`,
`verify_lookahead_fix.py`, and `historical_sales.py` as committed.

## Data

**Source.** Florida DOR Name-Address-Legal (NAL) and Sale Data File (SDF)
Final rolls for Miami-Dade, vintages 2016 through 2025. DOR only posts the
current year for download, so the prior years came from a public-records
request. Together these cover sale dates from January 2015 through December
2025, eleven calendar years of sales. No MLS data is used.

**Sales history (`historical_sales.py`).** Each annual SDF covers roughly
18–20 months of sales, so consecutive files overlap and the same sale appears
more than once. I stacked all ten files and deduplicated on parcel, sale year,
sale month, price, and recording reference (OR book/page, or clerk instrument
number). Sale dates are only given to the month, so price alone is not a safe
key. 319 groups had the same recorded instrument but disagreeing fields across
files; those were resolved by keeping the later file. The result is **945,592
distinct sales**, of which 421,426 are qualified arm's-length and 156,810 are
qualified single-family.

**Qualification-code harmonization.** Every sale carries a DOR qualification
code. Only codes `01` and `02` (qualified arm's-length) are used as comps or
outcomes. Before relying on that across eleven years, I checked the code
definitions in three DOR document vintages (effective 2015, 2018, and 2024;
the first two are only available through the Wayback Machine). Codes `01` and
`02` are worded identically in all three. The one change is code `21`
(contract for deed), added in 2018. It is a disqualified code either way, so
the arm's-length set is the same across the whole window.

| Code | Meaning | Used? |
|---|---|---|
| `01`, `02` | Qualified arm's length | Yes |
| `03`–`06` | Arm's length but excluded from DOR's ratio study (characteristics changed, multi-parcel, etc.) | No |
| `11`–`21` | Disqualified by deed type (quitclaim, tax deed, foreclosure-related, etc.) | No |
| `30`–`43` | Disqualified by evidence (related party, forced sale, atypical financing, etc.) | No |
| `98`–`99` | Pending | No |

Sources: [2015 vintage](https://web.archive.org/web/20180426230727/http://floridarevenue.com/property/Documents/salequalcodes_bef01012016.pdf),
[2018 vintage](https://web.archive.org/web/20210621225107/https://floridarevenue.com/property/Documents/salequalcodes_bef01012019.pdf),
[2024 vintage](https://floridarevenue.com/property/Documents/salequalcodes_bef01012025.pdf).

A separate `MULTI_PAR_SAL` flag can be set on a qualified sale. It means the
price covers a bundle of parcels. 287 qualified sales (0.07%) carry it, and
they are excluded from comps.

**Parcel snapshots.** For each vintage I also kept every single-family
parcel's living area, year built, neighborhood code, assessed ("just") value,
and owner mailing address. That makes it possible to describe a parcel as it
was in a given year rather than as it is today.

**Parcel ID stability.** 92.5% of parcel IDs appear in every vintage. Of the
IDs that disappear between years, 14.4% reappear under a new ID with the same
owner and address (splits, combinations, or renumbering). These are flagged
but not relinked.

## ARV method (`historical_arv.py`)

The ARV is computed **as of** a date D and uses nothing dated on or after D.

- **Comps:** qualified, non-multi-parcel single-family sales in the same DOR
  neighborhood code, within ±20% living area and ±15 years built, sold in the
  36 months before D. The parcel's own sales are excluded.
- **Same-vintage characteristics:** each comp's living area and year built
  come from the roll for the year it sold, not today's roll. A house that was
  1,400 sq ft when it sold in 2017 and was expanded to 2,200 in 2022 is
  compared as a 1,400 sq ft house. Of 156,779 comp-eligible single-family
  sales, 129,849 could be matched to a same-year snapshot.
- **Subject characteristics:** for D = January 1 of year Y, the subject is
  described by the Y−1 roll, because the year-Y roll is not certified until
  that fall.
- **Time adjustment:** comp prices per square foot are adjusted to D with a
  county-wide monthly index. The index is rebuilt for every D from sales
  before D only, and held flat after the last complete year rather than
  extrapolated.
- **Estimate:** median adjusted $/sq ft after trimming outliers beyond 1.5×
  IQR, times the subject's living area. If restricting comps to the subject's
  own assessed-value tercile within the neighborhood gives a tighter comp set,
  that set is used. Fewer than 5 comps means no ARV.
- **Confidence gate:** an ARV is "high confidence" if it has at least 8 comps
  and comp dispersion (IQR ÷ median $/sq ft) of 30% or less.

`spread_estimate` = ARV × 70% − just value, and `pct_spread` = spread ÷ ARV.
The just value stands in for acquisition cost because there is no listing or
offer data. It is a proxy, not a price.

## ARV accuracy

The table compares each ARV as of January 1 with the parcel's first qualified,
non-multi-parcel single-family sale in the next 12 months. Error is
|ARV − price| ÷ price.
"Before fix" is the leaky version described in the next section.

| As of | Group | n | Median error (before fix → after) | Mean error | Within 10% | Within 20% |
|---|---|---|---|---|---|---|
| 2018 | All | 12,493 | 10.5% → **11.3%** | 16.2% | 44.8% | 74.8% |
| | High confidence | 9,981 | 9.2% → **10.1%** | 13.0% | 49.8% | 81.2% |
| 2019 | All | 12,730 | 9.8% → **10.0%** | 15.0% | 49.9% | 77.9% |
| | High confidence | 10,640 | 8.8% → **8.9%** | 12.5% | 54.8% | 83.6% |
| 2020 | All | 13,261 | 9.9% → **10.7%** | 14.7% | 47.4% | 77.5% |
| | High confidence | 11,207 | 8.9% → **9.7%** | 12.7% | 51.5% | 82.3% |
| 2021 | All | 16,581 | 12.1% → **15.7%** | 21.8% | 31.6% | 62.6% |
| | High confidence | 13,778 | 10.8% → **14.6%** | 18.8% | 34.0% | 67.3% |

Mean and within-X% columns are after the fix. Low-confidence ARVs (the other
~15–20% of each year) have median errors of 19.6–25.3%, which is why the
default view gates on confidence. 2021 is worse, most likely because prices
rose quickly during 2021 and the engine can only use the 2020 price level on
January 1, 2021.

## The look-ahead leak

**What it was.** The first version built one time-adjustment index from the
whole 2015–2025 dataset and reused it for every as-of date. Each year's index
point was that year's full-year median, anchored at July 1, with interpolation
between. For D = January 1, 2021, the index value at D was interpolated
between the July 2020 and July 2021 anchors, and the July 2021 anchor is built
from sales that hadn't happened yet. In a rising market this quietly borrowed
future appreciation.

**How it was found.** While reviewing the method before running the
back-test, I asked whether the index value used at D could depend on any sale
dated on or after D. It could. The comp pool already had an assertion against
post-D sales, but the index was built outside that check.

**How much it mattered.** At each D the leaky index overstated the price
level by +4.0% (2018), +2.4% (2019), +4.4% (2020), and +9.9% (2021). That
made median error look 0.1 to 3.8 points better than it really was (table
above). The leak was worst in 2021, when the market moved most.

**Fix and verification.** The index is now rebuilt from pre-D sales only, and
asserts that no anchor falls on or after D. At every D it equals the prior
year's median exactly. The stronger check is the invariance test in
`verify_lookahead_fix.py`: delete every sale dated on or after January 1,
2021 from the inputs and rerun. The index, the ARVs (a 40,000-parcel sample),
and the back-test signals all came out bit-for-bit identical, so nothing
downstream reads data from the future. The same script confirms the
look-ahead assertion fires on leaky input; it caught 2,334 post-D rows.

## Back-test design (`backtest_discount.py`)

- **As-of dates:** January 1 of 2018, 2019, 2020, and 2021.
- **Subjects:** every single-family parcel on the prior year's roll (373,089
  to 377,943 per year).
- **Default population:** high-confidence ARV and at least $25,000 spread,
  the same filter the app uses: 32,741 / 35,555 / 40,024 / 69,631 parcels.
  Results are also reported for an ungated population (every parcel with an
  ARV) in the results CSV.
- **Signals, all computed as of D:** `pct_spread`; absentee (owner mailing
  address or ZIP differs from the property's); tenure (time since last
  recorded sale); equity proxy (just value minus last qualified sale price);
  and an equal-weight composite of all four.
- **Outcomes, over the 30 months after D:**
  - **Discount:** among parcels that had a qualified sale, did it close 20%
    or more below the as-of ARV?
  - **Any sale:** did the parcel have any qualified sale? No valuation is
    involved.
  - **Below just value:** among parcels that sold, was the price below the
    DOR just value from the roll before the sale year? This does not use the
    ARV.
- **Comparisons:** for continuous signals, the top 10% versus the bottom 50%,
  with a seeded random tie-break (at most 2.7% of rows tie at the cutoff).
  Yes/no signals (absentee; "no sale on record since 2015") are compared as
  groups, because slicing a decile out of a mostly tied column lets row order
  decide who is in it. The base rate always comes from the same population
  being compared.
- **Multiple testing:** 144 comparisons in all, so the Bonferroni cutoff is
  p < 0.00035. Results below are marked as significant only if they pass it.
- **No tuning:** composite weights were fixed at equal quarters before any
  outcome was looked at, and nothing was reweighted afterward.

## Results

All figures are for the default population, as of 2018 / 2019 / 2020 / 2021.

### 1. `pct_spread` predicts discount-to-ARV, but not whether a home sells

Among homes that sold, the top 10% by `pct_spread` sold 20%+ below ARV far
more often than the bottom half:

| | 2018 | 2019 | 2020 | 2021 |
|---|---|---|---|---|
| Top 10% | 52.5% | 55.3% | 33.3% | 21.6% |
| Bottom 50% | 13.5% | 13.0% | 6.8% | 2.7% |

All four are significant. But the ranking says nothing about whether a home
sells. The top 10% sold at 8.2% / 7.7% / 8.4% / 9.1%, against 8.5% / 10.3% /
10.8% / 9.0% for the bottom half. In 2019 and 2020 the top-ranked homes were
slightly *less* likely to sell.

**Self-consistency caveat.** The discount outcome is measured against the
tool's own ARV, and `pct_spread` is large exactly when the ARV is high
relative to the just value. If the ARV overestimates a home's value, that home
ranks high **and** its eventual sale looks like a discount. The two errors are
the same error. So part of this result is probably the ARV grading itself.
The below-just-value outcome avoids that problem because it doesn't use the
ARV, and there `pct_spread` shows no lift: 1.8% vs 1.9%, 1.1% vs 1.7%, 0.2% vs
0.8%, 0.0% vs 0.6%. That check is weak, though. Only 0.4–1.6% of sales in this
population closed below just value, so it has little power. The honest
reading is that the ranking picks out homes whose sale prices land well under
the model's estimate. How much of that is a real bargain and how much is model
error, this data can't separate.

The equal-weight composite did worse than `pct_spread` alone on the discount
outcome (26.1% vs 16.9% in 2018, the only year that passes the cutoff). Min-max
scaling squeezed `pct_spread` into a very narrow range (standard deviation
0.17–0.19, against 35–46 for absentee and tenure), so the "equal" composite
was effectively absentee plus tenure.

### 2. Absentee ownership shows nothing usable

| | 2018 | 2019 | 2020 | 2021 |
|---|---|---|---|---|
| Discount: absentee vs not | 22.9% vs 18.4% | 21.5% vs 21.0% | 13.3% vs 11.7% | 6.4% vs 5.7% |
| Any sale: absentee vs not | 8.6% vs 8.6% | 9.5% vs 9.9% | 9.7% vs 10.5% | 9.8% vs 9.2% |

None of these pass the multiple-testing cutoff, the ungated population looks
the same, and the direction isn't consistent. Absentee owners made up about
14–15% of the default population.

### 3. Tenure: censored at 2015, and long tenure means fewer sales

The sales history starts in January 2015, so observed tenure tops out at about
three years for the 2018 as-of date and six years for 2021. A home that last
sold in 1998 and one that has never sold look the same. In practice the tenure
signal reduces to "has a recorded sale since 2015, or not," and 66–79% of the
default population has none.

That group sold **less**, not more:

| | 2018 | 2019 | 2020 | 2021 |
|---|---|---|---|---|
| Any sale: no sale since 2015 | 7.2% | 8.1% | 8.5% | 8.0% |
| Any sale: sold since 2015 | 13.9% | 14.8% | 14.7% | 11.6% |
| Relative difference | −48% | −45% | −42% | −31% |

All four years are significant (the ungated population shows −29% to −40%).
This data doesn't say why, but it runs against the premise that long-held
homes are the likeliest to come to market. When these
homes did sell, they were slightly more likely to sell at a discount (20.6% vs
16.1% in 2018), but no year passes the cutoff.

Among homes that *do* have a sale since 2015, longer tenure leaned toward
fewer sales (12.2% vs 15.6% in 2018, for example), but no year passes the
cutoff on any outcome.

**Equity proxy** only exists for parcels with a qualified sale since 2015
(10–17% of the default population), and its results on both the discount and
below-just-value outcomes partly reuse the same just value those outcomes
depend on. I don't treat it as evidence either way.

## Limitations

- **The ranking is validated on discount-to-ARV only,** and that outcome is
  partly self-referential (see above). No outcome here measures whether an
  owner would have accepted an off-market offer.
- **Just value is not a price.** Florida's Save Our Homes cap and assessment
  lag mean just value can sit well below market for long-held homesteaded
  homes. That inflates `spread_estimate` for exactly those homes.
- **No condition data.** The comps can't tell a renovated interior from an
  original one. Every ARV assumes "typical for its cohort."
- **Tenure is censored at 2015,** as described above.
- **Subject characteristics lag by up to a year,** since the as-of-January
  subject is described by the prior year's roll.
- **County-wide time index.** Neighborhoods that appreciated faster or slower
  than the county get a biased adjustment. This contributes to the 2021 error.
- **Renumbered parcels are not relinked,** so some sale histories are split
  across two IDs.
- **Miami-Dade single-family only.** `data_pipeline.py` can read Broward and
  Palm Beach NALs, but nothing outside Miami-Dade has been validated.
- **Not in the data:** tax delinquency (Miami-Dade publishes a free annual
  PDF, not yet ingested), lis pendens / pre-foreclosure (no free bulk source),
  and probate.
- **The app's ranking is as of January 1, 2026,** built from comps recorded
  through September 2025, the latest qualified single-family sale in the
  data.

## Repository layout

| File | What it does |
|---|---|
| `historical_sales.py` | Builds the deduplicated 2015–2025 sale history and per-vintage parcel snapshots from the DOR files |
| `historical_arv.py` | As-of ARV engine: comps, time index, confidence gate, spread columns |
| `backtest_discount.py` | The back-test described above; writes `data/backtest_discount_results.csv` |
| `verify_lookahead_fix.py` | Before/after accuracy, the delete-the-future invariance test, and the assertion check |
| `data_pipeline.py` | Parses the current (2025) NAL into `data/parcels.csv`: owner, mailing, and signal fields for the app |
| `app.py` | Streamlit screener. Default view: high-confidence ARV, $25K+ spread, sorted by `pct_spread` |
| `scorer.py` | Motivation grade (absentee / tenure / equity composite) shown in the app, labeled as not validated |
| `arv.py`, `backtest.py` | Earlier single-year ARV and flip back-test, superseded by the multi-year versions above |

## Setup

```bash
pip install -r requirements.txt
```

The raw DOR files are not in this repo. Place the Miami-Dade Final NAL and SDF
zips for 2016–2025 at `data/raw/historical/NAL/<year>F.zip` and
`data/raw/historical/SDF/<year>F.zip`, and the 2025 NAL at
`data/raw/Dade 23 Final NAL 2025.zip`. Then:

```bash
python3 historical_sales.py      # sale history + parcel snapshots (parquet)
python3 data_pipeline.py         # data/parcels.csv for the app
python3 backtest_discount.py     # back-test results
python3 verify_lookahead_fix.py  # leak check and accuracy table
streamlit run app.py
```

Raw and derived data files are excluded by `.gitignore`. They are large, and
the derived files include owner names and mailing addresses. Those are public
records, but I don't republish them.
