"""
data_pipeline.py — Florida DOR NAL 2025 Data Pipeline
Parses six county NAL zip files, flags absentee/OOS owners, aggregates to ZIP.

Usage:
    python3 data_pipeline.py

Output:
    data/real_data.csv   — same schema as data/sample_data.csv
"""

import zipfile
import io
import time
from pathlib import Path
from datetime import date

import pandas as pd

# ── Configuration ──────────────────────────────────────────────────────────────

RAW_DIR = Path("data/raw")
OUTPUT_CSV = Path("data/real_data.csv")

ASSESSMENT_YEAR = 2025

# Florida DOR county number → (zip-file-glob, NAL-csv-name, county-label)
COUNTIES = {
    "Miami-Dade":  ("Dade 23 Final NAL 2025.zip",       "NAL23F202501.csv"),
    "Broward":     ("Broward 16 Final NAL 2025.zip",     "NAL16F202501.csv"),
    "Palm Beach":  ("Palm Beach 60 Final NAL 2025.zip",  "NAL60F202501.csv"),
    "Collier":     ("Collier 21 Final NAL 2025.zip",     "NAL21F202501.csv"),
    "Martin":      ("Martin 53 Final NAL 2025.zip",      "NAL53F202501.csv"),
    "Monroe":      ("Monroe 54 Final NAL 2025.zip",      "NAL54F202501.csv"),
}

# Columns we actually need (by name — safe across layout variations)
NAL_COLS = [
    "PARCEL_ID",   # parcel identifier
    "DOR_UC",      # DOR land use code
    "JV",          # just/assessed value
    "SALE_YR1",    # year of most-recent sale
    "SALE_MO1",    # month of most-recent sale
    "OWN_NAME",    # owner name
    "OWN_ADDR1",   # mailing address line 1
    "OWN_ADDR2",   # mailing address line 2
    "OWN_CITY",    # mailing city
    "OWN_STATE",   # mailing state
    "OWN_ZIPCD",   # mailing zip
    "PHY_ADDR1",   # property address line 1
    "PHY_CITY",    # property city
    "PHY_ZIPCD",   # property zip
]

# ── Static ZIP-code centroid lookup (lat, lon) ─────────────────────────────────
# Covers ~95 %+ of parcels in the 6 target counties; county centroid used as fallback.

ZIP_CENTROIDS: dict[str, tuple[float, float]] = {
    # Miami-Dade
    "33010": (25.857, -80.278), "33012": (25.870, -80.307), "33013": (25.882, -80.292),
    "33014": (25.905, -80.302), "33015": (25.934, -80.302), "33016": (25.917, -80.314),
    "33018": (25.925, -80.337), "33030": (25.469, -80.478), "33031": (25.531, -80.452),
    "33032": (25.549, -80.432), "33033": (25.453, -80.427), "33034": (25.419, -80.543),
    "33035": (25.393, -80.527), "33039": (25.391, -80.497), "33054": (25.886, -80.255),
    "33055": (25.940, -80.278), "33056": (25.937, -80.254), "33101": (25.774, -80.194),
    "33109": (25.790, -80.160), "33122": (25.795, -80.291), "33125": (25.769, -80.227),
    "33126": (25.771, -80.265), "33127": (25.816, -80.206), "33128": (25.774, -80.208),
    "33129": (25.749, -80.202), "33130": (25.762, -80.200), "33131": (25.760, -80.188),
    "33132": (25.775, -80.184), "33133": (25.729, -80.244), "33134": (25.747, -80.263),
    "33135": (25.765, -80.235), "33136": (25.784, -80.201), "33137": (25.822, -80.194),
    "33138": (25.844, -80.194), "33139": (25.793, -80.135), "33140": (25.811, -80.125),
    "33141": (25.837, -80.122), "33142": (25.810, -80.230), "33143": (25.712, -80.274),
    "33144": (25.762, -80.264), "33145": (25.754, -80.229), "33146": (25.724, -80.258),
    "33147": (25.836, -80.233), "33149": (25.728, -80.158), "33150": (25.858, -80.207),
    "33154": (25.878, -80.128), "33155": (25.747, -80.283), "33156": (25.680, -80.285),
    "33157": (25.626, -80.335), "33158": (25.629, -80.303), "33160": (25.933, -80.142),
    "33161": (25.888, -80.172), "33162": (25.917, -80.177), "33165": (25.759, -80.326),
    "33166": (25.814, -80.296), "33167": (25.869, -80.232), "33168": (25.885, -80.213),
    "33169": (25.931, -80.215), "33170": (25.574, -80.435), "33172": (25.781, -80.345),
    "33173": (25.703, -80.354), "33174": (25.763, -80.358), "33175": (25.740, -80.356),
    "33176": (25.671, -80.334), "33177": (25.618, -80.380), "33178": (25.841, -80.347),
    "33179": (25.948, -80.178), "33180": (25.961, -80.157), "33181": (25.893, -80.148),
    "33183": (25.711, -80.377), "33184": (25.759, -80.379), "33185": (25.737, -80.399),
    "33186": (25.677, -80.376), "33187": (25.655, -80.415), "33189": (25.579, -80.392),
    "33190": (25.572, -80.347), "33193": (25.711, -80.416), "33194": (25.748, -80.409),
    "33196": (25.693, -80.425),
    # Broward
    "33004": (26.029, -80.148), "33009": (26.010, -80.148), "33019": (26.015, -80.124),
    "33020": (26.009, -80.149), "33021": (26.015, -80.168), "33023": (25.988, -80.210),
    "33024": (26.006, -80.234), "33025": (25.977, -80.234), "33026": (26.005, -80.263),
    "33027": (25.993, -80.291), "33028": (26.019, -80.295), "33029": (26.016, -80.318),
    "33060": (26.225, -80.132), "33062": (26.239, -80.099), "33063": (26.197, -80.249),
    "33064": (26.255, -80.119), "33065": (26.213, -80.249), "33066": (26.211, -80.221),
    "33067": (26.228, -80.209), "33068": (26.175, -80.219), "33069": (26.163, -80.156),
    "33071": (26.161, -80.259), "33073": (26.243, -80.209), "33076": (26.251, -80.249),
    "33309": (26.175, -80.168), "33311": (26.122, -80.178), "33312": (26.088, -80.165),
    "33313": (26.125, -80.212), "33314": (26.087, -80.206), "33315": (26.090, -80.148),
    "33316": (26.095, -80.115), "33317": (26.088, -80.230), "33319": (26.156, -80.198),
    "33321": (26.135, -80.241), "33322": (26.123, -80.243), "33323": (26.134, -80.266),
    "33324": (26.105, -80.248), "33325": (26.109, -80.277), "33326": (26.094, -80.272),
    "33327": (26.124, -80.323), "33328": (26.079, -80.260), "33330": (26.058, -80.276),
    "33331": (26.049, -80.306), "33332": (26.051, -80.341), "33334": (26.162, -80.121),
    "33388": (26.134, -80.269), "33441": (26.304, -80.097), "33442": (26.333, -80.105),
    # Palm Beach
    "33401": (26.711, -80.065), "33403": (26.778, -80.076), "33404": (26.797, -80.071),
    "33405": (26.666, -80.055), "33406": (26.654, -80.088), "33407": (26.750, -80.088),
    "33408": (26.819, -80.053), "33409": (26.679, -80.108), "33410": (26.828, -80.108),
    "33411": (26.688, -80.130), "33412": (26.765, -80.165), "33413": (26.661, -80.130),
    "33414": (26.645, -80.182), "33415": (26.641, -80.108), "33417": (26.695, -80.077),
    "33418": (26.868, -80.127), "33426": (26.530, -80.089), "33428": (26.484, -80.164),
    "33430": (26.672, -80.397), "33431": (26.386, -80.099), "33432": (26.362, -80.076),
    "33433": (26.381, -80.134), "33434": (26.405, -80.135), "33435": (26.531, -80.062),
    "33436": (26.527, -80.090), "33437": (26.545, -80.130), "33438": (26.935, -80.608),
    "33444": (26.361, -80.095), "33445": (26.422, -80.107), "33446": (26.443, -80.152),
    "33448": (26.461, -80.098), "33449": (26.563, -80.174), "33458": (26.935, -80.186),
    "33460": (26.612, -80.055), "33461": (26.618, -80.097), "33462": (26.592, -80.076),
    "33463": (26.608, -80.130), "33467": (26.611, -80.164), "33469": (26.975, -80.101),
    "33470": (26.766, -80.283), "33472": (26.580, -80.220), "33473": (26.539, -80.218),
    "33477": (26.952, -80.087), "33478": (26.967, -80.204), "33480": (26.702, -80.040),
    "33483": (26.362, -80.064), "33484": (26.430, -80.095), "33486": (26.365, -80.109),
    "33487": (26.396, -80.079), "33496": (26.409, -80.200), "33498": (26.428, -80.222),
    # Collier
    "34102": (26.139, -81.797), "34103": (26.155, -81.790), "34104": (26.145, -81.733),
    "34105": (26.170, -81.759), "34108": (26.232, -81.804), "34109": (26.237, -81.768),
    "34110": (26.290, -81.770), "34112": (26.109, -81.754), "34113": (26.052, -81.712),
    "34114": (26.040, -81.635), "34116": (26.177, -81.701), "34117": (26.132, -81.629),
    "34119": (26.238, -81.720), "34120": (26.255, -81.655), "34134": (26.332, -81.822),
    "34135": (26.342, -81.754), "34138": (25.903, -81.361), "34139": (25.947, -81.375),
    "34140": (25.992, -81.403), "34141": (25.866, -81.018), "34142": (26.337, -81.330),
    "34145": (25.943, -81.730),
    # Martin
    "34957": (27.190, -80.189), "34990": (27.102, -80.363), "34994": (27.078, -80.336),
    "34996": (27.183, -80.268), "34997": (27.046, -80.358), "34956": (27.097, -80.458),
    "34958": (27.170, -80.327),
    # Monroe
    "33001": (24.735, -81.383), "33036": (24.861, -80.799), "33037": (25.122, -80.424),
    "33040": (24.560, -81.784), "33041": (24.558, -81.783), "33042": (24.661, -81.521),
    "33043": (24.683, -81.366), "33050": (24.705, -81.070), "33051": (24.734, -80.980),
    "33070": (24.861, -80.795),
}

COUNTY_CENTROIDS: dict[str, tuple[float, float]] = {
    "Miami-Dade": (25.775, -80.208),
    "Broward":    (26.166, -80.333),
    "Palm Beach": (26.715, -80.053),
    "Collier":    (26.112, -81.400),
    "Martin":     (27.050, -80.400),
    "Monroe":     (24.660, -81.530),
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _zip5(raw: pd.Series) -> pd.Series:
    """Normalize a ZIP-code series to exactly 5 digits (strip, take first 5 chars)."""
    return raw.str.strip().str[:5].str.zfill(5)


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

    # Strip whitespace from all string columns
    for col in df.columns:
        df[col] = df[col].str.strip()

    df["county"] = county_label
    elapsed = time.time() - t0
    print(f"{len(df):,} rows in {elapsed:.1f}s")
    return df


# ── Main pipeline ──────────────────────────────────────────────────────────────

def run():
    print("=== NAL 2025 Data Pipeline ===\n")

    frames = []
    for county_label, (zip_name, csv_name) in COUNTIES.items():
        zip_path = RAW_DIR / zip_name
        if not zip_path.exists():
            print(f"  WARNING: {zip_path} not found — skipping {county_label}")
            continue
        df = _read_nal(zip_path, csv_name, county_label)
        frames.append(df)

    if not frames:
        raise RuntimeError("No county files found in data/raw/")

    print(f"\nCombining {len(frames)} counties …")
    df = pd.concat(frames, ignore_index=True)
    print(f"Total parcels: {len(df):,}\n")

    # ── Clean numeric fields ────────────────────────────────────────────────────
    df["JV"] = pd.to_numeric(df["JV"], errors="coerce")
    df["SALE_YR1"] = pd.to_numeric(df["SALE_YR1"], errors="coerce")

    # ── Normalize ZIP codes to 5 digits ────────────────────────────────────────
    df["prop_zip"] = _zip5(df["PHY_ZIPCD"].fillna(""))
    df["mail_zip"] = _zip5(df["OWN_ZIPCD"].fillna(""))
    # NAL OWN_STATE may be full name ("FLORIDA") or abbreviation ("FL")
    raw_state = df["OWN_STATE"].fillna("").str.strip().str.upper()
    df["mail_state"] = raw_state

    # Drop parcels with no valid property ZIP (unfixable / blank → "00000")
    pre = len(df)
    df = df[(df["prop_zip"].str.len() == 5) & (df["prop_zip"] != "00000")].copy()
    print(f"Dropped {pre - len(df):,} parcels with missing/invalid property ZIP")

    # Drop non-investable DOR land-use codes:
    #   080-099 = agricultural, conservation, government, exempt, right-of-way
    #   These inflate absentee rates for ZIPs that are mostly public land.
    dor = pd.to_numeric(df["DOR_UC"], errors="coerce")
    non_res_mask = (dor >= 80) & (dor <= 99)
    pre2 = len(df)
    df = df[~non_res_mask].copy()
    print(f"Dropped {pre2 - len(df):,} parcels with agricultural/exempt DOR codes (80-99)")

    # ── Flags ──────────────────────────────────────────────────────────────────
    # Absentee: mailing zip ≠ property zip (and mailing zip is not empty)
    df["absentee"] = (
        (df["mail_zip"] != df["prop_zip"]) &
        (df["mail_zip"] != "00000") &
        (df["mail_zip"] != "")
    ).astype(int)

    # Out-of-state: mailing state is not FL (NAL uses full name "FLORIDA")
    FL_VARIANTS = {"FL", "FLORIDA"}
    df["out_of_state"] = (
        (~df["mail_state"].isin(FL_VARIANTS)) &
        (df["mail_state"] != "")
    ).astype(int)

    # Years since last sale (use ASSESSMENT_YEAR as reference)
    df["years_since_sale"] = ASSESSMENT_YEAR - df["SALE_YR1"]
    # Cap unreasonable values: [0, 100]
    df["years_since_sale"] = df["years_since_sale"].clip(0, 100)

    print(f"Absentee owners:   {df['absentee'].mean()*100:.1f}%")
    print(f"Out-of-state:      {df['out_of_state'].mean()*100:.1f}%")

    # ── Aggregate to ZIP code ──────────────────────────────────────────────────
    print("\nAggregating to ZIP level …")

    agg = df.groupby("prop_zip").agg(
        property_count    = ("PARCEL_ID",       "count"),
        absentee_sum      = ("absentee",         "sum"),
        out_of_state_sum  = ("out_of_state",     "sum"),
        avg_years_since_sale = ("years_since_sale", "mean"),
        median_home_value = ("JV",               "median"),
    ).reset_index()

    agg["absentee_owner_rate"]   = (agg["absentee_sum"]     / agg["property_count"] * 100).round(2)
    agg["out_of_state_rate"]     = (agg["out_of_state_sum"] / agg["property_count"] * 100).round(2)
    agg["avg_years_ownership"]   = agg["avg_years_since_sale"].round(1)
    agg["median_home_value"]     = agg["median_home_value"].round(0).astype("Int64")

    # Most-common city name per ZIP
    city_map = (
        df[df["PHY_CITY"].notna()]
        .groupby("prop_zip")["PHY_CITY"]
        .agg(lambda x: x.value_counts().index[0])
    )
    agg["city"] = agg["prop_zip"].map(city_map).fillna("")

    # Most-common county per ZIP (usually one-to-one)
    county_map = (
        df.groupby("prop_zip")["county"]
        .agg(lambda x: x.value_counts().index[0])
    )
    agg["county"] = agg["prop_zip"].map(county_map)

    # Placeholders — not available in NAL
    agg["probate_activity_rate"] = 0.0
    agg["tax_delinquency_rate"]  = 0.0
    agg["preforeclosure_rate"]   = 0.0

    # ── Lat / Lon ──────────────────────────────────────────────────────────────
    def _get_lat_lon(row):
        z = row["prop_zip"]
        if z in ZIP_CENTROIDS:
            return ZIP_CENTROIDS[z]
        # Fall back to county centroid
        return COUNTY_CENTROIDS.get(row["county"], (25.775, -80.208))

    agg[["lat", "lon"]] = agg.apply(_get_lat_lon, axis=1, result_type="expand")

    # ── Filter: keep only ZIPs with ≥ 50 parcels ──────────────────────────────
    agg = agg[agg["property_count"] >= 50].copy()
    print(f"ZIP codes with ≥ 50 parcels: {len(agg)}")

    # ── Final column order (matches sample_data.csv) ──────────────────────────
    out = agg[[
        "prop_zip",
        "city",
        "county",
        "probate_activity_rate",
        "absentee_owner_rate",
        "tax_delinquency_rate",
        "preforeclosure_rate",
        "avg_years_ownership",
        "median_home_value",
        "lat",
        "lon",
        # Extra columns (not in sample_data but useful)
        "out_of_state_rate",
        "property_count",
    ]].rename(columns={"prop_zip": "zip_code"})

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUTPUT_CSV, index=False)
    print(f"\nWrote {len(out):,} ZIP codes → {OUTPUT_CSV}")

    # Summary
    print("\n── Top 10 by absentee_owner_rate ──")
    top = out.nlargest(10, "absentee_owner_rate")[
        ["zip_code", "city", "county", "absentee_owner_rate", "out_of_state_rate",
         "avg_years_ownership", "median_home_value", "property_count"]
    ]
    print(top.to_string(index=False))


if __name__ == "__main__":
    run()
