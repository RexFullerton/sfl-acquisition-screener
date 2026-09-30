South Florida Acquisition Screener

Finding off-market sellers was the hardest part of the wholesale work I did with my family's company in Maryland, DC and Virginia. Most of our deals came from referrals and cold outreach. Since I go to school in Miami, I wanted to know two things: could public records do that search instead, and would the approach carry over to a different market? So I built a screening tool for Miami-Dade single-family homes from Florida Department of Revenue (DOR) tax-roll records, then back-tested it against what actually happened to those homes, using only information that existed at each point in time.

The valuation held up. The seller signals didn't. My comp-based valuation lands within about 10% median error of actual 2018-2020 sale prices. Ranking homes by the gap between that estimate and assessed value picks out homes that later sold well below the estimate, though part of that is the model grading itself (explained below), and the ranking says nothing about whether a home will sell. Absentee ownership predicted nothing I could use. Homes with no recorded sale since 2015 were 31-48% less likely to sell, the opposite of the long-tenure idea. I also found a look-ahead leak in my own valuation engine that had been making it look more accurate than it was.

The tool runs locally as a Streamlit app. Its default view is the ranking the back-test measured.

The data

The first version used a single year of DOR data, and that turned out to be its biggest problem. The annual file only carries about 18-20 months of sales, so there wasn't enough history to value homes in the past or test anything. My first back-test caught 2 of 39 known flips in its top decile, which is no better than chance. DOR only posts the current year for download, so I filed a public-records request for older files. What I got back was 10 annual tax rolls covering 11 years of sales (2015-2025): the Miami-Dade NAL (parcel) and SDF (sale) Final rolls for 2016 through 2025. No MLS data is used anywhere.

Because each annual sale file overlaps the next, the same sale shows up more than once. I stacked all ten years and deduplicated on parcel, sale year, sale month, price and recording reference (OR book and page, or the clerk's instrument number). Sales are only dated to the month, so price alone isn't a safe key. 319 groups had the same recorded instrument but disagreed across files, and I kept the later file's version. That leaves 945,592 distinct sales, of which 421,426 are qualified arm's-length and 156,810 are qualified single-family.

Every sale carries a DOR qualification code, and I only use codes 01 and 02 (qualified arm's-length) as comps or outcomes. Before trusting those across eleven years, I checked the code definitions in three versions of DOR's document, effective 2015, 2018 and 2024. The first two are only on the Wayback Machine. Codes 01 and 02 are worded the same in all three. The one change I found was code 21 (contract for deed), added in 2018. It's a disqualified code either way, so the arm's-length set doesn't change.

Code	Meaning	Used?
01, 02	Qualified arm's length	Yes
03-06	Arm's length but excluded from DOR's ratio study (characteristics changed, multi-parcel, etc.)	No
11-21	Disqualified by deed type (quitclaim, tax deed, foreclosure-related, etc.)	No
30-43	Disqualified by evidence (related party, forced sale, atypical financing, etc.)	No
98-99	Pending	No

Sources: 2015 version, 2018 version, 2024 version.

A separate flag marks sales where one price covered several parcels. 287 qualified sales (0.07%) have it, and I drop them from comps. I also kept a snapshot of every single-family parcel for each year (living area, year built, neighborhood code, assessed "just" value, owner mailing address), so a home can be described as it was in a given year instead of as it is today. 92.5% of parcel IDs appear in every year. Of the IDs that disappear, 14.4% come back under a new ID with the same owner and address. I flag those but don't relink them.

How the valuation works

The ARV (after-repair value) is calculated as of a date D and can't use anything dated on or after D. Comps are qualified, single-parcel, single-family sales in the same DOR neighborhood, within ±20% living area and ±15 years built, sold in the 36 months before D, excluding the home's own sales. Each comp is described by the tax roll from the year it sold, not today's. A house that was 1,400 sq ft when it sold in 2017 and was expanded to 2,200 in 2022 gets compared as a 1,400 sq ft house. Of 156,779 eligible single-family sales, 129,849 matched to a same-year snapshot. The home being valued is described by the prior year's roll, because the current year's roll isn't certified until the fall.

Comp prices per square foot are adjusted to D with a county-wide monthly index, rebuilt for each D from earlier sales only and held flat after the last complete year. The estimate is the median adjusted $/sq ft after trimming outliers beyond 1.5× IQR, times the home's living area. If restricting comps to the home's own assessed-value tercile in the neighborhood gives a tighter set, I use that set. With fewer than 5 comps there's no estimate. An estimate counts as high confidence if it has at least 8 comps and comp dispersion (IQR ÷ median $/sq ft) of 30% or less.

The spread is ARV × 70% minus the just value, and pct_spread is that spread divided by ARV. Just value stands in for the purchase price because there's no listing or offer data. It's a proxy, not a price.

How accurate it is

This compares each January 1 estimate with the home's first qualified, single-parcel single-family sale in the next 12 months. Error is |ARV − price| ÷ price. "Before fix" is the leaky version described in the next section.

As of	Group	n	Median error (before fix → after)	Mean error	Within 10%	Within 20%
2018	All	12,493	10.5% → 11.3%	16.2%	44.8%	74.8%
	High confidence	9,981	9.2% → 10.1%	13.0%	49.8%	81.2%
2019	All	12,730	9.8% → 10.0%	15.0%	49.9%	77.9%
	High confidence	10,640	8.8% → 8.9%	12.5%	54.8%	83.6%
2020	All	13,261	9.9% → 10.7%	14.7%	47.4%	77.5%
	High confidence	11,207	8.9% → 9.7%	12.7%	51.5%	82.3%
2021	All	16,581	12.1% → 15.7%	21.8%	31.6%	62.6%
	High confidence	13,778	10.8% → 14.6%	18.8%	34.0%	67.3%

The mean and within-X% columns are after the fix. The low-confidence estimates, about 15-20% of each year, have median errors of 19.6-25.3%, which is why the app hides them by default. 2021 is worse, most likely because prices rose fast that year and on January 1, 2021 the model can only see the 2020 price level.

The look-ahead leak

My first version built one price index from the whole 2015-2025 dataset and reused it for every valuation date. Each year's index point was that year's full-year median, placed at July 1, with interpolation in between. For a valuation as of January 1, 2021, that meant interpolating toward a July 2021 point built from sales that hadn't happened yet. In a rising market, the model was quietly borrowing future appreciation.

I found it while reviewing the method before running the back-test. I asked whether the index value used at a date could depend on any sale on or after that date, and it could. I had already put a check on the comp pool that fails if any comp is dated on or after D, but the index was built outside that check, so nothing caught it.

At each date, the leaky index overstated the price level by +4.0% (2018), +2.4% (2019), +4.4% (2020) and +9.9% (2021). That made median error look 0.1 to 3.8 points better than it really was, worst in 2021, when the market moved most. Now the index is rebuilt from earlier sales only, and it fails loudly if any anchor falls on or after D. The test I trust more is in verify_lookahead_fix.py: delete every sale dated on or after January 1, 2021 and rerun. The index, the valuations (a 40,000-parcel sample) and the back-test signals all came out bit-for-bit identical, so nothing downstream reads the future. The same script confirms the check fires on leaky input. It caught 2,334 post-date rows.

The back-test

I scored every single-family home as of January 1 of 2018, 2019, 2020 and 2021 (373,089 to 377,943 homes each year), using the prior year's roll and only sales before that date. The main population is the one the app shows by default: high-confidence estimates with at least $25,000 of spread, which was 32,741 / 35,555 / 40,024 / 69,631 homes. The signals were pct_spread, absentee ownership (owner's mailing address or ZIP differs from the property's), tenure (time since the last recorded sale), an equity proxy (just value minus the last qualified sale price), and an equal-weight composite of all four.

I measured three outcomes over the next 30 months. The discount outcome asks whether homes that sold went for 20% or more below the estimate. The any-sale outcome asks whether a home sold at all, with no valuation involved. The below-just-value outcome asks whether homes that sold went for less than the prior year's DOR just value, which doesn't use my estimate.

For continuous signals I compared the top 10% with the bottom 50%, breaking ties randomly with a fixed seed (at most 2.7% of rows tie at the cutoff). Yes/no signals, like absentee or "no sale since 2015," I compared as groups. My first run sliced deciles out of those mostly tied columns and let row order decide who was in them, which produced results that weren't real. That run also used a base rate from a bigger population than the one being compared. Both are fixed. There are 144 comparisons in all, so I only call a result significant if it clears a Bonferroni cutoff of p < 0.00035. I set the composite weights at equal quarters before looking at any outcome and didn't change them afterward.

What I found

All figures below are the default population, as of 2018 / 2019 / 2020 / 2021.

pct_spread predicts which sales close well below the estimate. Among homes that sold, the top 10% sold 20%+ below the estimate far more often than the bottom half, and all four years are significant:

	2018	2019	2020	2021
Top 10%	52.5%	55.3%	33.3%	21.6%
Bottom 50%	13.5%	13.0%	6.8%	2.7%

It doesn't predict whether a home sells. The top 10% sold at 8.2% / 7.7% / 8.4% / 9.1% against 8.5% / 10.3% / 10.8% / 9.0% for the bottom half, and in 2019 and 2020 the top-ranked homes were slightly less likely to sell.

I don't read the discount result as proof the ranking finds bargains. The outcome is measured against my own estimate, and pct_spread is large exactly when the estimate is high relative to just value. If the model overvalues a home, that home ranks high and its eventual sale looks like a discount, from the same error. The below-just-value outcome avoids that, and there the ranking shows no lift: 1.8% vs 1.9%, 1.1% vs 1.7%, 0.2% vs 0.8%, 0.0% vs 0.6%. That check is weak, though, because only 0.4-1.6% of sales in this group closed below just value. The fair summary is that the ranking picks out homes whose sale prices land well under the model's estimate, and this data can't separate a real bargain from model error.

The equal-weight composite did worse than pct_spread alone. On the discount outcome its top 10% came in at 26.1% against 16.9% for its bottom half in 2018, the only year that clears the cutoff, compared with 52.5% against 13.5% for pct_spread. The way I scaled the inputs squeezed pct_spread into a very narrow range (standard deviation 0.17-0.19, against 35-46 for absentee and tenure), so the "equal" composite was really absentee plus tenure.

Absentee ownership showed nothing usable:

	2018	2019	2020	2021
Discount: absentee vs not	22.9% vs 18.4%	21.5% vs 21.0%	13.3% vs 11.7%	6.4% vs 5.7%
Any sale: absentee vs not	8.6% vs 8.6%	9.5% vs 9.9%	9.7% vs 10.5%	9.8% vs 9.2%

None of those clear the cutoff, the direction isn't consistent, and the wider population looks the same. Absentee owners were about 14-15% of the default population.

Tenure turned out to be mostly a data artifact. My sale history starts in January 2015, so the longest tenure I can observe is about three years as of 2018 and six as of 2021. A home last sold in 1998 looks the same as one that has never sold. In practice the signal is just "has a recorded sale since 2015, or not," and 66-79% of the default population has none. Those homes sold less, not more:

	2018	2019	2020	2021
Any sale: no sale since 2015	7.2%	8.1%	8.5%	8.0%
Any sale: sold since 2015	13.9%	14.8%	14.7%	11.6%
Relative difference	−48%	−45%	−42%	−31%

All four years are significant, and the wider population shows −29% to −40%. The data doesn't say why, but it cuts against the idea that long-held homes are the likeliest to come to market. When those homes did sell they were slightly more likely to sell at a discount (20.6% vs 16.1% in 2018), but no year clears the cutoff. Among homes that do have a sale since 2015, longer tenure leaned toward fewer sales (12.2% vs 15.6% in 2018, for example), but again nothing clears the cutoff.

The equity proxy only exists for homes with a qualified sale since 2015 (10-17% of the default population), and its results reuse the same just value the discount and below-just-value outcomes depend on. I don't count it as evidence either way.

What I'd do next

The question I started with, whether an owner would take an off-market offer, needs outcome data this project doesn't have: offers made and accepted. The next honest test is to pull a small, recent list from the default ranking, work it the way we worked leads at my family's company, and track response and conversion rates. On the data side, Miami-Dade's tax-delinquency list and probate filings are the two public signals I'd add first.

Limitations

The ranking is only validated on discount-to-estimate, and that outcome is partly self-referential. Nothing here measures whether an owner would have taken an off-market offer, which is the question I started with.

Just value isn't a price. Florida's Save Our Homes cap and assessment lag can keep it well below market for long-held homesteaded homes, which inflates the spread for exactly those homes. The comps also can't tell a renovated interior from an original one, so every estimate assumes a home is typical for its group. The home being valued is described by the prior year's roll, so its characteristics can lag by up to a year. The price index is county-wide, so neighborhoods that appreciated faster or slower than the county get a biased adjustment, which is part of the 2021 error. Renumbered parcels aren't relinked, so some sale histories are split across two IDs.

Everything is Miami-Dade single-family. data_pipeline.py can read Broward and Palm Beach files, but I haven't validated anything outside Miami-Dade. Tax delinquency (Miami-Dade publishes a free annual PDF I haven't ingested), lis pendens and pre-foreclosure (no free bulk source), and probate aren't in the data. The app's ranking is as of January 1, 2026, using comps through September 2025, the latest qualified single-family sale in the data.

Files and how to run it
File	What it does
historical_sales.py	Builds the deduplicated 2015-2025 sale history and yearly parcel snapshots from the DOR files
historical_arv.py	The as-of valuation engine: comps, price index, confidence gate, spread
backtest_discount.py	The back-test above; writes data/backtest_discount_results.csv
verify_lookahead_fix.py	Before/after accuracy, the delete-the-future test, and the leak-check test
data_pipeline.py	Parses the 2025 roll into data/parcels.csv (owner, mailing and signal fields for the app)
app.py	The Streamlit screener. Default view: high confidence, $25K+ spread, sorted by pct_spread
scorer.py	The absentee/tenure/equity grade shown in the app, labeled as not validated
arv.py, backtest.py	The earlier single-year valuation and flip back-test, replaced by the files above

The raw DOR files aren't in the repo. Put the Miami-Dade Final NAL and SDF zips for 2016-2025 at data/raw/historical/NAL/<year>F.zip and data/raw/historical/SDF/<year>F.zip, and the 2025 NAL at data/raw/Dade 23 Final NAL 2025.zip. Then:

bash
pip install -r requirements.txt
python3 historical_sales.py
python3 data_pipeline.py
python3 backtest_discount.py
python3 verify_lookahead_fix.py
streamlit run app.py

Raw and derived data files are gitignored. They're large, and the derived files include owner names and mailing addresses. Those are public records, but I don't want to republish them.
