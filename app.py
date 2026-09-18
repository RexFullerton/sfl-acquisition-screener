"""
app.py — South Florida Wholesale Deal Finder
Streamlit UI: parcel-level leaderboard, parcel detail, MAO calculator.

Miami-Dade only for v1 (see data_pipeline.py). The ZIP-choropleth map from
the previous ZIP-grain version is removed, not adapted — the NAL file
carries no parcel coordinates, and faking a map without real geocoding
would misrepresent the data. See README for what it would take to add one.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

import arv as arv_mod
import scorer
from scorer import DEFAULT_WEIGHTS, SIGNAL_LABELS, GRADE_BG, GRADE_FG, mao

PARCELS_PATH = Path("data/parcels.csv")
DTYPES = {"parcel_id": str, "nbrhd_cd": str, "sale1_qual_cd": str, "sale2_qual_cd": str}

st.set_page_config(page_title="SF Wholesale Deal Finder", page_icon="🏡", layout="wide")


@st.cache_data(show_spinner="Loading parcels and computing ARV (comp-based, ~1 min on first run)…")
def load_scored_parcels() -> pd.DataFrame:
    parcels = pd.read_csv(PARCELS_PATH, dtype=DTYPES)
    arv_res = arv_mod.estimate_arv(parcels)
    return pd.concat([parcels, arv_res], axis=1)


if not PARCELS_PATH.exists():
    st.error("data/parcels.csv not found. Run `python3 data_pipeline.py` first.")
    st.stop()

base_df = load_scored_parcels()

if "notes" not in st.session_state:
    st.session_state["notes"] = {}

# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Controls")

    st.subheader("Signal Weights")
    st.caption(
        "Motivation score = weighted composite of these 3 signals. "
        "Weights are normalized — they don't need to sum to 100."
    )
    weights: dict[str, int] = {}
    for signal, label in SIGNAL_LABELS.items():
        weights[signal] = st.slider(label, 0, 100, DEFAULT_WEIGHTS[signal], step=5, key=f"weight_{signal}")
    if sum(weights.values()) == 0:
        st.error("At least one weight must be > 0.")
        st.stop()

    st.markdown("---")
    st.subheader("Filters")

    min_comp_count = st.slider(
        "Min ARV comp count", 0, 30, 5, step=1,
        help="Parcels with fewer comps than this show no ARV / spread (raise for more confidence, at the cost of coverage).",
    )
    max_dispersion = st.slider(
        "Max ARV dispersion (%)", 10, 300, 100, step=5,
        help="IQR of comp $/sqft, as a % of the median. Lower = tighter, more trustworthy comps.",
    )
    absentee_only = st.checkbox("Absentee owners only")
    max_just_value = st.slider("Max just (assessed) value ($k)", 100, 5000, 2000, step=50)

# ── Score ────────────────────────────────────────────────────────────────────

scored = scorer.score_parcels(base_df, weights)

mask = (scored["just_value"] <= max_just_value * 1000)
if absentee_only:
    mask &= scored["absentee"]
mask &= scored["arv_comp_count"].fillna(0) >= min_comp_count
mask &= scored["arv_dispersion_pct"].fillna(999) <= max_dispersion
filtered = scored[mask].copy()

# ── Header ───────────────────────────────────────────────────────────────────

st.title("South Florida Wholesale Deal Finder")
st.markdown(
    f"**{len(base_df):,}** single-family parcels ingested (Miami-Dade, DOR NAL 2025) · "
    f"**{filtered['arv_estimate'].notna().sum():,}** have a comp-based ARV meeting your filters.  \n"
    "Ranked by estimated spread (ARV × 70% − assessed value). See README for methodology and limitations."
)
st.divider()

grade_counts = filtered["grade"].value_counts().reindex(list("ABCDF") + ["N/A"], fill_value=0)
cols = st.columns(6)
for i, grade in enumerate(["A", "B", "C", "D", "F", "N/A"]):
    cols[i].metric(grade, int(grade_counts[grade]))
st.divider()

# ── Leaderboard ──────────────────────────────────────────────────────────────

st.subheader("📊 Leaderboard")

COL_MAP = {
    "parcel_id": "Parcel ID", "situs_addr1": "Address", "situs_city": "City",
    "grade": "Grade", "motivation_score": "Motiv. Score",
    "just_value": "Just Value", "arv_estimate": "ARV",
    "arv_comp_count": "Comps", "arv_dispersion_pct": "Disp %",
    "spread_estimate": "Spread (70% rule)",
    "absentee": "Absentee", "out_of_state": "Out-of-State",
    "ownership_tenure_years": "Tenure (yrs)",
}

ranked = filtered[filtered["spread_estimate"].notna()].sort_values("spread_estimate", ascending=False)
unranked = filtered[filtered["spread_estimate"].isna()]

full_board = pd.concat([ranked, unranked])[list(COL_MAP.keys())].rename(columns=COL_MAP).reset_index(drop=True)

csv_bytes = full_board.to_csv(index=False).encode("utf-8")
st.download_button(
    f"⬇️ Export full leaderboard as CSV ({len(full_board):,} rows)",
    csv_bytes, "sf_wholesale_parcels.csv", "text/csv",
)

DISPLAY_ROWS = 500
board = full_board.head(DISPLAY_ROWS)
if len(full_board) > DISPLAY_ROWS:
    st.caption(f"Showing the top {DISPLAY_ROWS:,} of {len(full_board):,} matching parcels on screen — export the CSV for the rest.")


def _row_style(row: pd.Series) -> list[str]:
    bg = GRADE_BG.get(row["Grade"], "#ffffff")
    fg = GRADE_FG.get(row["Grade"], "#000000")
    return [f"background-color: {bg}; color: {fg}"] * len(row)


styled = (
    board.style.apply(_row_style, axis=1).format({
        "Motiv. Score": "{:.0f}", "Just Value": "${:,.0f}", "ARV": "${:,.0f}",
        "Disp %": "{:.1f}%", "Spread (70% rule)": "${:,.0f}", "Tenure (yrs)": "{:.1f}",
    }, na_rep="—")
)
st.dataframe(styled, use_container_width=True, height=560)
st.caption(
    "Sorted by spread_estimate (parcels without a qualifying ARV are listed after, unranked). "
    "High spread + high dispersion/low comp count = treat with skepticism, not excitement — "
    "see README 'Known limitations.'"
)
st.divider()

# ── Parcel Detail ────────────────────────────────────────────────────────────

st.subheader("🔍 Parcel Detail & Notes")

options = board["Parcel ID"].tolist()
if options:
    selected = st.selectbox(
        "Select a parcel:", options=options,
        format_func=lambda pid: (
            f"{board.loc[board['Parcel ID']==pid,'Address'].iat[0]} — "
            f"{board.loc[board['Parcel ID']==pid,'City'].iat[0]} "
            f"({board.loc[board['Parcel ID']==pid,'Grade'].iat[0]})"
        ),
    )
    row = filtered[filtered["parcel_id"] == selected].iloc[0]
    grade = row["grade"]
    bg, fg = GRADE_BG.get(grade, "#fff"), GRADE_FG.get(grade, "#000")

    st.markdown(
        f"<span style='background:{bg};color:{fg};padding:4px 14px;border-radius:6px;"
        f"font-size:1.1rem;font-weight:700;'>Grade {grade}</span>&nbsp;&nbsp;"
        f"<span style='font-size:1.1rem;'>{row['situs_addr1']}, {row['situs_city']} — "
        f"Parcel {row['parcel_id']}</span>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Absentee Owner", "Yes" if row["absentee"] else "No")
    m2.metric("Out-of-State", "Yes" if row["out_of_state"] else "No")
    m3.metric("Ownership Tenure", f"{row['ownership_tenure_years']:.1f} yrs")
    m4.metric("Just Value", f"${row['just_value']:,.0f}")
    equity = row["equity_proxy"]
    m5.metric("Equity Proxy", f"${equity:,.0f}" if pd.notna(equity) else "—")

    a1, a2, a3 = st.columns(3)
    arv_val = row["arv_estimate"]
    a1.metric("ARV Estimate", f"${arv_val:,.0f}" if pd.notna(arv_val) else "No qualifying comps")
    a2.metric("Comp Count", f"{row['arv_comp_count']:.0f}")
    disp = row["arv_dispersion_pct"]
    a3.metric("Comp Dispersion", f"{disp:.1f}%" if pd.notna(disp) else "—")

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

default_arv = int(row["arv_estimate"]) if options and pd.notna(row["arv_estimate"]) else 350_000

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
    "Miami-Dade single-family only (v1 scope). Absentee ownership, ownership tenure, and "
    "equity proxy are real signals computed from Florida DOR NAL 2025 records. Tax delinquency "
    "and pre-foreclosure/lis pendens are NOT included — see README 'Known limitations' for why."
)
