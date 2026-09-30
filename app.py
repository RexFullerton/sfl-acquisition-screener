"""
app.py — South Florida Acquisition Screener
Streamlit UI: parcel-level leaderboard, parcel detail, MAO calculator.

Miami-Dade single-family only (see data_pipeline.py). ARVs come from
historical_arv.py — the same as-of engine the back-test validated — run as of
2026-01-01 against the 2025 Final roll, so the on-screen ranking is the one
the back-test measured, not a different production variant. Owner/mailing
fields and the motivation signals come from data/parcels.csv (2025 NAL).

The ZIP-choropleth map from the earlier ZIP-grain version is removed, not
adapted — the NAL file carries no parcel coordinates.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

import historical_arv as harv
import scorer
from scorer import DEFAULT_WEIGHTS, SIGNAL_LABELS, mao

PARCELS_PATH = Path("data/parcels.csv")
DTYPES = {"parcel_id": str, "nbrhd_cd": str, "sale1_qual_cd": str, "sale2_qual_cd": str}
AS_OF = pd.Timestamp("2026-01-01")
ARV_COLS = ["arv_estimate", "arv_comp_count", "arv_dispersion_pct", "high_confidence"]

st.set_page_config(page_title="SFL Acquisition Screener", page_icon="🏡", layout="wide")


@st.cache_data(show_spinner="Loading parcels and computing as-of ARVs (~1-2 min on first run)…")
def load_scored_parcels() -> pd.DataFrame:
    parcels = pd.read_csv(PARCELS_PATH, dtype=DTYPES)
    snaps = pd.read_parquet(harv.SNAPSHOTS_PARQUET)
    comp_events = harv.load_comp_events()
    subjects = harv.subjects_as_of(AS_OF, snaps)
    arv_res = subjects[["parcel_id", "just_value"]].join(harv.estimate_historical_arv(subjects, AS_OF, comp_events))
    # Spread uses the engine's own 2025F just value (the back-tested definition). parcels.csv
    # was built from an earlier 2025 NAL release whose just value differs on ~6K parcels.
    df = parcels.drop(columns=["just_value"]).merge(arv_res, on="parcel_id", how="inner")
    df["high_confidence"] = df["high_confidence"].fillna(False).astype(bool)
    return harv.add_spread_columns(df)


missing = [p for p in (PARCELS_PATH, harv.SALES_PARQUET, harv.SNAPSHOTS_PARQUET) if not p.exists()]
if missing:
    st.error(
        "Missing data files: " + ", ".join(str(p) for p in missing) + ". "
        "Run `python3 data_pipeline.py` and `python3 historical_sales.py` first (see README)."
    )
    st.stop()

base_df = load_scored_parcels()

if "notes" not in st.session_state:
    st.session_state["notes"] = {}

# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Controls")

    st.subheader("Ranking population")
    high_conf_only = st.checkbox(
        "High-confidence ARVs only", value=True,
        help=f"At least {harv.CONFIDENCE_MIN_COMPS} comps and comp $/sqft dispersion "
             f"≤ {harv.CONFIDENCE_MAX_DISPERSION_PCT:.0f}%. This is the population the back-test evaluated.",
    )
    min_spread = st.number_input(
        "Minimum dollar spread ($)", min_value=0, value=harv.DEFAULT_MIN_DOLLAR_SPREAD, step=5_000,
        help="Spread = ARV × 70% − just value. $25K is the back-tested default.",
    )
    sort_by = st.radio("Sort by", ["% spread (default)", "Dollar spread"], index=0)

    st.markdown("---")
    st.subheader("Filters")
    absentee_only = st.checkbox("Absentee owners only")
    max_just_value = st.slider(
        "Max just (assessed) value ($k)", 100, 5000, 5000, step=50,
        help="At 5000 (the default) there is no cap, so the view matches the back-tested population.",
    )

    st.markdown("---")
    with st.expander("Motivation grade weights (not validated)"):
        st.caption(
            "The grade is a weighted composite of absentee, tenure, and equity proxy. "
            "The back-test found none of these predicts discounted sales — see the note above the leaderboard."
        )
        weights: dict[str, int] = {}
        for signal, label in SIGNAL_LABELS.items():
            weights[signal] = st.slider(label, 0, 100, DEFAULT_WEIGHTS[signal], step=5, key=f"weight_{signal}")
    if sum(weights.values()) == 0:
        st.error("At least one weight must be > 0.")
        st.stop()

# ── Score & filter ───────────────────────────────────────────────────────────

scored = scorer.score_parcels(base_df, weights)
scored["spread_estimate"] = base_df["spread_estimate"]  # same formula; keep the engine's column authoritative

mask = scored["arv_estimate"].notna()
mask &= scored["spread_estimate"] >= min_spread
if max_just_value < 5000:
    mask &= scored["just_value"] <= max_just_value * 1000
if high_conf_only:
    mask &= scored["high_confidence"]
if absentee_only:
    mask &= scored["absentee"]
sort_col = "pct_spread" if sort_by.startswith("%") else "spread_estimate"
filtered = scored[mask].sort_values(sort_col, ascending=False).copy()

# ── Header ───────────────────────────────────────────────────────────────────

st.title("South Florida Acquisition Screener")
st.markdown(
    f"**{len(base_df):,}** Miami-Dade single-family parcels (DOR 2025 Final roll) · "
    f"**{base_df['arv_estimate'].notna().sum():,}** have a comp-based ARV as of {AS_OF.date()} "
    f"(comps: qualified sales in the prior 36 months) · **{len(filtered):,}** match the current view."
)

st.info(
    "**What this ranking has and hasn't been shown to predict.** "
    "Back-tested as of Jan 1 of 2018–2021 on the default view (high-confidence ARV, $25K+ spread), "
    "with outcomes over the following 30 months:\n\n"
    "- **Shown:** among parcels that sold, the top 10% by % spread sold 20%+ below their estimated ARV "
    "22–55% of the time, vs. 3–13% for the bottom half. That discount is measured against this tool's "
    "own ARV, so part of it may be ARV error rather than a real bargain; on a check that doesn't use the "
    "ARV (sale below the prior-year DOR just value), the ranking showed no lift.\n"
    "- **Not shown:** that a high-ranked home is any more likely to sell. The top 10% sold at 7.7–9.1% "
    "vs. 8.5–10.8% for the bottom half.\n"
    "- **Not shown:** that absentee ownership, ownership tenure, or the motivation grade predicts a "
    "discounted sale. Homes with no recorded sale since 2015 were *less* likely to sell.\n\n"
    "This is a screening list, not an underwriting result. See the README for the full back-test."
)
st.divider()

# ── Leaderboard ──────────────────────────────────────────────────────────────

st.subheader("📊 Leaderboard")

COL_MAP = {
    "parcel_id": "Parcel ID", "situs_addr1": "Address", "situs_city": "City",
    "just_value": "Just Value", "arv_estimate": "ARV",
    "pct_spread": "% Spread", "spread_estimate": "Spread ($, 70% rule)",
    "arv_comp_count": "Comps", "arv_dispersion_pct": "Disp %", "high_confidence": "High Conf.",
    "absentee": "Absentee", "out_of_state": "Out-of-State",
    "ownership_tenure_years": "Tenure (yrs)", "grade": "Grade (not validated)",
}

full_board = filtered[list(COL_MAP.keys())].rename(columns=COL_MAP).reset_index(drop=True)

csv_bytes = full_board.to_csv(index=False).encode("utf-8")
st.download_button(
    f"⬇️ Export current view as CSV ({len(full_board):,} rows)",
    csv_bytes, "sfl_screener_parcels.csv", "text/csv",
)

DISPLAY_ROWS = 500
board = full_board.head(DISPLAY_ROWS)
if len(full_board) > DISPLAY_ROWS:
    st.caption(f"Showing the top {DISPLAY_ROWS:,} of {len(full_board):,} matching parcels on screen — export the CSV for the rest.")

styled = board.style.format({
    "Just Value": "${:,.0f}", "ARV": "${:,.0f}", "% Spread": "{:.1%}",
    "Spread ($, 70% rule)": "${:,.0f}", "Disp %": "{:.1f}%", "Tenure (yrs)": "{:.1f}",
}, na_rep="—")
st.dataframe(styled, use_container_width=True, height=560)
st.caption(
    "% spread = (ARV × 70% − just value) ÷ ARV. Just value stands in for acquisition basis because "
    "there is no listing or offer data; it is a proxy, not a price."
)
st.divider()

# ── Parcel Detail ────────────────────────────────────────────────────────────

st.subheader("🔍 Parcel Detail & Notes")

options = board["Parcel ID"].tolist()
row = None
if options:
    selected = st.selectbox(
        "Select a parcel:", options=options,
        format_func=lambda pid: (
            f"{board.loc[board['Parcel ID']==pid,'Address'].iat[0]} — "
            f"{board.loc[board['Parcel ID']==pid,'City'].iat[0]}"
        ),
    )
    row = filtered[filtered["parcel_id"] == selected].iloc[0]

    st.markdown(f"**{row['situs_addr1']}, {row['situs_city']}** — Parcel {row['parcel_id']}")

    a1, a2, a3, a4, a5 = st.columns(5)
    a1.metric("ARV Estimate", f"${row['arv_estimate']:,.0f}")
    a2.metric("Just Value", f"${row['just_value']:,.0f}")
    a3.metric("% Spread", f"{row['pct_spread']:.1%}")
    a4.metric("Comp Count", f"{row['arv_comp_count']:.0f}")
    disp = row["arv_dispersion_pct"]
    a5.metric("Comp Dispersion", f"{disp:.1f}%" if pd.notna(disp) else "—")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Absentee Owner", "Yes" if row["absentee"] else "No")
    m2.metric("Out-of-State", "Yes" if row["out_of_state"] else "No")
    m3.metric("Ownership Tenure", f"{row['ownership_tenure_years']:.1f} yrs")
    equity = row["equity_proxy"]
    m4.metric("Equity Proxy", f"${equity:,.0f}" if pd.notna(equity) else "—")

    st.markdown(
        f"**Living area:** {row['living_area']:,.0f} sqft &nbsp;·&nbsp; "
        f"**Year built:** {int(row['year_built'])} &nbsp;·&nbsp; "
        f"**Owner:** {row['owner_name']} &nbsp;·&nbsp; "
        f"**Mailing:** {row['own_addr1']}, {row['own_city']}, {row['own_state']}"
    )

    current_note = st.session_state["notes"].get(selected, "")
    new_note = st.text_area(
        f"Notes for {row['situs_addr1']}", value=current_note, height=100,
        placeholder="Lead source, agent contact, drive-by condition, owner outreach status…",
        key=f"note_{selected}",
    )
    if new_note != current_note:
        st.session_state["notes"][selected] = new_note
        st.toast("Note saved.", icon="✅")
else:
    st.info("No parcels match the current filters.")

st.divider()

# ── MAO Calculator ───────────────────────────────────────────────────────────

st.subheader("🧮 MAO Calculator — 70% Rule")
st.markdown("**Max Allowable Offer = (ARV × Rule%) − Repair Costs**")

default_arv = int(row["arv_estimate"]) if row is not None else 350_000

inp1, inp2, inp3 = st.columns(3)
with inp1:
    arv_input = st.number_input("After Repair Value (ARV) $", min_value=0, value=default_arv, step=5_000, format="%d")
with inp2:
    repairs = st.number_input("Estimated Repair Cost $", min_value=0, value=35_000, step=1_000, format="%d")
with inp3:
    rule_pct = st.slider("Rule %", 55, 80, 70, step=1)

mao_result = mao(arv_input, repairs, rule_pct / 100)
res1, res2, res3 = st.columns(3)
res1.metric(f"MAO at {rule_pct}%", f"${mao_result:,.0f}")
res2.metric("MAO at 65% (conservative)", f"${mao(arv_input, repairs, 0.65):,.0f}")
res3.metric("MAO at 75% (aggressive)", f"${mao(arv_input, repairs, 0.75):,.0f}")

st.divider()
st.caption(
    "Miami-Dade single-family only. All inputs are Florida DOR public tax-roll records. "
    "Tax delinquency and pre-foreclosure/lis pendens are not included — see README 'Limitations.'"
)
