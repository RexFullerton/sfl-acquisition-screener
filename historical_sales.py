"""
historical_sales.py — Multi-year Miami-Dade sale history, built from real DOR records.

Validated against real NAL/SDF Final rolls, 2016-2025 (covering sale dates 2015-01
through 2025-12), received via a Florida DOR public-records request. This replaces
the earlier untested stub.

Qualification codes (arm's-length definition): verified stable across the whole
2015-2025 period against three DOR vintages of the "Real Property Transfer
Qualification Codes" document:
  - effective 2015-01-01 (Revised 10-03-2014) — via Wayback Machine, DOR no longer
    hosts this vintage: https://web.archive.org/web/20180426230727/http://floridarevenue.com/property/Documents/salequalcodes_bef01012016.pdf
  - effective 2018-01-01 (Revised 11-17-2017) — via Wayback Machine:
    https://web.archive.org/web/20210621225107/https://floridarevenue.com/property/Documents/salequalcodes_bef01012019.pdf
  - effective 2024-01-01 (Revised 9-14-2023) — live on DOR's site:
    https://floridarevenue.com/property/Documents/salequalcodes_bef01012025.pdf
Codes 01 and 02 ("qualified arm's length") are worded identically across all three.
One real change found: code 21 ("Contract for Deed; Agreement for Deed") was added
in the 2018 revision and does not exist in the 2015 vintage — it's a disqualified
code either way, so it does not affect the arm's-length set, but it means a sale
recorded before 2018 could not have carried that code.

Dedup key, per instruction: sales are only dated to the month in SDF, so
(parcel_id, sale_year, sale_month, sale_price) alone is not a reliable unique key —
a recording reference (OR_BOOK/OR_PAGE, or CLERK_NO where the clerk's office uses
instrument numbering instead) is included.
"""

import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

HIST_DIR = Path("data/raw/historical")
OUTPUT_PARQUET = Path("data/historical_sales.parquet")

YEARS = list(range(2016, 2026))  # Final-roll vintages received; each covers ~18-20mo of sale dates

ARMS_LENGTH_QUAL_CODES = {"01", "02"}

SDF_COLS = [
    "PARCEL_ID", "DOR_UC", "NBRHD_CD", "SALE_ID_CD", "VI_CD",
    "OR_BOOK", "OR_PAGE", "CLERK_NO", "QUAL_CD", "SALE_YR", "SALE_MO", "SALE_PRC",
    "MULTI_PAR_SAL",
]

NAL_STABILITY_COLS = ["PARCEL_ID", "OWN_NAME", "PHY_ADDR1"]


# ── Loading ──────────────────────────────────────────────────────────────────

def _read_sdf_year(year: int) -> pd.DataFrame:
    path = HIST_DIR / "SDF" / f"{year}F.zip"
    with zipfile.ZipFile(path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            df = pd.read_csv(fh, usecols=SDF_COLS, dtype=str, keep_default_na=False, na_values=[""])
    for c in df.columns:
        df[c] = df[c].str.strip()
    df["source_year"] = year
    return df


def _read_nal_stability_cols(year: int) -> pd.DataFrame:
    path = HIST_DIR / "NAL" / f"{year}F.zip"
    with zipfile.ZipFile(path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            df = pd.read_csv(fh, usecols=NAL_STABILITY_COLS, dtype=str, keep_default_na=False, na_values=[""])
    for c in df.columns:
        df[c] = df[c].str.strip()
    return df


def _normalize_addr(s: pd.Series) -> pd.Series:
    s = s.fillna("").str.upper().str.strip()
    s = s.str.replace(r"[^\w\s]", "", regex=True)
    s = s.str.replace(r"\s+", " ", regex=True)
    return s


# ── Recording reference ─────────────────────────────────────────────────────

def build_recording_ref(df: pd.DataFrame) -> pd.Series:
    """
    (OR_BOOK, OR_PAGE) when the clerk's office uses that system, else
    "CLERK:<CLERK_NO>" when it uses instrument numbering, else NA (neither present).
    """
    or_book = df["OR_BOOK"]
    or_page = df["OR_PAGE"]
    clerk = df["CLERK_NO"]

    has_orbook = or_book.notna() & (or_book != "")
    has_clerk = (~has_orbook) & clerk.notna() & (clerk != "")

    ref = pd.Series(pd.NA, index=df.index, dtype="object")
    ref.loc[has_orbook] = or_book[has_orbook] + "-" + or_page[has_orbook].fillna("")
    ref.loc[has_clerk] = "CLERK:" + clerk[has_clerk]
    return ref


# ── No-lookahead guard (built now, used starting Step 3) ───────────────────

class LookaheadError(AssertionError):
    pass


def assert_no_lookahead(sales: pd.DataFrame, as_of: pd.Timestamp, sale_date_col: str = "sale_date") -> None:
    """
    Raise LookaheadError if any row in `sales` has a sale date on or after `as_of`.
    Call this on every comp pool / ARV input / score input built for a given as-of
    date, in steps 3 and 4. A leaking back-test is worse than none — this makes
    leakage a hard failure, not a hope.
    """
    if sale_date_col not in sales.columns:
        raise LookaheadError(f"'{sale_date_col}' column not present — cannot verify no-lookahead.")
    leaking = sales[sales[sale_date_col] >= as_of]
    if len(leaking) > 0:
        raise LookaheadError(
            f"{len(leaking)} row(s) with sale_date >= as_of ({as_of.date()}) leaked into an "
            f"as-of-{as_of.date()} input. First offending parcel_id(s): "
            f"{leaking['parcel_id'].head(5).tolist() if 'parcel_id' in leaking.columns else '(no parcel_id col)'}"
        )


# ── Main build ───────────────────────────────────────────────────────────────

def build():
    print("=== Loading SDF, all years ===")
    frames = []
    qual_counts_by_year = {}
    for y in YEARS:
        df = _read_sdf_year(y)
        qual_counts_by_year[y] = df["QUAL_CD"].value_counts(dropna=False)
        df["recording_ref"] = build_recording_ref(df)
        frames.append(df)
        print(f"  {y}F: {len(df):,} rows")

    raw = pd.concat(frames, ignore_index=True)
    total_raw = len(raw)
    print(f"\nTotal raw SDF rows across all years: {total_raw:,}")

    raw["SALE_PRC"] = pd.to_numeric(raw["SALE_PRC"], errors="coerce")
    raw["SALE_YR"] = pd.to_numeric(raw["SALE_YR"], errors="coerce")
    raw["SALE_MO"] = pd.to_numeric(raw["SALE_MO"], errors="coerce")

    no_ref = raw["recording_ref"].isna().sum()
    print(f"Rows with no recording reference at all (neither OR_BOOK/PAGE nor CLERK_NO): "
          f"{no_ref:,} ({no_ref/total_raw*100:.2f}%) — these can only be dedup'd/conflict-checked "
          f"on (parcel, year, month, price), not verified against a recording instrument.")

    print("\n=== Qualification code distribution by year (raw SDF, pre-dedup) ===")
    for y in YEARS:
        vc = qual_counts_by_year[y]
        print(f"  {y}F: " + ", ".join(f"{code}={n:,}" for code, n in vc.items()))

    # ── Dedup ────────────────────────────────────────────────────────────────
    raw = raw.sort_values("source_year")  # so keep='last' below prefers the later file
    dedup_key = ["PARCEL_ID", "SALE_YR", "SALE_MO", "SALE_PRC", "recording_ref"]

    exact_dupe_count = raw.duplicated(subset=dedup_key, keep="last").sum()
    deduped = raw.drop_duplicates(subset=dedup_key, keep="last").copy()
    print(f"\n=== Dedup step 1: exact duplicates ===")
    print(f"Exact duplicate rows removed (identical parcel+year+month+price+recording_ref, "
          f"differing only by which vintage file reported them): {exact_dupe_count:,}")
    print(f"Remaining after exact-dedup: {len(deduped):,}")

    # Conflict detection: same real transaction (parcel + recording_ref), but the
    # exact-key dedup above did NOT collapse it -- meaning year/month/price/qual
    # disagree across vintages for what should be the same recorded instrument.
    has_ref = deduped["recording_ref"].notna()
    ref_groups = deduped[has_ref].groupby(["PARCEL_ID", "recording_ref"])
    group_sizes = ref_groups.size()
    conflicting_keys = group_sizes[group_sizes > 1]
    n_conflict_groups = len(conflicting_keys)
    n_conflict_rows = int(conflicting_keys.sum())

    print(f"\n=== Dedup step 2: conflicting duplicates (same parcel + recording ref, "
          f"different year/month/price/qual across vintages) ===")
    print(f"Conflicting transaction groups found: {n_conflict_groups:,} "
          f"(spanning {n_conflict_rows:,} rows before resolution)")

    if n_conflict_groups > 0:
        conflict_idx = deduped[has_ref].set_index(["PARCEL_ID", "recording_ref"]).index.isin(conflicting_keys.index)
        conflict_rows = deduped[has_ref][conflict_idx]
        # keep the row with the max source_year per (PARCEL_ID, recording_ref) group
        keep_idx = conflict_rows.groupby(["PARCEL_ID", "recording_ref"])["source_year"].idxmax()
        rows_to_drop = conflict_rows.index.difference(keep_idx)
        print(f"Resolved by keeping the row from the latest source_year per conflicting group "
              f"('prefer the later file'); dropped {len(rows_to_drop):,} superseded rows.")
        deduped = deduped.drop(index=rows_to_drop)
    else:
        print("None found.")

    print(f"\nDistinct sales after full dedup: {len(deduped):,}")

    # ── Harmonized qualified flag ───────────────────────────────────────────
    deduped["qualified"] = deduped["QUAL_CD"].isin(ARMS_LENGTH_QUAL_CODES)

    # ── Sale date, output schema ────────────────────────────────────────────
    deduped["sale_date"] = pd.to_datetime(
        {"year": deduped["SALE_YR"], "month": deduped["SALE_MO"].where(deduped["SALE_MO"].between(1, 12)), "day": 1},
        errors="coerce",
    )

    # MULTI_PAR_SAL is orthogonal to QUAL_CD: a sale can be "qualified" (01/02) AND
    # part of a multi-parcel transaction at the same time -- meaning the recorded
    # price covers a bundle of parcels, not this one. Comp-usable arm's-length
    # sales must exclude these regardless of qual_code. See README "Qualification
    # codes" for why this isn't code 02 (checked against 3 DOR vintages: 02 has
    # always meant "documented evidence," never multi-parcel -- code 05 is the
    # dedicated multi-parcel qual code, but MULTI_PAR_SAL can be set independently
    # of qual_code entirely, which is the actual risk to guard against here).
    deduped["multi_parcel_sale"] = deduped["MULTI_PAR_SAL"].notna() & (deduped["MULTI_PAR_SAL"] != "")
    deduped["comp_eligible"] = deduped["qualified"] & (~deduped["multi_parcel_sale"])

    n_qualified = int(deduped["qualified"].sum())
    n_qualified_multi = int((deduped["qualified"] & deduped["multi_parcel_sale"]).sum())
    print(f"\n=== Multi-parcel contamination check ===")
    print(f"Qualified (01/02) sales: {n_qualified:,}")
    print(f"Of those, also flagged MULTI_PAR_SAL (price covers a parcel bundle, not this "
          f"one parcel alone): {n_qualified_multi:,} ({n_qualified_multi/n_qualified*100:.2f}%) "
          f"-- excluded from comp_eligible regardless of qual_code.")
    print(f"comp_eligible (qualified AND NOT multi-parcel) sales: {int(deduped['comp_eligible'].sum()):,}")

    out = deduped.rename(columns={
        "PARCEL_ID": "parcel_id", "SALE_YR": "sale_year", "SALE_MO": "sale_month",
        "SALE_PRC": "price", "QUAL_CD": "qual_code", "VI_CD": "vi_code",
        "DOR_UC": "dor_use_code",
    })[[
        "parcel_id", "sale_year", "sale_month", "price", "recording_ref",
        "qual_code", "qualified", "multi_parcel_sale", "comp_eligible", "vi_code",
        "dor_use_code", "sale_date", "source_year",
    ]]

    out.to_parquet(OUTPUT_PARQUET, index=False)
    print(f"\nWrote {len(out):,} deduplicated sales -> {OUTPUT_PARQUET}")

    print("\n=== Qualified (01/02) single-family (DOR_UC=001) sales by sale year, 2015-2025 ===")
    sf_qualified = out[out["qualified"] & (out["dor_use_code"] == "001")]
    print(sf_qualified["sale_year"].value_counts().sort_index().to_string())

    print("\n=== Sale-date coverage of the deduplicated table ===")
    print(f"min: {out['sale_date'].min()}   max: {out['sale_date'].max()}")

    return out


# ── Parcel ID stability across NAL vintages ────────────────────────────────

def parcel_stability_report():
    print("\n=== Parcel ID stability across NAL vintages, 2016-2025 ===")
    nal = {y: _read_nal_stability_cols(y) for y in YEARS}
    id_sets = {y: set(nal[y]["PARCEL_ID"]) for y in YEARS}

    all_ids = set.union(*id_sets.values())
    in_all_years = set.intersection(*id_sets.values())
    print(f"Union of parcel IDs across all 10 years: {len(all_ids):,}")
    print(f"Parcel IDs present in EVERY year (2016-2025): {len(in_all_years):,} "
          f"({len(in_all_years)/len(all_ids)*100:.1f}% of the union)")

    total_disappear = 0
    total_matched_new_id = 0
    total_true_drop = 0
    for y in YEARS[:-1]:
        y2 = y + 1
        disappearing = id_sets[y] - id_sets[y2]
        if not disappearing:
            continue
        left = nal[y][nal[y]["PARCEL_ID"].isin(disappearing)].copy()
        left["addr_key"] = _normalize_addr(left["OWN_NAME"].fillna("") + "|" + left["PHY_ADDR1"].fillna(""))

        right_keys = set(
            _normalize_addr(nal[y2]["OWN_NAME"].fillna("") + "|" + nal[y2]["PHY_ADDR1"].fillna(""))
        )
        matched = left["addr_key"].isin(right_keys).sum()
        dropped = len(left) - matched
        total_disappear += len(left)
        total_matched_new_id += matched
        total_true_drop += dropped
        print(f"  {y}->{y2}: {len(left):,} parcel IDs disappeared; {matched:,} have the same "
              f"(owner name + situs address) reappearing under a different ID in {y2} "
              f"(likely split/combine/renumber); {dropped:,} do not match anything in {y2} "
              f"(true drop — combined into a parcel with a different address representation, "
              f"or genuinely removed from the roll).")

    print(f"\nTotals across all 9 year-to-year transitions: {total_disappear:,} disappearances, "
          f"{total_matched_new_id:,} likely renumbered ({total_matched_new_id/total_disappear*100:.1f}%), "
          f"{total_true_drop:,} true drops ({total_true_drop/total_disappear*100:.1f}%).")
    print("Sales tied to a 'likely renumbered' old parcel_id are NOT automatically relinked to "
          "the new ID in historical_sales.py -- flagging this here rather than silently dropping "
          "or silently merging. Relinking would need a decision on which ID to canonicalize on; "
          "not done without sign-off.")


NAL_SNAPSHOT_COLS = ["PARCEL_ID", "DOR_UC", "NBRHD_CD", "TOT_LVG_AREA", "ACT_YR_BLT", "JV",
                      "PHY_ADDR1", "PHY_CITY", "PHY_ZIPCD"]
NAL_SNAPSHOTS_PARQUET = Path("data/nal_snapshots.parquet")


def build_nal_snapshots(dor_use_codes=("001",)) -> pd.DataFrame:
    """
    One row per (parcel, vintage_year), single-family only by default -- the
    per-year characteristics needed for the same-vintage comp rule (a comp's
    living area/year built/neighborhood as they were assessed in the comp's OWN
    sale year, not as of today's roll) and for scoring a subject's own as-of
    characteristics.
    """
    frames = []
    for y in YEARS:
        path = HIST_DIR / "NAL" / f"{y}F.zip"
        with zipfile.ZipFile(path) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as fh:
                df = pd.read_csv(fh, usecols=NAL_SNAPSHOT_COLS, dtype=str, keep_default_na=False, na_values=[""])
        for c in df.columns:
            df[c] = df[c].str.strip()
        df = df[df["DOR_UC"].isin(dor_use_codes)].copy()
        df["vintage_year"] = y
        frames.append(df)
        print(f"  {y}F NAL: {len(df):,} single-family parcels")

    out = pd.concat(frames, ignore_index=True).rename(columns={
        "PARCEL_ID": "parcel_id", "NBRHD_CD": "nbrhd_cd", "TOT_LVG_AREA": "living_area",
        "ACT_YR_BLT": "year_built", "JV": "just_value", "PHY_ADDR1": "situs_addr1",
        "PHY_CITY": "situs_city", "PHY_ZIPCD": "situs_zip",
    })
    out["living_area"] = pd.to_numeric(out["living_area"], errors="coerce")
    out["year_built"] = pd.to_numeric(out["year_built"], errors="coerce")
    out["just_value"] = pd.to_numeric(out["just_value"], errors="coerce")

    out.to_parquet(NAL_SNAPSHOTS_PARQUET, index=False)
    print(f"Wrote {len(out):,} parcel-vintage snapshots -> {NAL_SNAPSHOTS_PARQUET}")
    return out


if __name__ == "__main__":
    build()
    parcel_stability_report()
    print("\n=== Building per-vintage NAL snapshots (single-family) ===")
    build_nal_snapshots()
