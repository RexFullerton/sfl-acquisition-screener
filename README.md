# South Florida Wholesale Deal Finder

> **Status:** Validation is pending receipt of multi-year historical NAL/SDF
> files from a Florida DOR public-records request (submitted; turnaround
> unknown). Current results are based on a single assessment year with a
> ~20-month sale-history window — see "Historical data" and "Known
> limitations" below before treating any number here as final.

## What this is

A residential acquisition-screening tool for off-market wholesale deals in
South Florida. It ingests county public-records data, computes distress
signals and a comp-based ARV (after-repair value) at the individual-parcel
level, and ranks properties by estimated spread between that ARV and their
current assessed value. It runs locally as a Streamlit app.

v1 is scoped to **Miami-Dade County, single-family homes only**. The
ingestion code is written to be multi-county (Broward and Palm Beach are
wired up, using the same Florida DOR data format), but only Miami-Dade has
been validated end-to-end — see "Known limitations."

## Data sources

| Source | Agency | What it provides | Access |
|---|---|---|---|
| NAL (Name-Address-Legal) bulk file, 2025 | Florida Department of Revenue / Miami-Dade Property Appraiser | Parcel ID, situs & mailing address, owner name, land use code, living area, year built, assessed/just value, and the two most recent recorded sales (price, date, arm's-length qualification code) per parcel | Free bulk download, `.zip` per county, from the Florida DOR property tax data portal |
| Delinquent Real Estate Property Taxes notice | Miami-Dade Tax Collector | Annual public-notice list of delinquent parcels ahead of the June 1 tax certificate sale | Free PDF, published annually — **not yet ingested, see limitations** |

**Not used:** MLS data (excluded per project scope — bulk extraction from
MLS violates its terms of service). Everything here comes from county
public records.

## Underwriting signals — what's real and why

Three signals, computed per parcel from the NAL file:

1. **Absentee ownership** — the owner's mailing address (normalized string
   comparison, not just ZIP) differs from the property's situs address, or
   the mailing ZIP differs from the situs ZIP. Rationale: an owner who
   doesn't live at the property is more reachable for an off-market offer
   and less likely to have an emotional attachment driving up their price
   expectation. `out_of_state` is tracked as a separate flag (mailing state
   isn't Florida) for filtering, not folded into the score.
2. **Ownership tenure** — years since the most recent recorded sale (from
   `SALE_YR1`). Long tenure often correlates with either a paid-off
   mortgage (motivated, flexible seller) or an aging/inherited situation.
   **Caveat:** ~88% of single-family parcels have no recorded sale at all
   in the NAL file. We treat a blank sale year as "long-tenured" (capped at
   100 years) rather than dropping it from the average — the alternative
   (silently averaging only the ~12% with a recent sale) is a real bug we
   found and fixed; see "Known limitations" for why even the fix is a
   judgment call, not a fact.
3. **Equity proxy** — `just_value − last recorded sale price`, exposed as
   both a dollar figure and an annualized % (using ownership tenure). This
   is **explicitly not real equity** — see limitations below. It's null
   for any parcel without a qualifying recorded sale (~95% of parcels),
   which is most of them; we return null rather than guess.

**Cut from v1 — not implemented:**
- **Tax delinquency**: Miami-Dade Tax Collector publishes a real bulk PDF
  of delinquent parcels, but it's a once-a-year snapshot requiring PDF
  table extraction, and it's not yet built. To add it: download the
  current year's PDF from mdctaxcollector.gov, extract the folio list, and
  join it to `parcels.csv` on `parcel_id`.
- **Pre-foreclosure / lis pendens**: no free bulk source exists. The Miami-
  Dade Clerk of Courts offers a paid "Commercial Data Services" bulk
  subscription ($110/month per data folder + notarized registration), but
  their own documentation doesn't confirm lis pendens filings are broken
  out in it without paying to see the file layout. The only free option is
  per-document search, which we were explicitly told not to script
  against. Cut for v1; revisit if the paid subscription is worth it to you.
- **Probate activity**: removed entirely. The prior version of this tool
  carried it as a signal with no ingestion behind it — a placeholder that
  silently contributed a constant score to every parcel and compressed the
  output's grade distribution. A signal that isn't measured isn't scored.

## ARV methodology

Comp-based, built entirely from recorded arm's-length sales in the NAL file
— no typed-in numbers, no synthetic data.

- **Comp geography**: the property appraiser's own `NBRHD_CD` (valuation
  neighborhood) code, not ZIP (too coarse — a ZIP can span very different
  submarkets) and not a lat/lon radius (the NAL file carries no
  coordinates; adding one would mean introducing an external geocoding
  dependency, out of scope here).
- **Comp filters**: living area within ±20%, year built within ±15 years,
  sold within the trailing 24 months, qualification code `01` or `02`
  (Florida DOR's own "arm's-length, included in sales ratio analysis"
  codes — see [floridarevenue.com/property/Documents/salequalcodes_bef01012025.pdf](https://floridarevenue.com/property/Documents/salequalcodes_bef01012025.pdf)).
- **Minimum comp count**: 5. Below that, `arv_estimate` is null — the tool
  does not guess.
- **Price/sqft**: median of the comp set, after trimming values outside
  1.5× IQR (guards against one data-entry error or atypical sale skewing a
  thin comp pool).
- **No market-time adjustment.** The 24-month window is the only control
  on comp staleness; a proper adjustment would need a repeat-sales price
  index built from more data than we have.
- **Confidence**: every ARV carries `arv_comp_count` and
  `arv_dispersion_pct` (IQR ÷ median $/sqft). Use them — see limitations.

Ranking: `spread_estimate = (ARV × 70%) − just_value`. The 70% figure
matches the MAO calculator's own default rule. `just_value` (current
assessed value) stands in for acquisition basis because we have no
listing-price or actual-offer data — it is a proxy, not a true basis.

## Back-test design and result

**There was no prior back-test in this codebase** — no file, no captured
methodology. This one was built from scratch, and the validation
population was derived directly from the NAL file's own sale records (not
from an externally supplied transaction list):

- A **flip candidate** = a parcel with two recorded, qualified arm's-length
  sales where the earlier one (acquisition) is 6–18 months before the
  later one (exit), with an exit price at least 10% above the acquisition
  price.
- Of 559 Miami-Dade single-family parcels with two qualified arm's-length
  sales on record, 329 have SALE_1 (documented as "the first selected
  sale") not actually later than SALE_2. This is **not** a data error —
  Florida DOR's own 2025 User's Guide states plainly that "sale selection
  is not necessarily based on chronological occurrence"; SAL1/SAL2 are
  whichever two sales DOR judged "most suitable for statistical analysis,"
  not guaranteed most-recent-first. (An earlier draft of this README
  guessed this was corrective/re-recorded deeds — that guess was wrong and
  has been corrected here.) Our code never relied on the slot labels for
  ordering: `backtest.py` derives acquisition-vs-exit strictly from the
  actual `sale1_date`/`sale2_date` values, so this DOR quirk doesn't
  invalidate the population, it just means the label names are not
  informative on their own. Of the 559, 230 have SALE_1's actual date
  later than SALE_2's; of those, **39 met the 6–18 month / ≥10% gain flip
  definition** — this is the validation population.
- **No-lookahead rule**: each flip is scored as of its acquisition month.
  Comps, the neighborhood comp pool, and the field of competing parcels it's
  ranked against all exclude any sale on or after that date — including
  the flip's own future exit sale.

**Result: 2 of 39 known flips (5.1%) landed in the top decile of the
model's ranked output.** Of the 39, only 23 could actually be scored at
all — the other 16 fell in a comp window with too little (in 7 cases,
*zero*) sale history to rank against (see below). Restricting to just the
23 that got a real ranking: **2 of 23 (8.7%)**. Either way this is at or
below the ~10% you'd expect from picking randomly — this model, as built,
is **not** currently better than chance at spotting these flips in
advance. Full detail in `data/backtest_results.csv`.

*(You previously recalled 12 of 15 from an earlier version of this
project. That number isn't reproducible from anything in this codebase —
no back-test file or methodology survived, and the two other copies of
this project found on disk before this rebuild — see Phase 1 assessment —
didn't contain one either. The number above is the real, current,
from-scratch result, not a comparison to that recollection.)*

**Why it's this low — three real, disclosed reasons, not tuning excuses:**

1. **The NAL file's sale history is structurally shallow — this is
   confirmed against DOR's own spec, not a parsing bug.** `SALE_YR1`/
   `SALE_YR2` across the *entire* Miami-Dade file contain no dates earlier
   than January 2024 (verified independently with Python's raw `csv`
   module, bypassing pandas and this project's pipeline entirely — see
   "Historical data" below). The reason is documented in the Florida DOR
   **2025 User's Guide, Department Property Tax Data Files**
   ([floridarevenue.com/property/dataportal/Documents/PTO%20Data%20Portal/User%20Guides/2025%20Users%20guide%20and%20quick%20reference/2025_NAL_SDF_NAP_Users_Guide.pdf](https://floridarevenue.com/property/dataportal/Documents/PTO%20Data%20Portal/User%20Guides/2025%20Users%20guide%20and%20quick%20reference/2025_NAL_SDF_NAP_Users_Guide.pdf)):
   the NAL's sale fields are merged in from the **Sale Data File (SDF)**,
   which by design "includes only parcels that transferred ownership
   during the year immediately preceding the January 1 assessment date
   and the sales that occurred after the January 1 assessment date up to
   the required submission date" — roughly an 18-20 month window, every
   year, for every Florida county. **This is a permanent structural
   ceiling on the single-year NAL file, not something a different parse
   or a bug fix would recover.** It means a flip acquired in January 2024
   has *zero* possible comps in a trailing-24-month window, because the
   window reaches back before any sale data exists in this file at all.
   The fix is ingesting multiple years of NAL/SDF (see "Historical data"
   below), not re-reading this one differently.
2. **Small, noisy sample.** 23-39 flips is not a lot of signal; a couple
   of hits either way swings the percentage by 4-5 points.
3. **The flip population may not be the right test of what this tool
   screens for.** A "flip" here is defined purely by price appreciation
   over a short hold — it says nothing about whether the *seller* was
   motivated at acquisition. The tool's actual distress signals (absentee
   ownership, tenure, equity proxy) aren't used in this ranking at all —
   only `spread_estimate` (ARV vs. assessed value) is. So this back-test
   measures the ARV/spread engine's retrospective accuracy, not the
   distress-signal screening that's the tool's actual differentiator. We
   have no ground truth for "was this owner motivated," so that half of
   the tool remains unvalidated by this or any back-test we could build
   from NAL data alone.

Nothing was adjusted to change this number. If you want to try a different
holding-period window, price-gain threshold, or top-quintile instead of
top-decile, say so explicitly and I'll rerun and report both the before
and after — not just the after.

**Known caveat on the back-test's ranking mechanics**: the NAL file is a
single 2025 snapshot — there's no historical time series of assessed
values. When scoring a flip "as of" an earlier acquisition date, its
competing parcels' `just_value` still reflects 2025 assessments, not what
they were assessed at then. This lookahead is applied uniformly to every
parcel being ranked at a given date, so it shouldn't systematically favor
the flip candidates over the rest of the field — but it does mean the
back-test measures *relative* ranking ability more cleanly than it
measures *absolute* dollar accuracy of `spread_estimate` at any historical
point.

## Historical data — status and how to get it

The single-year NAL file caps comp and back-test history at ~20 months
(see above). Getting past that requires **additional years of NAL/SDF**,
which Florida DOR does not publish for bulk download except for the
current assessment year — confirmed directly against the live DOR data
portal, which currently lists only `2026F`/`2026P` in its NAL directory.

Per DOR's Data Portal page (floridarevenue.com/property/Pages/DataPortal_RequestAssessmentRollGISData.aspx):

> "Only the most current version of each roll type is posted on our
> website. Data from previous years or prior rolls from the current year
> are available by request. ... The available tax rolls are: Preliminary
> and Final NAL and NAP files from **2002 to the current year**. Sale
> files from **2009 to the current year**."

So Miami-Dade NAL/SDF for 2016-2025 should exist, but it's **request-only**
— email `PTOTechnology@floridarevenue.com` (or fax/mail/phone; an optional
request form is also available), specifying county, year(s), and roll
type. No stated fee; turnaround isn't published. **This request has been
submitted** (NAL + SDF, Miami-Dade, 2016-2025) as of this writing —
turnaround unknown. This is a manual-request data source exactly like the
tax-delinquency PDF: ingestion code is written to read from locally-placed
files, never to fake a pull.

**Ingestion is written and waiting, not yet runnable**: `historical_sales.py`
stacks whatever NAL/SDF vintages land in `data/raw/historical/`, keyed for
dedup on `(parcel_id, sale_year, sale_month, sale_price, qual_code)`, and
does not assume SALE_1 is chronologically later than SALE_2 (see the
non-chronological-slot finding above) — ordering is always derived from
the actual date values. It includes a stub for SDF's different layout
(`SALE_ID_CD` per transaction, not a fixed SAL1/SAL2 pair) that hasn't
been exercised against a real SDF file yet. **This code is unvalidated**
— it was written against the known 2025 schema and reasoned expectations
about SDF, not against real multi-year files, because none exist locally
yet. Treat every assumption in it as provisional until it's run against
real 2016-2025 data and the per-year sale-date distributions are checked
(see the discipline in `backtest.py`'s original single-year investigation
— same standard applies here: if a year's dates don't land in that year's
expected ~18-20 month window, stop and report it, don't silently coerce).

**A bias to watch for once the data arrives**: joining a historical sale
to *current-roll* parcel characteristics (today's living area, year built,
DOR use code) assumes nothing physically changed at the parcel between
the sale and now. A parcel renovated, expanded, or rebuilt since a 2018
sale would have its 2018 sale price compared against 2025 characteristics
that didn't exist in 2018 — corrupting both comps and any back-test that
uses it. Having per-year NAL files (rather than one snapshot) fixes this
properly: each vintage carries the parcel's characteristics *as they were
assessed that year*, so a historical sale can be matched to
period-appropriate living area/year-built/use-code instead of today's.
`historical_sales.py` is structured to carry each vintage's own parcel
snapshot alongside its sales for this reason, but the actual
period-matching logic isn't built yet — it can't be tested without real
multi-year files.

## Known limitations — what a skeptical reviewer would poke holes in

- **Equity proxy is not equity.** It ignores any mortgage or lien balance
  entirely. Worse, Florida's Save Our Homes assessment cap can keep
  `just_value` well below true market value for long-held homesteaded
  parcels — which biases this proxy *low* for exactly the owners most
  likely to have real, substantial equity. Treat it as a rough, directional
  signal, not a number to underwrite against.
- **Spread estimates get unreliable at the extremes.** In ultra-luxury
  waterfront submarkets (found during testing: Pine Tree Dr, Hibiscus Dr,
  and Di Lido Dr on Miami Beach; Edgewater Dr in Coral Gables), the top of
  the spread-sorted leaderboard is dominated by parcels with old, modest
  assessed values sitting in the same `NBRHD_CD` as newly-built mega-
  mansions selling for 3–8× more per square foot. The comp-count and
  dispersion figures correctly flag these as low-confidence (dispersion
  32–58% vs. a 19.7% median, comp counts right at the 5-minimum floor), but
  the leaderboard's plain sort by `spread_estimate` doesn't yet *filter* on
  that confidence — so the least trustworthy estimates currently surface
  first unless you tighten the sidebar's dispersion/comp-count filters
  yourself. Filtering this automatically wasn't added post-hoc precisely
  because it was discovered late — see the project notes on not tuning to
  fit intuition.
- **Ownership tenure has a resolution problem.** ~88% of single-family
  parcels have no recorded sale in the NAL file at all, so they're all
  pinned at the 100-year tenure ceiling. The signal only meaningfully
  discriminates among the ~12% of parcels with an actual sale on record.
- **Absentee-address matching is exact-normalized-string, not fuzzy.**
  Minor formatting differences between how the same address is written in
  the owner-mailing vs. situs fields could produce false positives; the
  ZIP cross-check reduces but doesn't eliminate this.
- **No condition/finish-level data anywhere.** The comp model can't tell a
  gut-renovated interior from a 1970s original — every dollar figure here
  assumes "typical for its cohort," which is a real source of error at the
  parcel level even when the comp pool is solid.
- **The NAL file's sale history is structurally shallow — every recorded
  sale date in it falls between January 2024 and September 2025, by
  design (see "Historical data" above for the DOR spec citation).** For
  the current production ARV (which looks back from "now") this doesn't
  matter — the full window has data. It matters for anyone trying to run
  comps or a back-test "as of" an earlier date, where the trailing
  lookback window can run out of data entirely. Fix in progress: a
  multi-year records request has been submitted; ingestion code exists
  but is unvalidated until those files arrive.
- **Two of four target distress signals are simply not in this build**
  (tax delinquency, pre-foreclosure/lis pendens) — see "Cut from v1" above.
  Ranking currently rests on absentee ownership, tenure, and equity proxy
  only.
- **Validated on one county.** Broward and Palm Beach ingestion code runs,
  but hasn't been checked against real output the way Miami-Dade has.

## Setup / run

```bash
pip install -r requirements.txt

# 1. Download the Miami-Dade NAL 2025 zip from the Florida DOR property tax
#    data portal and place it at data/raw/Dade 23 Final NAL 2025.zip
#    (Broward/Palm Beach files go in the same folder if you want to try
#    the multi-county path — see data_pipeline.py's COUNTIES dict for the
#    exact expected filenames.)

python3 data_pipeline.py          # writes data/parcels.csv
python3 backtest.py               # optional — reruns the back-test, writes data/backtest_results.csv
streamlit run app.py              # opens the leaderboard at localhost:8501

# Once multi-year NAL/SDF arrive from the DOR records request (see
# "Historical data" below), place them under data/raw/historical/ and run:
python3 historical_sales.py       # UNVALIDATED until real files exist — see its docstring
```

## Raw data is not committed

`data/raw/` (the county NAL zip files, including `data/raw/historical/`
once multi-year files arrive) and the derived `data/parcels.csv` /
`data/historical_sales.csv` are excluded via `.gitignore` — they're large,
and the derived files carry real owner names and addresses pulled from
public records, which we'd rather not publish to a public repo by default
even though the source is public. Regenerate by downloading the NAL files
(see Setup above) and running `data_pipeline.py` / `historical_sales.py`.
