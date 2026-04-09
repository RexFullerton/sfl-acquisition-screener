"""
scorer.py — Zip code scoring model for SF Wholesale Deal Finder.

Signals are min-max normalized across the loaded dataset so scores
reflect relative rank within the market, not absolute thresholds.
"""

import pandas as pd

# ── Default signal weights (raw points; auto-normalized internally) ────────────
DEFAULT_WEIGHTS: dict[str, int] = {
    "probate_activity_rate": 30,
    "absentee_owner_rate":   25,
    "tax_delinquency_rate":  20,
    "preforeclosure_rate":   15,
    "avg_years_ownership":   10,
}

SIGNAL_LABELS: dict[str, str] = {
    "probate_activity_rate": "Probate / Inherited Activity",
    "absentee_owner_rate":   "Absentee / Out-of-State Owner Rate",
    "tax_delinquency_rate":  "Tax Delinquency Rate",
    "preforeclosure_rate":   "Pre-Foreclosure Filing Rate",
    "avg_years_ownership":   "Avg Years of Ownership",
}

# Grade colour palette (Bootstrap-inspired, readable on white)
GRADE_BG: dict[str, str] = {
    "A": "#d4edda",  # green
    "B": "#d1ecf1",  # teal/cyan
    "C": "#fff3cd",  # amber
    "D": "#fde8d8",  # orange
    "F": "#f8d7da",  # red
}

GRADE_FG: dict[str, str] = {
    "A": "#155724",
    "B": "#0c5460",
    "C": "#856404",
    "D": "#7d2e00",
    "F": "#721c24",
}


# ── Core scoring ───────────────────────────────────────────────────────────────

def _normalize(df: pd.DataFrame, signals: list[str]) -> pd.DataFrame:
    """Min-max scale each signal to [0, 100] across the dataframe."""
    df = df.copy()
    for col in signals:
        lo, hi = df[col].min(), df[col].max()
        if hi > lo:
            df[f"{col}_norm"] = (df[col] - lo) / (hi - lo) * 100.0
        else:
            df[f"{col}_norm"] = 50.0  # degenerate: every row tied
    return df


def score_zip_codes(df: pd.DataFrame, weights: dict[str, int]) -> pd.DataFrame:
    """
    Return a copy of *df* with two additional columns:
      - total_score : weighted composite, 0–100
      - grade       : letter grade A–F
    """
    signals = list(weights.keys())
    df = _normalize(df, signals)

    total_w = sum(weights.values()) or 1  # guard against all-zero sliders
    df["total_score"] = sum(
        df[f"{s}_norm"] * (w / total_w) for s, w in weights.items()
    )
    df["grade"] = df["total_score"].map(_to_grade)
    return df


def _to_grade(score: float) -> str:
    if score >= 80:
        return "A"
    elif score >= 65:
        return "B"
    elif score >= 50:
        return "C"
    elif score >= 35:
        return "D"
    return "F"


# ── MAO calculator ─────────────────────────────────────────────────────────────

def mao(arv: float, repair_cost: float, rule: float = 0.70) -> float:
    """Maximum Allowable Offer = (ARV × rule) − repair_cost."""
    return max(0.0, arv * rule - repair_cost)
