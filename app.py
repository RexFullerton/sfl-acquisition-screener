"""
app.py — South Florida Wholesale Deal Finder
Streamlit UI: leaderboard, zip detail + notes, MAO calculator.
"""

import io
import json
from pathlib import Path

import folium
import streamlit as st
import pandas as pd
from streamlit_folium import st_folium
from scorer import (
    score_zip_codes,
    DEFAULT_WEIGHTS,
    SIGNAL_LABELS,
    GRADE_BG,
    GRADE_FG,
    mao,
)

# Folium fill colors — solid hex, matched to GRADE_BG palette
STRATEGY_PRESETS: dict[str, dict[str, int]] = {
    "Distressed owners": {
        "probate_activity_rate": 40,
        "tax_delinquency_rate":  30,
        "preforeclosure_rate":   20,
        "avg_years_ownership":   10,
        "absentee_owner_rate":    0,
    },
    "Investor / Absentee": {
        "absentee_owner_rate":   35,
        "tax_delinquency_rate":  25,
        "preforeclosure_rate":   20,
        "avg_years_ownership":   10,
        "probate_activity_rate": 10,
    },
}

GRADE_FILL: dict[str, str] = {
    "A": "#1a7a4a",  # dark green
    "B": "#0d7377",  # teal
    "C": "#d28c00",  # amber
    "D": "#c84614",  # orange-red
    "F": "#b71c1c",  # red
}
GEOJSON_PATH = Path("data/fl_zips.geojson")
GEOJSON_URL  = (
    "https://raw.githubusercontent.com/OpenDataDE/State-zip-code-GeoJSON"
    "/master/fl_florida_zip_codes_geo.min.json"
)

st.set_page_config(
    page_title="SF Wholesale Deal Finder",
    page_icon="🏡",
    layout="wide",
)

# ── Data ───────────────────────────────────────────────────────────────────────

REAL_DATA_PATH   = Path("data/real_data.csv")
SAMPLE_DATA_PATH = Path("data/sample_data.csv")

@st.cache_data
def load_data(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"zip_code": str})


@st.cache_data(show_spinner="Loading ZIP boundary GeoJSON…")
def load_geojson() -> dict:
    """Load FL ZIP boundary GeoJSON from disk cache or download once."""
    if GEOJSON_PATH.exists():
        return json.loads(GEOJSON_PATH.read_text())
    import subprocess
    GEOJSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["curl", "-fsSL", GEOJSON_URL, "-o", str(GEOJSON_PATH)],
        check=True,
    )
    return json.loads(GEOJSON_PATH.read_text())


raw_df: pd.DataFrame  # assigned inside sidebar block below

# ── Session state: notes per zip ───────────────────────────────────────────────

if "notes" not in st.session_state:
    st.session_state["notes"] = {}

# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Controls")

    # ── Strategy Mode ──────────────────────────────────────────────────────────
    st.subheader("Strategy Mode")
    strategy = st.radio(
        "Strategy Mode",
        options=list(STRATEGY_PRESETS.keys()),
        key="strategy_mode",
        label_visibility="collapsed",
    )
    # When the strategy changes, push preset weights into slider session state
    # keys before the sliders render so they pick up the new values.
    if st.session_state.get("_last_strategy") != strategy:
        for sig, val in STRATEGY_PRESETS[strategy].items():
            st.session_state[f"weight_{sig}"] = val
        st.session_state["_last_strategy"] = strategy

    st.markdown("---")
    # ── Data source toggle ─────────────────────────────────────────────────────
    st.subheader("Data Source")
    real_available = REAL_DATA_PATH.exists()
    data_options = ["Sample data"]
    if real_available:
        data_options.append("Real data (NAL 2025)")
    data_choice = st.radio(
        "Source",
        options=data_options,
        index=len(data_options) - 1 if real_available else 0,
        label_visibility="collapsed",
    )
    use_real = data_choice.startswith("Real")
    if not real_available:
        st.caption("Run `python3 data_pipeline.py` to generate real_data.csv.")

    raw_df = load_data(str(REAL_DATA_PATH if use_real else SAMPLE_DATA_PATH))

    st.markdown("---")
    # Weight sliders
    st.subheader("Signal Weights")
    st.caption(
        "Drag sliders to reflect your strategy. "
        "Weights are normalized — they don't need to sum to 100."
    )

    weights: dict[str, int] = {}
    for signal, label in SIGNAL_LABELS.items():
        weights[signal] = st.slider(
            label,
            min_value=0,
            max_value=100,
            value=DEFAULT_WEIGHTS[signal],
            step=5,
            key=f"weight_{signal}",
        )

    total_w = sum(weights.values())
    if total_w == 0:
        st.error("At least one weight must be > 0.")
        st.stop()

    # When real data is active, zero out signals not in NAL and redistribute
    # their points to absentee_owner_rate and avg_years_ownership.
    NAL_ZERO_SIGNALS = {"probate_activity_rate", "tax_delinquency_rate", "preforeclosure_rate"}
    NAL_KEEP_SIGNALS = {"absentee_owner_rate", "avg_years_ownership"}
    if use_real:
        zeroed_pts = sum(weights[s] for s in NAL_ZERO_SIGNALS)
        keep_base  = sum(weights[s] for s in NAL_KEEP_SIGNALS) or 1
        scoring_weights = dict(weights)
        for s in NAL_ZERO_SIGNALS:
            scoring_weights[s] = 0
        if zeroed_pts > 0:
            for s in NAL_KEEP_SIGNALS:
                scoring_weights[s] = weights[s] + round(zeroed_pts * weights[s] / keep_base)
    else:
        scoring_weights = weights

    st.markdown("---")
    st.caption("**Effective weights after normalization:**")
    display_weights = scoring_weights if use_real else weights
    display_total   = sum(display_weights.values()) or 1
    for sig, w in display_weights.items():
        if w == 0:
            continue
        pct = w / display_total * 100
        st.caption(f"• {SIGNAL_LABELS[sig]}: **{pct:.1f}%**")
    if use_real:
        st.caption("_Probate, tax delinquency & pre-FC zeroed — not in NAL._")

    # ── Filters ────────────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Filters")

    all_counties = sorted(raw_df["county"].unique())
    selected_counties = st.multiselect(
        "Counties",
        options=all_counties,
        default=all_counties,
    )

    has_prop_count = "property_count" in raw_df.columns

    if has_prop_count:
        min_prop_count = st.slider(
            "Min property count",
            min_value=50,
            max_value=5000,
            value=500,
            step=50,
            help="ZIP codes with fewer parcels are excluded (removes Keys/rural/conservation areas).",
        )
    else:
        min_prop_count = 0

    max_home_value = st.slider(
        "Max median home value ($k)",
        min_value=100,
        max_value=3000,
        value=1200,
        step=25,
        help="Exclude vacation/luxury ZIP codes above this median assessed value.",
    )

    if not selected_counties:
        st.warning("Select at least one county.")
        st.stop()

# ── Score & filter ─────────────────────────────────────────────────────────────

scored_df = score_zip_codes(raw_df, scoring_weights)

county_mask = scored_df["county"].isin(selected_counties)
prop_mask   = (scored_df["property_count"] >= min_prop_count) if has_prop_count else True
value_mask  = scored_df["median_home_value"] <= (max_home_value * 1000)

filtered_df = scored_df[county_mask & prop_mask & value_mask].copy()

# ── Page header ────────────────────────────────────────────────────────────────

st.title("South Florida Wholesale Deal Finder")
zip_count = len(raw_df)
data_label = "NAL 2025 county records" if use_real else "sample data"
st.markdown(
    f"Motivated-seller signal scores for **{zip_count} zip codes** across a ~2-hour radius "
    f"of Coral Gables — Miami-Dade, Broward, Palm Beach, Collier, Martin & Monroe counties.  \n"
    f"Tune signal weights in the sidebar. Currently using **{data_label}**."
)
st.divider()

# ── Grade summary bar ──────────────────────────────────────────────────────────

grade_counts = (
    filtered_df["grade"]
    .value_counts()
    .reindex(list("ABCDF"), fill_value=0)
)

grade_labels = {
    "A": "A — Hot",
    "B": "B — Strong",
    "C": "C — Moderate",
    "D": "D — Weak",
    "F": "F — Pass",
}

summary_cols = st.columns(5)
for i, grade in enumerate("ABCDF"):
    summary_cols[i].metric(grade_labels[grade], int(grade_counts[grade]))

st.divider()

# ── Leaderboard + Map tabs ─────────────────────────────────────────────────────

COL_MAP = {
    "zip_code":              "ZIP",
    "city":                  "City",
    "county":                "County",
    "grade":                 "Grade",
    "total_score":           "Score",
    "probate_activity_rate": "Probate %",
    "absentee_owner_rate":   "Absentee %",
    "tax_delinquency_rate":  "Tax Delinq %",
    "preforeclosure_rate":   "Pre-FC %",
    "avg_years_ownership":   "Avg Yrs Own",
    "median_home_value":     "Median Value",
}

SORT_OPTIONS = {
    "Score (default)":               ("total_score",           False),
    "Absentee Rate (high to low)":   ("absentee_owner_rate",   False),
    "Median Value (low to high)":    ("median_home_value",      True),
    "Avg Years Ownership (high to low)": ("avg_years_ownership", False),
}

tab_table, tab_map = st.tabs(["📊 Leaderboard", "🗺️ Map View"])

# ── Tab 1: Leaderboard ─────────────────────────────────────────────────────────

with tab_table:
    sort_choice = st.selectbox(
        "Sort by",
        options=list(SORT_OPTIONS.keys()),
        index=0,
    )
    sort_col, sort_asc = SORT_OPTIONS[sort_choice]

    board = (
        filtered_df[list(COL_MAP.keys())]
        .sort_values(sort_col, ascending=sort_asc)
        .rename(columns=COL_MAP)
        .reset_index(drop=True)
    )

    # CSV export — built before the table so the button sits above it
    csv_bytes = board.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇️ Export leaderboard as CSV",
        data=csv_bytes,
        file_name="sf_wholesale_scores.csv",
        mime="text/csv",
        help="Downloads the current leaderboard (filtered + scored) as a CSV file.",
    )

    def _row_style(row: pd.Series) -> list[str]:
        bg = GRADE_BG.get(row["Grade"], "#ffffff")
        fg = GRADE_FG.get(row["Grade"], "#000000")
        return [f"background-color: {bg}; color: {fg}"] * len(row)

    styled_board = (
        board.style
        .apply(_row_style, axis=1)
        .format({
            "Score":        "{:.1f}",
            "Median Value": "${:,.0f}",
            "Probate %":    "{:.1f}%",
            "Absentee %":   "{:.1f}%",
            "Tax Delinq %": "{:.1f}%",
            "Pre-FC %":     "{:.2f}%",
            "Avg Yrs Own":  "{:.1f} yrs",
        })
    )

    st.dataframe(styled_board, use_container_width=True, height=560)
    st.caption(
        "Scores are **min-max normalized** across displayed zip codes — "
        "they reflect relative rank within your filtered market.  \n"
        "Grade thresholds: A ≥ 80 · B 65–79 · C 50–64 · D 35–49 · F < 35"
    )

# ── Tab 2: Map View ────────────────────────────────────────────────────────────

with tab_map:
    geojson = load_geojson()

    # Build a lookup: zip_code → row dict for scored ZIPs only
    zip_lookup: dict[str, dict] = {
        row["zip_code"]: row
        for row in filtered_df[
            ["zip_code", "city", "county", "grade", "total_score", "absentee_owner_rate"]
        ].to_dict("records")
    }

    m = folium.Map(
        location=[25.85, -80.35],
        zoom_start=10,
        tiles="CartoDB positron",
        prefer_canvas=True,
    )

    def _style(feature: dict) -> dict:
        z = str(feature["properties"].get("ZCTA5CE10", "")).zfill(5)
        if z in zip_lookup:
            grade = zip_lookup[z]["grade"]
            return {
                "fillColor":   GRADE_FILL.get(grade, "#aaaaaa"),
                "color":       "#ffffff",
                "weight":      1,
                "fillOpacity": 0.72,
            }
        return {
            "fillColor":   "#cccccc",
            "color":       "#bbbbbb",
            "weight":      0.3,
            "fillOpacity": 0.15,
        }

    def _tooltip_html(feature: dict) -> str:
        z = str(feature["properties"].get("ZCTA5CE10", "")).zfill(5)
        if z not in zip_lookup:
            return f"<b>{z}</b><br/><span style='color:#888'>Not scored</span>"
        r = zip_lookup[z]
        grade = r["grade"]
        bg  = GRADE_FILL.get(grade, "#888")
        return (
            f"<div style='font-family:Arial,sans-serif;font-size:13px;line-height:1.7'>"
            f"<b>{z} — {r['city']}</b><br/>"
            f"<span style='background:{bg};color:#fff;padding:1px 7px;"
            f"border-radius:4px;font-weight:700'>Grade {grade}</span>"
            f"&nbsp;&nbsp;Score <b>{r['total_score']:.1f}</b><br/>"
            f"<span style='color:#555'>{r['county']} County</span><br/>"
            f"Absentee <b>{r['absentee_owner_rate']:.1f}%</b>"
            f"</div>"
        )

    folium.GeoJson(
        geojson,
        style_function=_style,
        tooltip=folium.GeoJsonTooltip(
            fields=[],          # we override via popup-style below
            labels=False,
        ),
    ).add_to(m)

    # Re-add with proper per-feature HTML tooltip (GeoJsonTooltip can't do dynamic HTML,
    # so we use a custom GeoJson with popup-based tooltip via the style callback pattern)
    # Simpler: iterate scored ZIPs and add CircleMarker tooltips as invisible points
    for z, r in zip_lookup.items():
        # Find centroid from filtered_df (lat/lon are in the data)
        rows = filtered_df[filtered_df["zip_code"] == z]
        if rows.empty:
            continue
        lat = rows.iloc[0].get("lat", None)
        lon = rows.iloc[0].get("lon", None)
        if lat is None or lon is None:
            continue
        folium.CircleMarker(
            location=[lat, lon],
            radius=1,
            color="none",
            fill=False,
            tooltip=folium.Tooltip(
                f"<div style='font-family:Arial,sans-serif;font-size:13px;line-height:1.7'>"
                f"<b>{z} — {r['city']}</b><br/>"
                f"<span style='background:{GRADE_FILL.get(r['grade'], '#888')};"
                f"color:#fff;padding:1px 7px;border-radius:4px;font-weight:700'>"
                f"Grade {r['grade']}</span>"
                f"&nbsp;&nbsp;Score <b>{r['total_score']:.1f}</b><br/>"
                f"<span style='color:#555'>{r['county']} County</span><br/>"
                f"Absentee <b>{r['absentee_owner_rate']:.1f}%</b>"
                f"</div>",
                sticky=True,
            ),
        ).add_to(m)

    st_folium(m, use_container_width=True, height=640, returned_objects=[])

    # Legend
    legend_cols = st.columns(5)
    for grade, label in [("A","Hot"),("B","Strong"),("C","Moderate"),("D","Weak"),("F","Pass")]:
        bg  = GRADE_BG[grade]
        fg  = GRADE_FG[grade]
        legend_cols[list("ABCDF").index(grade)].markdown(
            f"<div style='background:{bg};color:{fg};text-align:center;"
            f"padding:4px 0;border-radius:5px;font-weight:700;font-size:0.85rem'>"
            f"{grade} — {label}</div>",
            unsafe_allow_html=True,
        )
    st.caption("ZIP boundaries colored by grade. Hover a ZIP for details. Unscored ZIPs are gray.")

st.divider()

# ── Zip Code Detail & Notes ────────────────────────────────────────────────────

st.subheader("🔍 Zip Code Detail & Notes")

zip_options = board["ZIP"].tolist()  # already sorted by score desc
selected_zip = st.selectbox(
    "Select a zip code to inspect:",
    options=zip_options,
    format_func=lambda z: (
        f"{z} — {board.loc[board['ZIP'] == z, 'City'].iat[0]}  "
        f"({board.loc[board['ZIP'] == z, 'Grade'].iat[0]}  "
        f"{board.loc[board['ZIP'] == z, 'Score'].iat[0]:.1f})"
    ),
)

if selected_zip:
    row = filtered_df[filtered_df["zip_code"] == selected_zip].iloc[0]
    grade = row["grade"]
    bg = GRADE_BG[grade]
    fg = GRADE_FG[grade]

    # Grade badge via HTML
    st.markdown(
        f"<span style='background:{bg}; color:{fg}; padding:4px 14px; "
        f"border-radius:6px; font-size:1.1rem; font-weight:700;'>"
        f"Grade {grade}</span>&nbsp;&nbsp;"
        f"<span style='font-size:1.1rem;'>Score: <b>{row['total_score']:.1f}</b> / 100 &nbsp;·&nbsp; "
        f"{row['city']}, {row['county']} County</span>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Probate Activity",  f"{row['probate_activity_rate']:.1f}%")
    m2.metric("Absentee Owners",   f"{row['absentee_owner_rate']:.1f}%")
    m3.metric("Tax Delinquency",   f"{row['tax_delinquency_rate']:.1f}%")
    m4.metric("Pre-Foreclosure",   f"{row['preforeclosure_rate']:.2f}%")
    m5.metric("Avg Yrs Ownership", f"{row['avg_years_ownership']:.1f} yrs")

    st.markdown(f"**Median Home Value:** ${row['median_home_value']:,.0f}")

    # Notes
    current_note = st.session_state["notes"].get(selected_zip, "")
    new_note = st.text_area(
        f"Notes for {selected_zip}",
        value=current_note,
        height=110,
        placeholder=(
            "Add acquisition notes here — lead sources, agent contacts, "
            "recent comps, drive-by observations, owner outreach status…"
        ),
        key=f"note_input_{selected_zip}",
    )
    if new_note != current_note:
        st.session_state["notes"][selected_zip] = new_note
        st.toast(f"Note saved for {selected_zip}.", icon="✅")

    # Show all saved notes in an expander
    saved = {z: n for z, n in st.session_state["notes"].items() if n.strip()}
    if saved:
        with st.expander(f"All saved notes ({len(saved)} zip codes)"):
            for z, n in saved.items():
                st.markdown(f"**{z}** — {n}")

st.divider()

# ── MAO Calculator ─────────────────────────────────────────────────────────────

st.subheader("🧮 MAO Calculator — 70% Rule")
st.markdown(
    "**Max Allowable Offer = (ARV × Rule%) − Repair Costs**  \n"
    "The 70% rule leaves room for holding costs, closing costs, and wholesale "
    "fee/assignment spread. Tighten to 65% for slower markets; loosen to 75% "
    "for hot retail flips."
)

inp1, inp2, inp3 = st.columns(3)
with inp1:
    arv = st.number_input(
        "After Repair Value (ARV) $", min_value=0, value=350_000,
        step=5_000, format="%d",
    )
with inp2:
    repairs = st.number_input(
        "Estimated Repair Cost $", min_value=0, value=35_000,
        step=1_000, format="%d",
    )
with inp3:
    rule_pct = st.slider("Rule %", min_value=55, max_value=80, value=70, step=1)

mao_result  = mao(arv, repairs, rule_pct / 100)
mao_65      = mao(arv, repairs, 0.65)
mao_75      = mao(arv, repairs, 0.75)
equity_est  = arv - repairs - mao_result

res1, res2, res3, res4 = st.columns(4)
res1.metric(f"MAO at {rule_pct}% (your rule)", f"${mao_result:,.0f}")
res2.metric("MAO at 65% (conservative)", f"${mao_65:,.0f}")
res3.metric("MAO at 75% (aggressive)",   f"${mao_75:,.0f}")
res4.metric("Est. Equity Spread",         f"${equity_est:,.0f}")

st.markdown(
    f"<div style='background:#e8f4fd;border-left:4px solid #1a7fbf;"
    f"padding:12px 16px;border-radius:4px;font-size:0.95rem;line-height:1.6'>"
    f"💡 At <b>{rule_pct}%</b> rule: offer no more than "
    f"<b>${mao_result:,.0f}</b> on a "
    f"${arv:,.0f} ARV / ${repairs:,.0f} repair property. "
    f"Estimated spread available: <b>${equity_est:,.0f}</b> "
    f"(covers your wholesale fee + end-buyer's profit)."
    f"</div>",
    unsafe_allow_html=True,
)

# ── Footer ─────────────────────────────────────────────────────────────────────

st.divider()
if use_real:
    st.caption(
        "Showing **real NAL 2025 data** — absentee and out-of-state rates are "
        "derived from Florida DOR county files. Probate, tax-delinquency, and "
        "pre-foreclosure rates are not yet in the NAL and remain at 0."
    )
else:
    st.caption(
        "Showing **sample data** — probate, absentee, tax-delinquency, and "
        "pre-foreclosure rates are illustrative. Run `python3 data_pipeline.py` "
        "then switch to Real data in the sidebar."
    )
