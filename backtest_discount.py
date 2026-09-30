"""
backtest_discount.py — Step 4 back-test: does the as-of score predict outcomes?

Three outcomes, all measured over the 30 months after as-of date D:
  - discount   : among parcels with a comp-eligible sale, share that sold 20%+
                 below their as-of ARV. (Depends on our own ARV.)
  - A_any_sale : share of parcels with ANY qualified (QUAL_CD 01/02) sale.
                 No valuation involved.
  - B_below_jv : among parcels with a qualified sale, share priced below the
                 parcel's DOR just value from the roll preceding the sale year
                 (vintage sale_year - 1, the latest certified roll at the time
                 of sale). Independent of our ARV -- but NOT independent of
                 pct_spread or equity_proxy, which are built from the same
                 just_value. See the caveat in the report.

No-lookahead, end to end:
  - ARV as of D uses only comps dated before D, with a time index rebuilt
    from pre-D sales only (historical_arv.py).
  - absentee / tenure / equity_proxy are computed from the vintage D.year-1
    roll and from sales dated before D (compute_historical_signals()).
  - Outcomes are only looked up for sales dated on/after D and never feed
    back into any score.

Evaluation rules (fixed after a bug was found in the first Step 4 run):
  - Base rate is always computed on the SAME population as the top/bottom
    groups being compared.
  - Binary or heavily tied signals are reported as group comparisons
    (absentee vs. not; "no sale on record since 2015" vs. has one), never as
    deciles -- slicing a decile out of a mostly-tied column lets row order
    decide group membership.
  - Continuous signals are split into top decile / bottom 50% with a seeded
    random tie-break, and the share of rows tied at the decile boundary is
    reported so any residual tie problem is visible.

Composite weights: equal quarters across absentee, tenure, equity_proxy,
pct_spread (as originally specified; deliberately not tuned to outcomes).
"""

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

import historical_arv as harv
from historical_sales import assert_no_lookahead

SALES_PARQUET = Path("data/historical_sales.parquet")
SNAPSHOTS_PARQUET = Path("data/nal_snapshots.parquet")
RESULTS_CSV = Path("data/backtest_discount_results.csv")

DISCOUNT_THRESHOLD = 0.20
OUTCOME_WINDOW_MONTHS = 30  # midpoint of the requested 24-36 month range
TIE_BREAK_SEED = 0

COMPOSITE_WEIGHTS = {
    "absentee_score": 25,
    "tenure_score": 25,
    "equity_score": 25,
    "pct_spread_score": 25,
}


def _normalize_addr(s: pd.Series) -> pd.Series:
    s = s.fillna("").str.upper().str.strip()
    s = s.str.replace(r"[^\w\s]", "", regex=True)
    s = s.str.replace(r"\s+", " ", regex=True)
    return s


def _zip5(s: pd.Series) -> pd.Series:
    return s.fillna("").str.strip().str[:5].str.zfill(5)


def _minmax(series: pd.Series) -> pd.Series:
    lo, hi = series.min(), series.max()
    if pd.isna(lo) or pd.isna(hi) or hi <= lo:
        return pd.Series(np.nan, index=series.index)
    return (series - lo) / (hi - lo) * 100.0


def _pct_rank(series: pd.Series) -> pd.Series:
    return series.rank(pct=True, method="average") * 100.0


# ── Historical signals, computed strictly as-of D ───────────────────────────

def compute_historical_signals(as_of: pd.Timestamp, subjects: pd.DataFrame, all_sales: pd.DataFrame) -> pd.DataFrame:
    """
    subjects: from subjects_as_of() (vintage D.year-1), carrying owner mailing
    fields. all_sales: full historical_sales.parquet.
    Tenure uses ANY recorded sale (production convention); equity_proxy uses
    only qualified sales (a $100 corrective deed would corrupt it).
    """
    out = subjects.copy()

    norm_own = _normalize_addr(out["own_addr1"])
    norm_situs = _normalize_addr(out["situs_addr1"])
    mail_zip = _zip5(out["own_zip"])
    situs_zip = _zip5(out["situs_zip"])
    out["absentee"] = (
        ((norm_own != norm_situs) | (mail_zip != situs_zip))
        & (mail_zip != "00000") & (mail_zip != "")
    )

    prior_sales = all_sales[all_sales["sale_date"] < as_of]
    assert_no_lookahead(prior_sales, as_of)

    # Sale history starts 2015-01, so observable tenure is censored at
    # (D - 2015-01): ~3 years for D=2018, ~6 for D=2021. "No sale on record"
    # is kept as its own flag because it is really a group, not a quantity.
    last_sale_date = prior_sales.sort_values("sale_date").groupby("parcel_id")["sale_date"].last()
    mapped = out["parcel_id"].map(last_sale_date)
    out["no_sale_on_record"] = mapped.isna()
    tenure_days = (as_of - mapped).dt.days
    out["ownership_tenure_years"] = (tenure_days / 365.25).fillna(100).clip(0, 100)

    qual_prior = prior_sales[prior_sales["qualified"]].sort_values("sale_date")
    last_qual = qual_prior.groupby("parcel_id").last()
    out["last_qual_price"] = out["parcel_id"].map(last_qual["price"])
    out["equity_proxy"] = out["just_value"] - out["last_qual_price"]

    return out


def build_composite_score(scored: pd.DataFrame, normalization: str = "minmax") -> pd.DataFrame:
    """
    normalization="minmax" is the composite as originally specified.
    normalization="rank" puts every component on a percentile scale first, so
    that a few extreme values in one signal can't compress its spread and
    silently shrink its effective weight. Outcome-free either way.
    """
    out = scored.copy()
    norm = _minmax if normalization == "minmax" else _pct_rank

    out["absentee_score"] = out["absentee"].astype(float) * 100.0
    out["tenure_score"] = norm(out["ownership_tenure_years"])

    equity = out["equity_proxy"]
    if normalization == "minmax":
        lo, hi = equity.quantile(0.05), equity.quantile(0.95)
        out["equity_score"] = _minmax(equity.clip(lo, hi))
    else:
        out["equity_score"] = _pct_rank(equity)
    out.loc[equity.isna(), "equity_score"] = np.nan

    out["pct_spread_score"] = norm(out["pct_spread"])
    out.loc[out["pct_spread"].isna(), "pct_spread_score"] = np.nan

    cols = list(COMPOSITE_WEIGHTS.keys())
    w = np.array([COMPOSITE_WEIGHTS[c] for c in cols], dtype=float)
    values = out[cols].to_numpy(dtype=float)
    present = ~np.isnan(values)
    weighted_sum = np.nansum(values * w, axis=1)
    weight_total = (present * w).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["composite_score"] = np.where(weight_total > 0, weighted_sum / weight_total, np.nan)
    return out


# ── Outcomes ─────────────────────────────────────────────────────────────────

def attach_outcomes(scored: pd.DataFrame, as_of: pd.Timestamp, all_sales: pd.DataFrame,
                     snaps: pd.DataFrame, window_months: int = OUTCOME_WINDOW_MONTHS) -> pd.DataFrame:
    window_end = as_of + pd.DateOffset(months=window_months)
    window = all_sales[
        (all_sales["dor_use_code"] == "001")
        & (all_sales["sale_date"] >= as_of) & (all_sales["sale_date"] < window_end)
    ].sort_values("sale_date")

    ce = window[window["comp_eligible"]].drop_duplicates("parcel_id", keep="first")
    ce = ce[["parcel_id", "price"]].rename(columns={"price": "ce_price"})

    q = window[window["qualified"]].drop_duplicates("parcel_id", keep="first")
    q = q[["parcel_id", "price", "sale_year"]].rename(columns={"price": "q_price", "sale_year": "q_sale_year"})
    q["jv_vintage"] = q["q_sale_year"] - 1
    jv = snaps[["parcel_id", "vintage_year", "just_value"]].rename(
        columns={"vintage_year": "jv_vintage", "just_value": "jv_prior"})
    q = q.merge(jv, on=["parcel_id", "jv_vintage"], how="left")

    out = scored.merge(ce, on="parcel_id", how="left").merge(q, on="parcel_id", how="left")

    out["sold_ce"] = out["ce_price"].notna()
    out["discount_pct"] = (out["arv_estimate"] - out["ce_price"]) / out["arv_estimate"]
    out["discounted"] = out["sold_ce"] & (out["discount_pct"] >= DISCOUNT_THRESHOLD)

    out["sold_q"] = out["q_price"].notna()                          # outcome A
    out["has_jv_prior"] = out["sold_q"] & out["jv_prior"].notna()
    out["below_jv"] = out["has_jv_prior"] & (out["q_price"] < out["jv_prior"])  # outcome B
    return out


# ── Evaluation ───────────────────────────────────────────────────────────────

def two_proportion_ztest(x1, n1, x2, n2):
    if n1 == 0 or n2 == 0:
        return np.nan, np.nan
    p_pool = (x1 + x2) / (n1 + n2)
    se = np.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return np.nan, np.nan
    z = (x1 / n1 - x2 / n2) / se
    return z, 2 * (1 - stats.norm.cdf(abs(z)))


def eval_rank(pop: pd.DataFrame, col: str, outcome: str, higher_is_better: bool = True) -> dict | None:
    df = pop.dropna(subset=[col])
    n = len(df)
    if n < 50:
        return None
    rng = np.random.default_rng(TIE_BREAK_SEED)
    df = df.assign(_tb=rng.random(n)).sort_values([col, "_tb"], ascending=[not higher_is_better, True])
    top, bottom = df.head(n // 10), df.tail(n // 2)
    boundary = top[col].iloc[-1]
    tie_share = (df[col] == boundary).mean()
    base = df[outcome].mean()
    z, p = two_proportion_ztest(top[outcome].sum(), len(top), bottom[outcome].sum(), len(bottom))
    return dict(method="top10 vs bottom50", n=n, base_rate=base,
                hi_rate=top[outcome].mean(), hi_n=len(top),
                lo_rate=bottom[outcome].mean(), lo_n=len(bottom),
                lift=top[outcome].mean() / base if base else np.nan,
                z=z, p=p, boundary_tie_share=tie_share)


def eval_group(pop: pd.DataFrame, flag: str, outcome: str) -> dict | None:
    df = pop.dropna(subset=[flag])
    g1, g0 = df[df[flag].astype(bool)], df[~df[flag].astype(bool)]
    if len(g1) < 20 or len(g0) < 20:
        return None
    base = df[outcome].mean()
    z, p = two_proportion_ztest(g1[outcome].sum(), len(g1), g0[outcome].sum(), len(g0))
    return dict(method=f"{flag} vs not", n=len(df), base_rate=base,
                hi_rate=g1[outcome].mean(), hi_n=len(g1),
                lo_rate=g0[outcome].mean(), lo_n=len(g0),
                lift=g1[outcome].mean() / base if base else np.nan,
                z=z, p=p, boundary_tie_share=np.nan)


SIGNAL_VIEWS = [
    ("composite",           "rank",  "composite_score"),
    ("pct_spread",          "rank",  "pct_spread"),
    ("equity_proxy",        "rank",  "equity_proxy"),
    ("absentee",            "group", "absentee"),
    ("tenure: no sale since 2015", "group", "no_sale_on_record"),
    ("tenure: years, among parcels with a sale on record", "rank_has_sale", "ownership_tenure_years"),
]

OUTCOMES = [
    ("discount",   "sold_ce",      "discounted"),
    ("A_any_sale", None,           "sold_q"),
    ("B_below_jv", "has_jv_prior", "below_jv"),
]


def evaluate(pop: pd.DataFrame, pop_label: str, year: int) -> list[dict]:
    rows = []
    for out_label, restrict, outcome in OUTCOMES:
        base_pop = pop[pop[restrict]] if restrict else pop
        for sig_label, kind, col in SIGNAL_VIEWS:
            if kind == "rank":
                r = eval_rank(base_pop, col, outcome)
            elif kind == "group":
                r = eval_group(base_pop, col, outcome)
            else:  # rank among parcels that have a sale on record
                r = eval_rank(base_pop[~base_pop["no_sale_on_record"]], col, outcome)
            if r is not None:
                rows.append(dict(year=year, population=pop_label, outcome=out_label, signal=sig_label, **r))
    return rows


def component_spread_diagnostic(pop: pd.DataFrame) -> pd.DataFrame:
    """How much each component actually moves the composite (outcome-free)."""
    comps = list(COMPOSITE_WEIGHTS.keys())
    d = pop[comps + ["composite_score"]]
    return pd.DataFrame({
        "std": d[comps].std(),
        "iqr": d[comps].quantile(0.75) - d[comps].quantile(0.25),
        "spearman_with_composite": [d[c].corr(d["composite_score"], method="spearman") for c in comps],
        "coverage": d[comps].notna().mean(),
    })


# ── One as-of year ───────────────────────────────────────────────────────────

def score_year(Y: int, comp_events, snaps, all_sales, normalization: str = "minmax") -> pd.DataFrame:
    as_of = pd.Timestamp(f"{Y}-01-01")
    subjects = harv.subjects_as_of(as_of, snaps)
    scored = subjects.join(harv.estimate_historical_arv(subjects, as_of, comp_events))
    scored = harv.add_spread_columns(scored)
    scored = compute_historical_signals(as_of, scored, all_sales)
    scored = build_composite_score(scored, normalization=normalization)
    return attach_outcomes(scored, as_of, all_sales, snaps)


def populations(scored: pd.DataFrame) -> dict[str, pd.DataFrame]:
    default = scored[scored["high_confidence"] & (scored["spread_estimate"] >= harv.DEFAULT_MIN_DOLLAR_SPREAD)]
    ungated = scored[scored["arv_estimate"].notna()]  # any confidence, no $ floor, but must HAVE an ARV
    return {"default": default, "ungated": ungated}


if __name__ == "__main__":
    comp_events = harv.load_comp_events()
    snaps = pd.read_parquet(SNAPSHOTS_PARQUET)
    all_sales = pd.read_parquet(SALES_PARQUET)

    all_rows = []
    for Y in [2018, 2019, 2020, 2021]:
        scored = score_year(Y, comp_events, snaps, all_sales)
        pops = populations(scored)
        print(f"\n=== as-of {Y}-01-01: subjects={len(scored):,}  default pop={len(pops['default']):,}  "
              f"ungated pop={len(pops['ungated']):,}  |  any qualified sale in {OUTCOME_WINDOW_MONTHS}mo: "
              f"{scored['sold_q'].mean()*100:.2f}%  |  absentee share (default)={pops['default']['absentee'].mean()*100:.1f}%  "
              f"no-sale-since-2015 share (default)={pops['default']['no_sale_on_record'].mean()*100:.1f}%")
        print("composite component diagnostic (default pop):")
        print(component_spread_diagnostic(pops["default"]).round(3).to_string())
        for label, pop in pops.items():
            all_rows.extend(evaluate(pop, label, Y))

    res = pd.DataFrame(all_rows)
    res.to_csv(RESULTS_CSV, index=False)
    print(f"\nWrote {len(res)} result rows -> {RESULTS_CSV}")
