"""
scorer.py — Parcel-level motivation score + spread ranking.

Two outputs, deliberately kept separate:
  - motivation_score (0-100): a weighted composite of the three real distress
    signals (absentee ownership, ownership tenure, equity proxy). This is a
    "how likely is this owner motivated to sell" signal.
  - spread_estimate ($): ARV x 0.70 (the same 70%-rule basis as the MAO
    calculator) minus current just_value, used as a rough stand-in for
    "how much room is there to profit." This is a "is this a good deal"
    signal, not a motivation signal.

The ranked leaderboard sorts by spread_estimate (that's what answers "which
properties should I make an offer on"), with motivation_score exposed as a
filterable column, not folded into one opaque number. See README for why
these are kept apart.

Probate was removed entirely — Phase 1 found it has no ingestion in this
codebase, and carrying it as a placeholder-zero signal previously compressed
the score distribution into 26 "A" / 184 "B" / 0 "C" out of 223 rows by
contributing a constant value to every row. A signal that isn't measured
isn't scored; it's just absent.
"""

import numpy as np
import pandas as pd

DEFAULT_WEIGHTS: dict[str, int] = {
    "absentee_score": 40,
    "tenure_score": 30,
    "equity_proxy_score": 30,
}

SIGNAL_LABELS: dict[str, str] = {
    "absentee_score": "Absentee / Out-of-State Owner",
    "tenure_score": "Ownership Tenure",
    "equity_proxy_score": "Equity Proxy",
}

GRADE_BG: dict[str, str] = {
    "A": "#d4edda", "B": "#d1ecf1", "C": "#fff3cd", "D": "#fde8d8", "F": "#f8d7da",
}
GRADE_FG: dict[str, str] = {
    "A": "#155724", "B": "#0c5460", "C": "#856404", "D": "#7d2e00", "F": "#721c24",
}

# equity_proxy is winsorized to this percentile band before min-max scaling —
# a handful of extreme parcels (very high JV vs. a decades-old, tiny recorded
# sale price) would otherwise collapse everyone else's normalized score near 0.
EQUITY_PROXY_WINSOR_PCT = (0.05, 0.95)

MAO_RULE = 0.70


def _minmax(series: pd.Series) -> pd.Series:
    lo, hi = series.min(), series.max()
    if pd.isna(lo) or pd.isna(hi) or hi <= lo:
        return pd.Series(np.nan, index=series.index)
    return (series - lo) / (hi - lo) * 100.0


def score_parcels(parcels: pd.DataFrame, weights: dict[str, int]) -> pd.DataFrame:
    """
    Adds motivation_score, grade, and spread_estimate to a copy of `parcels`.
    `parcels` must already carry arv_estimate (see arv.estimate_arv).

    Missing signals are excluded from a row's weighted average and the
    remaining weights are renormalized for that row — never zero-filled or
    defaulted to a constant. A row with no usable signals gets a null score.
    """
    df = parcels.copy()

    df["absentee_score"] = df["absentee"].astype(float) * 100.0
    df["tenure_score"] = _minmax(df["ownership_tenure_years"])

    equity = pd.to_numeric(df["equity_proxy"], errors="coerce")
    lo, hi = equity.quantile(EQUITY_PROXY_WINSOR_PCT[0]), equity.quantile(EQUITY_PROXY_WINSOR_PCT[1])
    equity_winsorized = equity.clip(lo, hi)
    df["equity_proxy_score"] = _minmax(equity_winsorized)
    df.loc[equity.isna(), "equity_proxy_score"] = np.nan  # winsorizing must not manufacture a value where there was none

    signal_cols = list(weights.keys())
    weight_arr = np.array([weights[c] for c in signal_cols], dtype=float)
    values = df[signal_cols].to_numpy(dtype=float)
    present = ~np.isnan(values)

    weighted_sum = np.nansum(values * weight_arr, axis=1)
    weight_total = (present * weight_arr).sum(axis=1)

    with np.errstate(invalid="ignore", divide="ignore"):
        motivation_score = np.where(weight_total > 0, weighted_sum / weight_total, np.nan)
    df["motivation_score"] = motivation_score
    df["grade"] = df["motivation_score"].map(_to_grade)

    arv = pd.to_numeric(df["arv_estimate"], errors="coerce")
    df["mao_estimate"] = arv * MAO_RULE
    df["spread_estimate"] = df["mao_estimate"] - df["just_value"]

    return df


def _to_grade(score: float) -> str:
    if pd.isna(score):
        return "N/A"
    if score >= 80:
        return "A"
    elif score >= 65:
        return "B"
    elif score >= 50:
        return "C"
    elif score >= 35:
        return "D"
    return "F"


def mao(arv: float, repair_cost: float, rule: float = MAO_RULE) -> float:
    """Maximum Allowable Offer = (ARV x rule) - repair_cost."""
    return max(0.0, arv * rule - repair_cost)
