"""
arv.py — Comparable-sales ARV estimator, parcel grain.

For each subject parcel, finds comparable arm's-length single-family sales
and applies their $/sqft to the subject's living area. No typed-in numbers,
no synthetic data — every comp is a real recorded sale from the NAL file.

Design decisions (deliberately explicit, not buried):

  - Geography unit: NBRHD_CD, the property appraiser's own valuation
    neighborhood code — not ZIP (too coarse; a ZIP can span very different
    submarkets) and not a lat/lon radius (the NAL file carries no
    coordinates, so a true radius would require introducing an external
    geocoding dependency, which is out of scope for this pass).
  - Living-area tolerance: +/-20% of the subject's sqft.
  - Year-built tolerance: +/-15 years.
  - Recency window: trailing 24 months, ending at `as_of`.
  - Minimum comp count: 5. Below that, arv_estimate is null — no guess.
  - Price/sqft: median of the comp set, after dropping outliers outside
    1.5x IQR on $/sqft (guards against a single data-entry error or an
    atypical sale skewing a small comp pool).
  - No time-adjustment for market movement across the comp window. A
    defensible adjustment would need a repeat-sales price index built from
    more data than we have here — this is a documented limitation, not an
    oversight. The 24-month window is the only control on comp staleness.

CRITICAL for back-test use: NAL orders sales newest-first — SALE_1 is the
most recent sale, SALE_2 is the one before it. For a completed flip, SALE_2
is the acquisition and SALE_1 is the exit/outcome sale. `as_of` must be set
to the ACQUISITION date (SALE_2's date), never SALE_1's — every comp sale
must have occurred strictly before `as_of`, so the subject's own exit sale
(SALE_1, which is later) and its own acquisition transaction (SALE_2, at
exactly `as_of`) are both excluded by construction. See backtest.py.
"""

import numpy as np
import pandas as pd

LIVING_AREA_TOLERANCE = 0.20     # +/- 20%
YEAR_BUILT_TOLERANCE = 15        # +/- 15 years
RECENCY_WINDOW_MONTHS = 24
MIN_COMPS = 5
IQR_MULTIPLIER = 1.5

ARMS_LENGTH_QUAL_CODES = {"01", "02"}


def _month_start(year: pd.Series, month: pd.Series) -> pd.Series:
    """Build a Timestamp from separate year/month columns; invalid -> NaT."""
    month_clamped = month.where(month.between(1, 12))
    return pd.to_datetime(
        {"year": year, "month": month_clamped, "day": 1}, errors="coerce"
    )


def _build_sale_events(parcels: pd.DataFrame) -> pd.DataFrame:
    """Melt each parcel's two sale slots into one row per (arms-length) sale event."""
    events = []
    for suffix in ("1", "2"):
        price_col, year_col, month_col, qual_col = (
            f"sale{suffix}_price", f"sale{suffix}_year", f"sale{suffix}_month", f"sale{suffix}_qual_cd",
        )
        e = pd.DataFrame({
            "parcel_id": parcels["parcel_id"],
            "nbrhd_cd": parcels["nbrhd_cd"],
            "living_area": parcels["living_area"],
            "year_built": parcels["year_built"],
            "sale_price": pd.to_numeric(parcels[price_col], errors="coerce"),
            "sale_date": _month_start(
                pd.to_numeric(parcels[year_col], errors="coerce"),
                pd.to_numeric(parcels[month_col], errors="coerce"),
            ),
            "qual_cd": parcels[qual_col],
        })
        events.append(e)
    events = pd.concat(events, ignore_index=True)
    events = events[
        events["qual_cd"].isin(ARMS_LENGTH_QUAL_CODES)
        & events["sale_price"].gt(0)
        & events["sale_date"].notna()
        & events["living_area"].gt(0)
    ].copy()
    events["price_per_sqft"] = events["sale_price"] / events["living_area"]
    return events


def _trim_outliers_np(values: np.ndarray) -> np.ndarray:
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    if iqr == 0:
        return values
    lo, hi = q1 - IQR_MULTIPLIER * iqr, q3 + IQR_MULTIPLIER * iqr
    return values[(values >= lo) & (values <= hi)]


def estimate_arv(parcels: pd.DataFrame, as_of: pd.Timestamp | None = None, chunk_size: int = 1500) -> pd.DataFrame:
    """
    Return a DataFrame indexed like `parcels` with columns:
      arv_estimate, arv_comp_count, arv_dispersion_pct (IQR / median $/sqft, in %)
    All three are null where the comp pool doesn't meet MIN_COMPS.

    `as_of`: comps must have sold in [as_of - 24mo, as_of). Defaults to the
    latest sale date present in the data (i.e. "now", for production use).

    Implemented as numpy-vectorized comparisons, chunked per neighborhood,
    rather than a per-row pandas loop — a naive per-parcel loop over 384K+
    single-family parcels was minutes-to-hours slow; this runs in seconds.
    """
    events = _build_sale_events(parcels)

    if as_of is None:
        as_of = events["sale_date"].max() + pd.DateOffset(months=1)

    window_start = as_of - pd.DateOffset(months=RECENCY_WINDOW_MONTHS)
    events = events[(events["sale_date"] >= window_start) & (events["sale_date"] < as_of)]

    n = len(parcels)
    arv_estimate = np.full(n, np.nan)
    arv_comp_count = np.zeros(n, dtype=int)
    arv_dispersion_pct = np.full(n, np.nan)

    subj_area = parcels["living_area"].to_numpy(dtype=float)
    subj_year = parcels["year_built"].to_numpy(dtype=float)
    subj_nbrhd = parcels["nbrhd_cd"].to_numpy()
    subj_pid = parcels["parcel_id"].to_numpy()

    ev_nbrhd = events["nbrhd_cd"].to_numpy()
    ev_area = events["living_area"].to_numpy(dtype=float)
    ev_year = events["year_built"].to_numpy(dtype=float)
    ev_psf = events["price_per_sqft"].to_numpy(dtype=float)
    ev_pid = events["parcel_id"].to_numpy()

    for nbrhd in pd.unique(subj_nbrhd):
        subj_positions = np.flatnonzero(subj_nbrhd == nbrhd)
        comp_mask = ev_nbrhd == nbrhd
        if not comp_mask.any():
            continue
        c_area, c_year, c_psf, c_pid = ev_area[comp_mask], ev_year[comp_mask], ev_psf[comp_mask], ev_pid[comp_mask]

        for start in range(0, len(subj_positions), chunk_size):
            chunk_pos = subj_positions[start:start + chunk_size]
            a = subj_area[chunk_pos][:, None]
            y = subj_year[chunk_pos][:, None]
            pid = subj_pid[chunk_pos][:, None]

            area_ok = (c_area[None, :] >= a * (1 - LIVING_AREA_TOLERANCE)) & (c_area[None, :] <= a * (1 + LIVING_AREA_TOLERANCE))
            year_ok = (c_year[None, :] >= y - YEAR_BUILT_TOLERANCE) & (c_year[None, :] <= y + YEAR_BUILT_TOLERANCE)
            self_ok = c_pid[None, :] != pid
            mask = area_ok & year_ok & self_ok  # (chunk, n_comps)

            for row, pos in enumerate(chunk_pos):
                vals = c_psf[mask[row]]
                if len(vals) == 0:
                    continue
                trimmed = _trim_outliers_np(vals)
                if len(trimmed) < MIN_COMPS:
                    arv_comp_count[pos] = len(trimmed)
                    continue
                median_psf = np.median(trimmed)
                q1, q3 = np.percentile(trimmed, [25, 75])
                arv_estimate[pos] = round(median_psf * subj_area[pos], 0)
                arv_comp_count[pos] = len(trimmed)
                arv_dispersion_pct[pos] = round((q3 - q1) / median_psf * 100, 1) if median_psf else np.nan

    out = pd.DataFrame({
        "arv_estimate": arv_estimate,
        "arv_comp_count": arv_comp_count,
        "arv_dispersion_pct": arv_dispersion_pct,
    }, index=parcels.index)
    out.loc[out["arv_estimate"].isna(), "arv_estimate"] = pd.NA
    out.loc[out["arv_dispersion_pct"].isna(), "arv_dispersion_pct"] = pd.NA
    return out


def mao(arv: float, repair_cost: float, rule: float = 0.70) -> float:
    """Maximum Allowable Offer = (ARV x rule) - repair_cost."""
    return max(0.0, arv * rule - repair_cost)
