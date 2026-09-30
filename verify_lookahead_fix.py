"""
verify_lookahead_fix.py — reproduces the look-ahead leak check reported in the README.
 1. Before/after ARV accuracy with the ORIGINAL leaky global index vs. the fixed
    per-as-of index, identical everything else, all requested metrics.
 2. Index level at D, both ways, and proof the fixed one is held flat at the
    last complete-year anchor.
 3. Invariance test: delete every sale dated >= D from the inputs and confirm the
    fixed index, the ARVs, and the Step 4 signals are bit-for-bit unchanged.
    (Deleting future rows is the strongest leak test available -- if anything
    downstream moved, something was reading the future.)
 4. Confirm the assertions actually fire on leaky input.
"""
import numpy as np
import pandas as pd

import historical_arv as harv
import backtest_discount as bd
from historical_sales import assert_no_lookahead, LookaheadError

comp_events = harv.load_comp_events()
snaps = pd.read_parquet("data/nal_snapshots.parquet")
sales = pd.read_parquet("data/historical_sales.parquet")
sales_sf_comp = sales[sales["comp_eligible"] & (sales["dor_use_code"] == "001")]

FIXED_BUILD = harv.build_index_asof


def leaky_index(comp_events_, as_of_):
    """Exact reconstruction of the pre-fix build_monthly_index (commit dc128d8 era):
    one global index over ALL years, as_of ignored."""
    annual = comp_events_.groupby("sale_year")["price_per_sqft"].median().sort_index()
    anchors = pd.to_datetime({"year": annual.index, "month": 7, "day": 1})
    s = pd.Series(annual.values, index=anchors)
    full_range = pd.date_range(s.index.min(), s.index.max(), freq="MS")
    s = s.reindex(s.index.union(full_range)).sort_index().interpolate(method="time")
    return s.reindex(full_range)


def metrics(err):
    return dict(n=len(err), median=err.median() * 100, mean=err.mean() * 100,
                w10=(err <= 0.10).mean() * 100, w20=(err <= 0.20).mean() * 100)


rows = []
print("=== (2) Index level at D ===")
for Y in [2018, 2019, 2020, 2021]:
    as_of = pd.Timestamp(f"{Y}-01-01")
    fixed = FIXED_BUILD(comp_events, as_of)
    leak = leaky_index(comp_events, as_of)
    lvl_fixed = harv._index_level_at(fixed, [as_of])[0]
    lvl_leak = harv._index_level_at(leak, [as_of])[0]
    med_prev = comp_events[comp_events["sale_year"] == Y - 1]["price_per_sqft"].median()
    print(f"  D={as_of.date()}: fixed={lvl_fixed:.2f} (== median of {Y-1} sales {med_prev:.2f}: "
          f"{np.isclose(lvl_fixed, med_prev)}), last fixed index point={fixed.index.max().date()}, "
          f"leaky={lvl_leak:.2f} ({(lvl_leak/lvl_fixed-1)*100:+.1f}% vs fixed)")

    for label, builder in [("leaky (before)", leaky_index), ("fixed (after)", FIXED_BUILD)]:
        harv.build_index_asof = builder
        subjects = harv.subjects_as_of(as_of, snaps)
        res = harv.estimate_historical_arv(subjects, as_of, comp_events).join(subjects[["parcel_id"]])
        harv.build_index_asof = FIXED_BUILD

        window_end = as_of + pd.DateOffset(months=12)
        out = sales_sf_comp[(sales_sf_comp["sale_date"] >= as_of) & (sales_sf_comp["sale_date"] < window_end)]
        out = out.sort_values("sale_date").drop_duplicates("parcel_id", keep="first")
        m = res.merge(out[["parcel_id", "price"]], on="parcel_id", how="inner")
        m = m[m["arv_estimate"].notna()]
        m["err"] = (m["arv_estimate"] - m["price"]).abs() / m["price"]
        for band, sub in [("overall", m), ("high_conf", m[m["high_confidence"]]), ("low_conf", m[~m["high_confidence"]])]:
            rows.append(dict(year=Y, version=label, band=band, **metrics(sub["err"])))

acc = pd.DataFrame(rows)
acc.to_csv("data/accuracy_before_after.csv", index=False)
pd.set_option("display.width", 200)
print("\n=== (1) Accuracy before/after ===")
print(acc.round(1).to_string(index=False))

print("\n=== (3) Invariance test: delete all data dated >= D, results must not change ===")
as_of = pd.Timestamp("2021-01-01")
comp_past = comp_events[comp_events["sale_date"] < as_of]
sales_past = sales[sales["sale_date"] < as_of]

idx_full = harv.build_index_asof(comp_events, as_of)
idx_past = harv.build_index_asof(comp_past, as_of)
print(f"  index identical with future rows deleted: {idx_full.equals(idx_past)}")

subjects = harv.subjects_as_of(as_of, snaps).sample(40000, random_state=0).reset_index(drop=True)
arv_full = harv.estimate_historical_arv(subjects, as_of, comp_events)
arv_past = harv.estimate_historical_arv(subjects, as_of, comp_past)
print(f"  ARV identical with future rows deleted (40k-subject sample): {arv_full.equals(arv_past)}")

sig_full = bd.compute_historical_signals(as_of, subjects, sales)
sig_past = bd.compute_historical_signals(as_of, subjects, sales_past)
cols = ["absentee", "ownership_tenure_years", "equity_proxy"]
print(f"  Step 4 signals identical with future rows deleted: {sig_full[cols].equals(sig_past[cols])}")

print("\n=== (4) Assertions fire on leaky input ===")
leaky_pool = comp_events[comp_events["sale_date"] <= as_of + pd.DateOffset(months=1)]
try:
    assert_no_lookahead(leaky_pool, as_of)
    print("  assert_no_lookahead: DID NOT FIRE -- problem")
except LookaheadError as e:
    print(f"  assert_no_lookahead fired as expected: {str(e)[:90]}...")
