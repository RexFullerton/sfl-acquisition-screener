"""
historical_arv.py — As-of, multi-year comp-based ARV engine for back-testing.

Built on historical_sales.py's outputs (data/historical_sales.parquet,
data/nal_snapshots.parquet). This is a SEPARATE engine from arv.py, which
stays as the production, single-snapshot ("now") engine the Streamlit app
uses. They're kept apart deliberately: time-appropriate comps need a
per-vintage NAL characteristic lookup that arv.py's single-roll design
doesn't have, and back-testing needs a strict as-of date that "now" doesn't.
See README for the reasoning.

Design decisions, made explicit per project convention:

- Same-vintage comp rule: every comp's living_area/year_built/nbrhd_cd come
  from the NAL vintage covering the comp's OWN sale year (that year's Jan-1
  assessment), never from today's roll. This is what actually fixes the
  renovation-bias problem the README has flagged since before real
  multi-year data existed.
- Subject as-of characteristics: for as_of = Jan 1 of year Y, the subject's
  own characteristics come from vintage Y-1. DOR's own publication schedule
  (preliminary July 1, initial final October, final after VAB) means year
  Y's own final roll is not actually certified until Y's fall — it was not
  public information as of Jan 1 of year Y. Using vintage Y-1 is the
  defensible, conservative choice; it does mean the subject's characteristics
  can lag the as-of date by close to a year, which is a real limitation, not
  hidden here.
- Comp-eligible sales only: qualified (QUAL_CD 01/02) AND NOT flagged
  MULTI_PAR_SAL (price covers a parcel bundle, not the one parcel) — see
  historical_sales.py and the README's "Qualification codes" section.
- Time adjustment: a COUNTY-WIDE (not neighborhood-specific) monthly $/sqft
  index, rebuilt FRESH for every as-of date D from ONLY comp-eligible sales
  dated strictly before D — never a single global index reused across dates.
  Complete prior years are anchored at July 1 (their full-year median);
  interpolation happens only BETWEEN such anchors, never past the last one.
  If D falls after the last complete-year anchor (e.g. D = Jan 1 of the
  following year), the index is held FLAT at that last anchor's value rather
  than interpolated toward a future point that doesn't exist yet as of D.
  build_index_asof() asserts that no sale on or after D contributed to the
  index it returns. Deliberately county-wide rather than per-neighborhood: a
  neighborhood-specific index built from the same thin comp pool used for
  that neighborhood's own ARVs would risk circularity (the index and the
  estimate would be leaning on the same handful of sales). A county-wide
  index avoids that at the cost of not capturing neighborhood-specific
  appreciation differences — a real, disclosed trade-off.
  (An earlier version of this module built one global index from the WHOLE
  dataset and interpolated toward it regardless of D -- for D = Jan 1 of
  year Y, that meant the index level AT D depended on the July-Y anchor,
  which is built from year Y's FULL annual median, i.e. months that hadn't
  happened yet as of Jan 1 Y. That was a real lookahead bug, caught before
  Step 4 and fixed here — see the README for the corrected vs. original
  accuracy numbers.)
- Value-band tightening, chosen adaptively per subject rather than by a
  static neighborhood pre-classification: a static "coarse neighborhood"
  threshold was tried first and rejected -- neighborhood-wide $/sqft
  dispersion (unfiltered by area/year) has a median of ~57% across
  Miami-Dade's single-family neighborhoods with enough sales to measure it
  reliably (n>=30), so a fixed cutoff either flagged the large majority of
  neighborhoods "coarse" or missed the pathological ones, depending on where
  it was set. What actually distinguishes a barrier-island-style thin,
  bimodal comp pool from a normal one is per-SUBJECT, not a fixed property
  of the neighborhood code. So for every subject, two candidate comp sets
  are built: the normal area/year-tolerance set, and the same set further
  restricted to comps in the subject's own just-value tercile (terciles
  computed from that neighborhood's own pre-as-of comp pool -- no
  lookahead). Whichever of the two has lower dispersion is used, provided it
  still clears the minimum comp count; the value-banded set is discarded if
  it doesn't have enough comps on its own. `classify_coarse_neighborhoods()`
  is kept as a reporting/diagnostic utility (used to characterize which
  neighborhoods are structurally high-dispersion, e.g. for the "how much did
  barrier-island dominance drop" comparison), not as a gate on the ARV
  computation itself.
- assert_no_lookahead() is called on every comp pool actually used to score,
  every time. A leaking back-test is worse than none.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from historical_sales import assert_no_lookahead

SALES_PARQUET = Path("data/historical_sales.parquet")
SNAPSHOTS_PARQUET = Path("data/nal_snapshots.parquet")

LIVING_AREA_TOLERANCE = 0.20
YEAR_BUILT_TOLERANCE = 15
RECENCY_WINDOW_MONTHS = 36
MIN_COMPS_DEFAULT = 5
MIN_COMPS_COARSE = 10
IQR_MULTIPLIER = 1.5
COARSE_DISPERSION_THRESHOLD = 0.35
COARSE_MIN_SAMPLE = 30
BASE_YEAR_FOR_INDEX = 2015

CONFIDENCE_MIN_COMPS = 8
CONFIDENCE_MAX_DISPERSION_PCT = 30.0


# ── Comp events: qualified single-family sales joined to their OWN vintage ──

def load_comp_events() -> pd.DataFrame:
    sales = pd.read_parquet(SALES_PARQUET)
    sales = sales[sales["comp_eligible"] & (sales["dor_use_code"] == "001")].copy()
    n_sales = len(sales)

    snaps = pd.read_parquet(SNAPSHOTS_PARQUET)  # already single-family filtered
    merged = sales.merge(
        snaps[["parcel_id", "vintage_year", "nbrhd_cd", "living_area", "year_built", "just_value"]],
        left_on=["parcel_id", "sale_year"], right_on=["parcel_id", "vintage_year"],
        how="inner",
    )
    dropped = n_sales - len(merged)
    print(f"Comp events: {n_sales:,} qualified single-family sales -> {len(merged):,} joined to "
          f"their own sale-year NAL characteristics ({dropped:,} dropped -- no same-year snapshot, "
          f"e.g. parcel didn't exist yet under DOR_UC=001 that year).")

    merged = merged[merged["living_area"] > 0].copy()
    merged["price_per_sqft"] = merged["price"] / merged["living_area"]
    return merged


# ── County-wide time-adjustment index, rebuilt per as-of date ──────────────

def build_index_asof(comp_events: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series:
    """
    Monthly $/sqft index usable for as-of date `as_of`, built ONLY from
    comp-eligible sales dated strictly before `as_of`. Complete prior years
    are anchored at July 1 (full-year median) and interpolated between;
    anything after the last anchor is held flat, never extrapolated forward.
    """
    pre = comp_events[comp_events["sale_date"] < as_of]
    if len(pre) == 0:
        raise ValueError(f"No comp-eligible sales before {as_of.date()} -- cannot build an index.")
    max_sale_used = pre["sale_date"].max()
    if max_sale_used >= as_of:
        raise AssertionError(  # should be structurally impossible given the filter above; belt-and-suspenders
            f"Index for as_of={as_of.date()} would depend on a sale dated {max_sale_used.date()}, "
            f"which is not strictly before as_of."
        )

    annual = pre.groupby("sale_year")["price_per_sqft"].median().sort_index()
    complete_years = [y for y in annual.index if y < as_of.year]

    anchor_dates = list(pd.to_datetime({"year": complete_years, "month": 7, "day": 1}))
    anchor_vals = list(annual.loc[complete_years].values)

    if as_of.year in annual.index:
        # Partial current-year data exists (as_of isn't Jan 1) -- anchor it at
        # the day before as_of using that partial year-to-date median, which
        # is the most recent honest point available.
        anchor_dates.append(as_of - pd.Timedelta(days=1))
        anchor_vals.append(annual.loc[as_of.year])

    if not anchor_dates:
        raise ValueError(f"No complete-year data available before {as_of.date()} to anchor an index.")

    s = pd.Series(anchor_vals, index=pd.DatetimeIndex(anchor_dates)).sort_index()
    assert (s.index < as_of).all(), "Index anchor at or after as_of -- lookahead."

    query_point = as_of - pd.Timedelta(days=1)
    full_range = pd.date_range(s.index.min(), query_point, freq="MS")
    combined = s.index.union(full_range).union(pd.DatetimeIndex([query_point]))
    s_full = s.reindex(combined).sort_index()
    s_full = s_full.interpolate(method="time", limit_area="inside")  # only between known anchors
    s_full = s_full.ffill()  # hold flat past the last anchor -- never extrapolate toward the future
    s_full = s_full.bfill()  # safety net; full_range starts at the first anchor so this is a no-op in practice

    assert (s_full.index < as_of).all(), "Index contains a point at or after as_of -- lookahead."
    return s_full


def _index_level_at(monthly_index: pd.Series, dates) -> np.ndarray:
    idx_days = monthly_index.index.values.astype("datetime64[D]").astype(float)
    idx_vals = monthly_index.values.astype(float)
    d_days = pd.to_datetime(dates).values.astype("datetime64[D]").astype(float)
    # Beyond the index's own range, hold flat (no extrapolation) rather than
    # letting np.interp silently clip to the boundary value -- functionally
    # the same result for a flat-carry design, made explicit here.
    return np.interp(d_days, idx_days, idx_vals, left=idx_vals[0], right=idx_vals[-1])


# ── Coarse-neighborhood classification ──────────────────────────────────────

def classify_coarse_neighborhoods(comp_events: pd.DataFrame, threshold: float = COARSE_DISPERSION_THRESHOLD):
    def _iqr_ratio(s):
        med = s.median()
        if not med:
            return np.nan
        q1, q3 = s.quantile(0.25), s.quantile(0.75)
        return (q3 - q1) / med

    stats = comp_events.groupby("nbrhd_cd")["price_per_sqft"].agg(n="count", median="median")
    stats["iqr_ratio"] = comp_events.groupby("nbrhd_cd")["price_per_sqft"].apply(_iqr_ratio)
    coarse = set(stats[(stats["iqr_ratio"] >= threshold) & (stats["n"] >= COARSE_MIN_SAMPLE)].index)
    return coarse, stats


# ── As-of ARV ────────────────────────────────────────────────────────────────

def _trim_iqr(vals: np.ndarray) -> np.ndarray:
    if len(vals) == 0:
        return vals
    q1, q3 = np.percentile(vals, [25, 75])
    iqr = q3 - q1
    if iqr <= 0:
        return vals
    lo, hi = q1 - IQR_MULTIPLIER * iqr, q3 + IQR_MULTIPLIER * iqr
    return vals[(vals >= lo) & (vals <= hi)]


def _dispersion(vals: np.ndarray) -> float:
    if len(vals) < 2:
        return np.inf
    q1, q3 = np.percentile(vals, [25, 75])
    med = np.median(vals)
    return (q3 - q1) / med if med else np.inf


def estimate_historical_arv(
    subjects: pd.DataFrame,
    as_of: pd.Timestamp,
    comp_events: pd.DataFrame,
    chunk_size: int = 1500,
) -> pd.DataFrame:
    """
    subjects: columns parcel_id, nbrhd_cd, living_area, year_built, just_value
              (already the caller's chosen as-of vintage -- Y-1 for as_of=Jan1,Y)
    Returns a DataFrame indexed like `subjects`: arv_estimate, arv_comp_count,
    arv_dispersion_pct, used_value_band, high_confidence.
    """
    window_start = as_of - pd.DateOffset(months=RECENCY_WINDOW_MONTHS)
    pool = comp_events[(comp_events["sale_date"] >= window_start) & (comp_events["sale_date"] < as_of)].copy()
    assert_no_lookahead(pool, as_of)  # hard fail on any leakage, by construction should never trip

    # Index rebuilt fresh from ONLY pre-as_of comp-eligible sales (not the
    # 36-month-windowed `pool` -- the index needs full-year anchors further
    # back than the comp window to interpolate/hold-flat correctly).
    monthly_index = build_index_asof(comp_events, as_of)
    idx_asof = _index_level_at(monthly_index, [as_of])[0]
    pool["adj_psf"] = pool["price_per_sqft"] * (idx_asof / _index_level_at(monthly_index, pool["sale_date"]))

    n = len(subjects)
    arv_estimate = np.full(n, np.nan)
    arv_comp_count = np.zeros(n, dtype=int)
    arv_dispersion_pct = np.full(n, np.nan)
    used_value_band = np.zeros(n, dtype=bool)

    subj_area = subjects["living_area"].to_numpy(dtype=float)
    subj_year = subjects["year_built"].to_numpy(dtype=float)
    subj_jv = subjects["just_value"].to_numpy(dtype=float)
    subj_nbrhd = subjects["nbrhd_cd"].to_numpy()
    subj_pid = subjects["parcel_id"].to_numpy()

    ev_nbrhd = pool["nbrhd_cd"].to_numpy()
    ev_area = pool["living_area"].to_numpy(dtype=float)
    ev_year = pool["year_built"].to_numpy(dtype=float)
    ev_jv = pool["just_value"].to_numpy(dtype=float)
    ev_psf = pool["adj_psf"].to_numpy(dtype=float)
    ev_pid = pool["parcel_id"].to_numpy()

    for nbrhd in pd.unique(subj_nbrhd):
        subj_positions = np.flatnonzero(subj_nbrhd == nbrhd)
        comp_mask = ev_nbrhd == nbrhd
        if not comp_mask.any():
            continue
        c_area, c_year, c_jv, c_psf, c_pid = (
            ev_area[comp_mask], ev_year[comp_mask], ev_jv[comp_mask], ev_psf[comp_mask], ev_pid[comp_mask],
        )
        can_band = len(c_jv) >= 15  # enough in the neighborhood pool to form meaningful terciles
        tercile_cuts = np.nanpercentile(c_jv, [33.3, 66.7]) if can_band else None
        c_tercile = np.digitize(c_jv, tercile_cuts) if can_band else None

        for start in range(0, len(subj_positions), chunk_size):
            chunk_pos = subj_positions[start:start + chunk_size]
            a = subj_area[chunk_pos][:, None]
            y = subj_year[chunk_pos][:, None]
            pid = subj_pid[chunk_pos][:, None]

            area_ok = (c_area[None, :] >= a * (1 - LIVING_AREA_TOLERANCE)) & (c_area[None, :] <= a * (1 + LIVING_AREA_TOLERANCE))
            year_ok = (c_year[None, :] >= y - YEAR_BUILT_TOLERANCE) & (c_year[None, :] <= y + YEAR_BUILT_TOLERANCE)
            self_ok = c_pid[None, :] != pid
            base_mask = area_ok & year_ok & self_ok

            if can_band:
                subj_tercile = np.digitize(subj_jv[chunk_pos], tercile_cuts)
                band_mask = base_mask & (c_tercile[None, :] == subj_tercile[:, None])

            for row, pos in enumerate(chunk_pos):
                unconstrained = _trim_iqr(c_psf[base_mask[row]])
                candidate = unconstrained
                banded_used = False

                if can_band:
                    banded = _trim_iqr(c_psf[band_mask[row]])
                    if len(banded) >= MIN_COMPS_DEFAULT and _dispersion(banded) < _dispersion(unconstrained):
                        candidate = banded
                        banded_used = True

                if len(candidate) < MIN_COMPS_DEFAULT:
                    arv_comp_count[pos] = len(candidate)
                    continue

                median_psf = np.median(candidate)
                q1, q3 = np.percentile(candidate, [25, 75])
                arv_estimate[pos] = round(median_psf * subj_area[pos], 0)
                arv_comp_count[pos] = len(candidate)
                arv_dispersion_pct[pos] = round((q3 - q1) / median_psf * 100, 1) if median_psf else np.nan
                used_value_band[pos] = banded_used

    out = pd.DataFrame({
        "arv_estimate": arv_estimate,
        "arv_comp_count": arv_comp_count,
        "arv_dispersion_pct": arv_dispersion_pct,
        "used_value_band": used_value_band,
    }, index=subjects.index)
    out["high_confidence"] = (out["arv_comp_count"] >= CONFIDENCE_MIN_COMPS) & (out["arv_dispersion_pct"] <= CONFIDENCE_MAX_DISPERSION_PCT)
    out.loc[out["arv_estimate"].isna(), "high_confidence"] = False
    return out


MAO_RULE = 0.70
DEFAULT_MIN_DOLLAR_SPREAD = 25_000


def add_spread_columns(scored: pd.DataFrame) -> pd.DataFrame:
    """
    spread_estimate ($) = ARV x 70% - just_value, same basis as the production
    MAO calculator. pct_spread = spread_estimate / arv_estimate -- the default
    RANKING metric (a $2M spread on a $20M ARV and a $50K spread on a $200K
    ARV are very different opportunities; pct_spread treats them comparably,
    dollar spread alone favors expensive homes regardless of confidence).
    """
    out = scored.copy()
    out["spread_estimate"] = out["arv_estimate"] * MAO_RULE - out["just_value"]
    out["pct_spread"] = out["spread_estimate"] / out["arv_estimate"]
    return out


def default_ranked(scored: pd.DataFrame, min_dollar_spread: float = DEFAULT_MIN_DOLLAR_SPREAD,
                    require_high_confidence: bool = True) -> pd.DataFrame:
    """
    The default view: high_confidence only (unless explicitly turned off),
    a minimum dollar-spread floor to keep trivial spreads out, sorted by
    pct_spread descending. Dollar spread stays available as a column for
    anyone who wants to sort by it instead -- it's not hidden, just not the
    default sort key.
    """
    df = add_spread_columns(scored)
    df = df[df["spread_estimate"] >= min_dollar_spread]
    if require_high_confidence:
        df = df[df["high_confidence"]]
    return df.sort_values("pct_spread", ascending=False)


# ── Subject as-of characteristics ───────────────────────────────────────────

def subjects_as_of(as_of: pd.Timestamp, snapshots: pd.DataFrame) -> pd.DataFrame:
    """
    All single-family parcels as they existed in vintage (as_of.year - 1) --
    the latest FINAL roll actually published before as_of, per the module
    docstring's reasoning.
    """
    vintage = as_of.year - 1
    subj = snapshots[snapshots["vintage_year"] == vintage].copy()
    subj = subj[subj["living_area"].gt(0) & subj["year_built"].gt(0)]
    return subj.reset_index(drop=True)
