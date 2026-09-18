"""
backtest.py — Back-test against known Miami-Dade single-family flips.

There is no prior back-test in this codebase and no captured methodology to
resume from (see Phase 1 assessment). The validation population is derived
directly from the NAL file's own two sale records — not from an external
transaction list — per the project owner's explicit instruction.

Flip definition: a parcel where SALE_2 (the earlier of the two recorded
sales) is the acquisition and SALE_1 (the later one) is the exit sale, both
qualified arm's-length (QUAL_CD in {01, 02}), with:
  - a holding period of 6-18 months between them, and
  - a price gain of at least 10% from SALE_2 to SALE_1.

NO-LOOKAHEAD RULE: each flip is scored as of its ACQUISITION date (SALE_2's
month) — the same date a wholesaler would have been evaluating the deal.
Comps and the ranked field of competing parcels are built using only sales
that happened strictly before that date. The flip's own exit sale (SALE_1)
and its own acquisition transaction never appear in its own comp pool or
in what the model "knows" at scoring time. See arv.py's estimate_arv().

Usage:
    python3 backtest.py
"""

import pandas as pd

import arv as arv_mod
import scorer

HOLDING_MONTHS_MIN = 6
HOLDING_MONTHS_MAX = 18
MIN_PRICE_GAIN_PCT = 10.0
TOP_DECILE_PCT = 90.0  # a flip "lands in the top decile" if its spread percentile rank >= this

DTYPES = {"parcel_id": str, "nbrhd_cd": str, "sale1_qual_cd": str, "sale2_qual_cd": str}


def _sale_date(df: pd.DataFrame, suffix: str) -> pd.Series:
    year = pd.to_numeric(df[f"sale{suffix}_year"], errors="coerce")
    month = pd.to_numeric(df[f"sale{suffix}_month"], errors="coerce").clip(1, 12)
    return pd.to_datetime({"year": year, "month": month, "day": 1}, errors="coerce")


def derive_flip_population(parcels: pd.DataFrame) -> pd.DataFrame:
    p = parcels.copy()
    for c in ["sale1_price", "sale2_price"]:
        p[c] = pd.to_numeric(p[c], errors="coerce")

    both_arms_length = p["sale1_qual_cd"].isin(arv_mod.ARMS_LENGTH_QUAL_CODES) & p["sale2_qual_cd"].isin(arv_mod.ARMS_LENGTH_QUAL_CODES)
    both_priced = p["sale1_price"].gt(0) & p["sale2_price"].gt(0)
    cand = p[both_arms_length & both_priced].copy()

    cand["sale1_date"] = _sale_date(cand, "1")
    cand["sale2_date"] = _sale_date(cand, "2")
    cand["holding_months"] = (
        (cand["sale1_date"].dt.year - cand["sale2_date"].dt.year) * 12
        + (cand["sale1_date"].dt.month - cand["sale2_date"].dt.month)
    )

    print(f"Parcels with two qualified arm's-length sales on record: {len(cand):,}")
    backwards_or_tied = cand["holding_months"] <= 0
    print(f"  Of those, SALE_1 not later than SALE_2 (tied or reversed order — "
          f"likely corrective/re-recorded deeds, not real flips): {backwards_or_tied.sum():,}")
    cand = cand[cand["holding_months"] > 0].copy()
    print(f"  Correctly-ordered acquisition -> exit pairs: {len(cand):,}")
    print(cand["holding_months"].describe())

    cand["price_delta_pct"] = (cand["sale1_price"] / cand["sale2_price"] - 1) * 100

    flips = cand[cand["holding_months"].between(HOLDING_MONTHS_MIN, HOLDING_MONTHS_MAX)].copy()
    print(f"\nHolding period {HOLDING_MONTHS_MIN}-{HOLDING_MONTHS_MAX} months: {len(flips):,}")
    print(flips["price_delta_pct"].describe())

    flips = flips[flips["price_delta_pct"] >= MIN_PRICE_GAIN_PCT].copy()
    print(f"\n+ price gain >= {MIN_PRICE_GAIN_PCT:.0f}%: {len(flips):,} — this is the validation population")
    return flips


def run_backtest(parcels: pd.DataFrame, flips: pd.DataFrame) -> pd.DataFrame:
    """For each flip, rank it against the full parcel population as of its
    acquisition month and report whether it landed in the top decile of
    spread_estimate. One estimate_arv() pass per distinct acquisition month
    (not per flip) — many flips share a month."""
    results = []
    for period, group in flips.groupby(flips["sale2_date"].dt.to_period("M")):
        as_of = period.to_timestamp()
        eligible = parcels[parcels["year_built"] <= as_of.year].copy()

        arv_res = arv_mod.estimate_arv(eligible, as_of=as_of)
        scored = scorer.score_parcels(pd.concat([eligible, arv_res], axis=1), scorer.DEFAULT_WEIGHTS)
        ranked = scored[scored["spread_estimate"].notna()].copy()
        ranked["percentile"] = ranked["spread_estimate"].rank(pct=True) * 100

        for _, flip in group.iterrows():
            row = ranked[ranked["parcel_id"] == flip["parcel_id"]]
            if row.empty:
                results.append({
                    "parcel_id": flip["parcel_id"], "as_of": as_of, "percentile": None,
                    "in_top_decile": False, "reason": "no ARV / not in ranked pool at as_of",
                })
                continue
            pct = row["percentile"].iloc[0]
            results.append({
                "parcel_id": flip["parcel_id"], "as_of": as_of, "percentile": round(pct, 1),
                "in_top_decile": pct >= TOP_DECILE_PCT, "reason": "",
                "holding_months": flip["holding_months"], "price_delta_pct": round(flip["price_delta_pct"], 1),
            })
        print(f"  scored {len(group)} flip(s) acquired {period} against {len(ranked):,} ranked parcels")

    return pd.DataFrame(results)


if __name__ == "__main__":
    parcels = pd.read_csv("data/parcels.csv", dtype=DTYPES)
    print("=== Deriving flip population from NAL sale records ===\n")
    flips = derive_flip_population(parcels)

    print("\n=== Scoring each flip as of its acquisition date (no lookahead) ===\n")
    results = run_backtest(parcels, flips)
    results.to_csv("data/backtest_results.csv", index=False)

    n = len(results)
    hits = results["in_top_decile"].sum()
    print(f"\n=== RESULT: {hits} of {n} known flips landed in the top decile of the model's ranked output ===")
