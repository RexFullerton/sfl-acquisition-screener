"""
data_pipeline.py — Florida DOR NAL 2025 Data Pipeline (parcel grain)

Parses county NAL zip files and produces a parcel-level dataset — one row
per property, not one row per ZIP code. ZIP-level aggregation is available
as an optional summary view (aggregate_to_zip) but is not the primary output.

Source: Florida Dept. of Revenue, county Property Appraiser NAL bulk files.
Sale qualification codes per:
https://floridarevenue.com/property/Documents/salequalcodes_bef01012025.pdf

Usage:
    python3 data_pipeline.py                       # Miami-Dade only (v1 default)
    python3 data_pipeline.py --counties "Miami-Dade,Broward,Palm Beach"
    python3 data_pipeline.py --include-extra-residential   # add condo/co-op/duplex

Output:
    data/parcels.csv — one row per parcel
"""

import argparse
import zipfile
import time
from pathlib import Path

import pandas as pd

# ── Configuration ──────────────────────────────────────────────────────────────

RAW_DIR = Path("data/raw")
OUTPUT_CSV = Path("data/parcels.csv")

ASSESSMENT_YEAR = 2025

# Florida DOR county number -> (zip-file-glob, NAL-csv-name, county-label)
# Ingestion is parameterized across all three target counties. v1 validates
# and ships on Miami-Dade only (see DEFAULT_COUNTIES) — Broward/Palm Beach
# ingestion runs the same code path but hasn't been validated end-to-end.
COUNTIES = {
    "Miami-Dade":  ("Dade 23 Final NAL 2025.zip",       "NAL23F202501.csv"),
    "Broward":     ("Broward 16 Final NAL 2025.zip",     "NAL16F202501.csv"),
    "Palm Beach":  ("Palm Beach 60 Final NAL 2025.zip",  "NAL60F202501.csv"),
}

DEFAULT_COUNTIES = ["Miami-Dade"]

# ── DOR land use codes (DOR_UC) ─────────────────────────────────────────────────
# Verified against the actual Miami-Dade NAL file, not assumed from memory.
# Core = single-family only. Extra-residential is configurable and OFF by
# default — condos/co-ops/duplexes are a different acquisition profile
# (HOA approval, rental restrictions, shared structure) and are kept out of
# the default run rather than silently blended into "residential."
CORE_USE_CODES = {"001"}                        # Single Family
EXTRA_RESIDENTIAL_USE_CODES = {
    "004",  # Condominiums
    "005",  # Cooperatives
    "008",  # Multi-family, < 10 units (duplex/triplex/quadplex)
}
# Explicitly excluded and why:
#   000 Vacant residential   — no structure; can't run sqft-based comps
#   002 Mobile homes         — different asset class / financing profile
#   003 Multi-family 10+     — not a single-property wholesale target
#   006 Retirement homes, 007 Misc residential, 009 Common elements — not
#       independently transactable single properties

# Sale qualification codes considered arm's-length (Florida DOR standard,
# "Qualified Arm's Length Real Property Transfers, included in sales ratio
# analysis"). Codes 03-06 are arm's-length at time of transfer but excluded
# from ratio analysis by DOR itself (property/legal characteristics changed,
# multi-parcel, cross-county); we exclude them from comps for the same reason
# DOR does. Codes 11+ are disqualified transfers (corrective/quitclaim/tax
# deeds, foreclosure-related, government, related-party, forced sales, etc.).
ARMS_LENGTH_QUAL_CODES = {"01", "02"}

NAL_COLS = [
    "PARCEL_ID",
    "DOR_UC",
    "NBRHD_CD",       # property appraiser's own valuation neighborhood — used as the comp geography unit (see arv.py)
    "JV",             # just/assessed value
    "LND_VAL",        # land value
    "TOT_LVG_AREA",   # total living area (sqft)
    "ACT_YR_BLT",     # actual year built
    "QUAL_CD1", "SALE_PRC1", "SALE_YR1", "SALE_MO1",   # most recent sale
    "QUAL_CD2", "SALE_PRC2", "SALE_YR2", "SALE_MO2",   # second most recent sale
    "OWN_NAME",
    "OWN_ADDR1", "OWN_ADDR2", "OWN_CITY", "OWN_STATE", "OWN_ZIPCD",
    "PHY_ADDR1", "PHY_ADDR2", "PHY_CITY", "PHY_ZIPCD",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _zip5(raw: pd.Series) -> pd.Series:
    """Normalize a ZIP-code series to exactly 5 digits."""
    return raw.fillna("").str.strip().str[:5].str.zfill(5)


def _normalize_addr(raw: pd.Series) -> pd.Series:
    """Uppercase, strip punctuation, collapse whitespace for address comparison."""
    s = raw.fillna("").str.upper().str.strip()
    s = s.str.replace(r"[^\w\s]", "", regex=True)
    s = s.str.replace(r"\s+", " ", regex=True)
    return s


def _read_nal(zip_path: Path, csv_name: str, county_label: str) -> pd.DataFrame:
    """Open the NAL zip, stream the CSV, return a trimmed DataFrame."""
    print(f"  Reading {county_label} …", end=" ", flush=True)
    t0 = time.time()

    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(csv_name) as raw_fh:
            df = pd.read_csv(
                raw_fh,
                usecols=NAL_COLS,
                dtype=str,
                low_memory=False,
                na_values=["", " "],
                keep_default_na=False,
            )

    for col in df.columns:
        df[col] = df[col].str.strip()

    df["county"] = county_label
    elapsed = time.time() - t0
    print(f"{len(df):,} rows in {elapsed:.1f}s")
    return df


# ── Core pipeline ────────────────────────────────────────────────────────────────

def build_parcels(counties: list[str], include_extra_residential: bool = False) -> pd.DataFrame:
    """Ingest, filter, and derive signals. Returns one row per parcel."""
    frames = []
    for county_label in counties:
        zip_name, csv_name = COUNTIES[county_label]
        zip_path = RAW_DIR / zip_name
        if not zip_path.exists():
            print(f"  WARNING: {zip_path} not found — skipping {county_label}")
            continue
        frames.append(_read_nal(zip_path, csv_name, county_label))

    if not frames:
        raise RuntimeError("No county files found in data/raw/")

    df = pd.concat(frames, ignore_index=True)
    print(f"Total parcels ingested: {len(df):,}")

    # ── Residential filter ──────────────────────────────────────────────────
    use_codes = set(CORE_USE_CODES)
    if include_extra_residential:
        use_codes |= EXTRA_RESIDENTIAL_USE_CODES
    pre = len(df)
    df = df[df["DOR_UC"].isin(use_codes)].copy()
    print(f"Kept {len(df):,} of {pre:,} parcels matching use codes {sorted(use_codes)}")

    # ── Numeric fields ──────────────────────────────────────────────────────
    for col in ["JV", "LND_VAL", "TOT_LVG_AREA", "ACT_YR_BLT",
                "SALE_PRC1", "SALE_YR1", "SALE_PRC2", "SALE_YR2"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop parcels with no living area or year built — can't run comps or
    # display a usable record without them (affects a negligible fraction;
    # see data_pipeline output for the exact count).
    pre = len(df)
    df = df[(df["TOT_LVG_AREA"] > 0) & (df["ACT_YR_BLT"] > 0)].copy()
    print(f"Dropped {pre - len(df):,} parcels with missing living area or year built")

    # ── ZIP normalization ───────────────────────────────────────────────────
    df["situs_zip"] = _zip5(df["PHY_ZIPCD"])
    df["mail_zip"] = _zip5(df["OWN_ZIPCD"])
    pre = len(df)
    df = df[(df["situs_zip"] != "00000")].copy()
    print(f"Dropped {pre - len(df):,} parcels with missing/invalid property ZIP")

    # ── Absentee ownership — full address comparison, not just ZIP ─────────
    norm_own_addr = _normalize_addr(df["OWN_ADDR1"])
    norm_phy_addr = _normalize_addr(df["PHY_ADDR1"])
    df["absentee"] = (
        (norm_own_addr != norm_phy_addr) | (df["mail_zip"] != df["situs_zip"])
    ) & (df["mail_zip"] != "00000") & (df["mail_zip"] != "")

    FL_VARIANTS = {"FL", "FLORIDA"}
    mail_state = df["OWN_STATE"].fillna("").str.strip().str.upper()
    df["out_of_state"] = (~mail_state.isin(FL_VARIANTS)) & (mail_state != "")

    # ── Ownership tenure — blank SALE_YR1 means no recorded sale, which we
    # treat as long-tenured (never sold since DOR records began) rather than
    # dropping it from the average. See README "Known limitations": this is
    # a judgment call, not a fact — a small share of blanks may be recent
    # unrecorded transfers rather than genuine multi-decade holds.
    LONG_TENURE_YEARS = 100  # matches the existing clip ceiling
    years_since_sale = ASSESSMENT_YEAR - df["SALE_YR1"]
    df["ownership_tenure_years"] = years_since_sale.fillna(LONG_TENURE_YEARS).clip(0, LONG_TENURE_YEARS)

    # ── Equity proxy — NOT real equity. See README for full caveats:
    # ignores any mortgage/lien balance, and JV can understate true market
    # value for long-held homesteaded parcels under Florida's Save Our Homes
    # assessment cap, which biases this proxy low for exactly the longest-
    # held owners. Only computable when there's a trustworthy sale price.
    has_trustworthy_sale = df["QUAL_CD1"].isin(ARMS_LENGTH_QUAL_CODES) & (df["SALE_PRC1"] > 0)
    df["equity_proxy"] = pd.NA
    df.loc[has_trustworthy_sale, "equity_proxy"] = (
        df.loc[has_trustworthy_sale, "JV"] - df.loc[has_trustworthy_sale, "SALE_PRC1"]
    )
    df["equity_proxy_annualized_pct"] = pd.NA
    valid_annualized = has_trustworthy_sale & (df["ownership_tenure_years"] > 0) & (df["SALE_PRC1"] > 0)
    df.loc[valid_annualized, "equity_proxy_annualized_pct"] = (
        ((df.loc[valid_annualized, "JV"] / df.loc[valid_annualized, "SALE_PRC1"]) - 1)
        / df.loc[valid_annualized, "ownership_tenure_years"] * 100
    )

    # ── Final schema ─────────────────────────────────────────────────────────
    out = df.rename(columns={
        "PARCEL_ID": "parcel_id",
        "DOR_UC": "dor_use_code",
        "NBRHD_CD": "nbrhd_cd",
        "JV": "just_value",
        "LND_VAL": "land_value",
        "TOT_LVG_AREA": "living_area",
        "ACT_YR_BLT": "year_built",
        "OWN_NAME": "owner_name",
        "OWN_ADDR1": "own_addr1", "OWN_CITY": "own_city", "OWN_STATE": "own_state",
        "PHY_ADDR1": "situs_addr1", "PHY_CITY": "situs_city",
        "QUAL_CD1": "sale1_qual_cd", "SALE_PRC1": "sale1_price",
        "SALE_YR1": "sale1_year", "SALE_MO1": "sale1_month",
        "QUAL_CD2": "sale2_qual_cd", "SALE_PRC2": "sale2_price",
        "SALE_YR2": "sale2_year", "SALE_MO2": "sale2_month",
    })[[
        "parcel_id", "county", "dor_use_code", "nbrhd_cd",
        "situs_addr1", "situs_city", "situs_zip",
        "owner_name", "own_addr1", "own_city", "own_state", "mail_zip",
        "living_area", "year_built", "land_value", "just_value",
        "sale1_price", "sale1_year", "sale1_month", "sale1_qual_cd",
        "sale2_price", "sale2_year", "sale2_month", "sale2_qual_cd",
        "absentee", "out_of_state", "ownership_tenure_years",
        "equity_proxy", "equity_proxy_annualized_pct",
    ]]

    print(f"\nFinal parcel count: {len(out):,}")
    print(f"Absentee owners:  {out['absentee'].mean()*100:.1f}%")
    print(f"Out-of-state:     {out['out_of_state'].mean()*100:.1f}%")
    print(f"Median tenure:    {out['ownership_tenure_years'].median():.1f} years")
    print(f"Parcels with a usable equity_proxy: {out['equity_proxy'].notna().sum():,} "
          f"({out['equity_proxy'].notna().mean()*100:.1f}%)")

    return out


def aggregate_to_zip(parcels: pd.DataFrame) -> pd.DataFrame:
    """Optional ZIP-level summary view — not the primary output."""
    agg = parcels.groupby("situs_zip").agg(
        property_count=("parcel_id", "count"),
        absentee_rate=("absentee", "mean"),
        out_of_state_rate=("out_of_state", "mean"),
        avg_tenure_years=("ownership_tenure_years", "mean"),
        median_just_value=("just_value", "median"),
    ).reset_index()
    for col in ["absentee_rate", "out_of_state_rate"]:
        agg[col] = (agg[col] * 100).round(2)
    agg["avg_tenure_years"] = agg["avg_tenure_years"].round(1)
    return agg


def run(counties: list[str], include_extra_residential: bool):
    print("=== NAL 2025 Parcel Pipeline ===\n")
    print(f"Counties: {', '.join(counties)}")
    print(f"Extra-residential (condo/co-op/duplex) included: {include_extra_residential}\n")

    parcels = build_parcels(counties, include_extra_residential)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    parcels.to_csv(OUTPUT_CSV, index=False)
    print(f"\nWrote {len(parcels):,} parcels → {OUTPUT_CSV}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--counties", default=",".join(DEFAULT_COUNTIES),
                         help="Comma-separated county names, e.g. 'Miami-Dade,Broward'")
    parser.add_argument("--include-extra-residential", action="store_true",
                         help="Include condo/co-op/duplex (DOR_UC 004/005/008) alongside single-family")
    args = parser.parse_args()
    run([c.strip() for c in args.counties.split(",")], args.include_extra_residential)
