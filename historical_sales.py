"""
historical_sales.py — Multi-vintage NAL/SDF sale-history stack. UNVALIDATED.

Status: written against the known 2025 Miami-Dade NAL schema and the
Florida DOR 2025 User's Guide's documented SDF layout. No historical NAL
or SDF file has been available to test this against — a public-records
request for Miami-Dade NAL + SDF, 2016-2025, has been submitted to DOR
(PTOTechnology@floridarevenue.com) but not yet fulfilled. Every assumption
below is provisional until run against real files. In particular:
  - We have not verified DOR_UC, field names, or field count for any year
    other than 2025 against that year's own User's Guide. Schema drift is
    only caught empirically (see check_schema_drift), not pre-verified.
  - The SDF field layout below is transcribed from the 2025 User's Guide
    and has never been run against an actual SDF file.
Do not treat this module's output as ARV/back-test input until it has
been exercised against real multi-year data and the per-year sale-date
distributions have been checked (see validate_year_window).

Expected input layout once files arrive:
    data/raw/historical/NAL/<year>/Dade 23 <Final|Preliminary> NAL <year>.zip
    data/raw/historical/SDF/<year>/Dade 23 <Final|Preliminary> SDF <year>.zip
(Naming is a guess pending real filenames from the DOR request — adjust
COUNTY_FILE_PATTERNS below once real files are in hand.)

Usage (once files exist):
    python3 historical_sales.py
"""

import re
import zipfile
from pathlib import Path

import pandas as pd

HISTORICAL_DIR = Path("data/raw/historical")
OUTPUT_CSV = Path("data/historical_sales.csv")

# ── 2025 NAL sale fields (verified against DOR's 2025 User's Guide) ────────────
NAL_SALE_COLS = [
    "PARCEL_ID", "DOR_UC", "NBRHD_CD",
    "QUAL_CD1", "SALE_PRC1", "SALE_YR1", "SALE_MO1",
    "QUAL_CD2", "SALE_PRC2", "SALE_YR2", "SALE_MO2",
    "TOT_LVG_AREA", "ACT_YR_BLT", "LND_VAL", "JV",
]

# ── 2025 SDF layout (Section 2 of the User's Guide) — one row per sale, not
# two slots per parcel. UNTESTED against a real SDF file.
SDF_COLS = [
    "PARCEL_ID",       # field 2
    "ASMNT_YR",        # field 3
    "DOR_UC",          # field 6
    "NBRHD_CD",        # field 7
    "SALE_ID_CD",      # field 10 — unique per transaction, not a slot number
    "VI_CD",           # field 12
    "QUAL_CD",         # field 16
    "SALE_YR",         # field 17
    "SALE_MO",         # field 18
    "SALE_PRC",        # field 19
    "MULTI_PAR_SAL",   # field 20
]

ARMS_LENGTH_QUAL_CODES = {"01", "02"}

DEDUP_KEY = ["parcel_id", "sale_year", "sale_month", "sale_price", "qual_code"]


# ── Schema drift detection ──────────────────────────────────────────────────

def check_schema_drift(year: int, header: list[str], expected_cols: list[str]) -> list[str]:
    """
    Compare a year's actual CSV header against the columns this module needs.
    Returns a list of human-readable problems (empty = no drift detected).
    Does NOT compare against that year's own official layout doc — we only
    have 2025's. A clean result here means "usable," not "matches DOR's
    2018/2020/etc. spec exactly" — that would need each year's own guide.
    """
    problems = []
    missing = [c for c in expected_cols if c not in header]
    if missing:
        problems.append(f"{year}: missing expected columns {missing} — DO NOT silently proceed; "
                         f"field names or positions may have changed for this vintage.")
    return problems


# ── Sale-date window sanity check ───────────────────────────────────────────

def validate_year_window(year: int, sale_dates: pd.Series) -> list[str]:
    """
    Per the DOR spec, a <year> NAL/SDF submission should only contain sales
    from roughly [Jan <year-1>, current submission date]. Flag anything
    that falls well outside that band — that's a "stop and tell me" case,
    not something to coerce quietly.
    """
    problems = []
    if sale_dates.empty:
        return problems
    lo_expected = pd.Timestamp(year=year - 1, month=1, day=1)
    hi_expected = pd.Timestamp(year=year + 1, month=1, day=1)
    out_of_band = sale_dates[(sale_dates < lo_expected) | (sale_dates >= hi_expected)]
    if len(out_of_band) > 0:
        problems.append(
            f"{year}: {len(out_of_band)} sale date(s) fall outside the expected "
            f"[{lo_expected.date()}, {hi_expected.date()}) window for this vintage — "
            f"stop and inspect before trusting this year's file."
        )
    return problems


# ── NAL -> long sale-event format (mirrors arv.py's _build_sale_events) ────

def _month_start(year: pd.Series, month: pd.Series) -> pd.Series:
    month_clamped = month.where(month.between(1, 12))
    return pd.to_datetime({"year": year, "month": month_clamped, "day": 1}, errors="coerce")


def nal_to_sale_events(nal_df: pd.DataFrame, vintage_year: int) -> pd.DataFrame:
    """
    One row per (parcel, slot) sale event, carrying that VINTAGE's own parcel
    characteristics (not current-roll characteristics) — see README
    "Historical data" for why period-appropriate characteristics matter.

    Never assumes SALE_1 is chronologically later than SALE_2 — the DOR
    2025 User's Guide states slot selection "is not necessarily based on
    chronological occurrence." Ordering, wherever it matters downstream,
    must be derived from the actual date values, exactly as backtest.py
    already does for the single-year case.
    """
    events = []
    for suffix in ("1", "2"):
        e = pd.DataFrame({
            "parcel_id": nal_df["PARCEL_ID"],
            "vintage_year": vintage_year,
            "dor_use_code": nal_df["DOR_UC"],
            "nbrhd_cd": nal_df["NBRHD_CD"],
            "living_area": pd.to_numeric(nal_df["TOT_LVG_AREA"], errors="coerce"),
            "year_built": pd.to_numeric(nal_df["ACT_YR_BLT"], errors="coerce"),
            "just_value": pd.to_numeric(nal_df["JV"], errors="coerce"),
            "sale_price": pd.to_numeric(nal_df[f"SALE_PRC{suffix}"], errors="coerce"),
            "sale_year": pd.to_numeric(nal_df[f"SALE_YR{suffix}"], errors="coerce"),
            "sale_month": pd.to_numeric(nal_df[f"SALE_MO{suffix}"], errors="coerce"),
            "qual_code": nal_df[f"QUAL_CD{suffix}"],
            "source": f"NAL_{vintage_year}_slot{suffix}",
        })
        events.append(e)
    out = pd.concat(events, ignore_index=True)
    out["sale_date"] = _month_start(out["sale_year"], out["sale_month"])
    return out


def sdf_to_sale_events(sdf_df: pd.DataFrame, vintage_year: int) -> pd.DataFrame:
    """UNTESTED — no real SDF file has been available to run this against."""
    out = pd.DataFrame({
        "parcel_id": sdf_df["PARCEL_ID"],
        "vintage_year": vintage_year,
        "dor_use_code": sdf_df["DOR_UC"],
        "nbrhd_cd": sdf_df["NBRHD_CD"],
        "living_area": pd.NA,   # SDF carries no living-area field — must be joined from that vintage's NAL
        "year_built": pd.NA,
        "just_value": pd.NA,
        "sale_price": pd.to_numeric(sdf_df["SALE_PRC"], errors="coerce"),
        "sale_year": pd.to_numeric(sdf_df["SALE_YR"], errors="coerce"),
        "sale_month": pd.to_numeric(sdf_df["SALE_MO"], errors="coerce"),
        "qual_code": sdf_df["QUAL_CD"],
        "source": f"SDF_{vintage_year}_{sdf_df.get('SALE_ID_CD', '')}",
    })
    out["sale_date"] = _month_start(out["sale_year"], out["sale_month"])
    return out


# ── Dedup across vintages ───────────────────────────────────────────────────

def dedup_sale_events(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    One row per distinct sale, keyed on DEDUP_KEY. Returns (deduped, conflicts).

    `conflicts`: groups sharing (parcel_id, sale_year, sale_month) but with
    MORE THAN ONE distinct sale_price — these are not resolved automatically.
    Report them; a human (or a follow-up rule, decided explicitly) picks.
    """
    events = events.dropna(subset=["parcel_id", "sale_year", "sale_month", "sale_price", "qual_code"])
    deduped = events.drop_duplicates(subset=DEDUP_KEY, keep="first").copy()

    price_variety = (
        deduped.groupby(["parcel_id", "sale_year", "sale_month"])["sale_price"]
        .nunique()
    )
    conflicting_keys = price_variety[price_variety > 1].index
    conflicts = deduped[
        deduped.set_index(["parcel_id", "sale_year", "sale_month"]).index.isin(conflicting_keys)
    ].sort_values(["parcel_id", "sale_year", "sale_month"])

    return deduped, conflicts


# ── Entry point ──────────────────────────────────────────────────────────────

def run():
    if not HISTORICAL_DIR.exists() or not any(HISTORICAL_DIR.rglob("*.zip")):
        print(f"No historical NAL/SDF files found under {HISTORICAL_DIR}/.")
        print("This module is unvalidated and has nothing to ingest yet — "
              "waiting on the Florida DOR public-records request "
              "(NAL + SDF, Miami-Dade, 2016-2025) to be fulfilled.")
        return

    # Real ingestion loop intentionally not fleshed out further: without a
    # real file in hand, hardcoding a zip-member-name / column-order
    # assumption beyond what's already verified for 2025 would be guessing,
    # not engineering. Wire this up against the first real file that lands,
    # then run check_schema_drift / validate_year_window before trusting it.
    raise NotImplementedError(
        "Historical files detected but the ingestion loop is not wired up yet — "
        "this needs to be written against the real file names/layout DOR sends back, "
        "not guessed in advance."
    )


if __name__ == "__main__":
    run()
