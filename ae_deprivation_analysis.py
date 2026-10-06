"""
NHS A&E 4-hour performance vs local deprivation (England, Jan-Jun 2026)

Question: does the level of deprivation in a trust's local area explain how
well the trust performs against the 4-hour A&E standard?

Pipeline
  1. Load the six monthly NHS England "A&E by provider" files
  2. Scope to comparable acute trusts (the exclusion funnel is printed at the end)
  3. Attach each trust's local authority via its HQ postcode
       NHS ODS trust list -> ONS Postcode Directory
  4. Attach IMD 2025 deprivation (LSOA deciles averaged to local authority)
  5. Test the relationship (trust-level Spearman correlation)
  6. Export the dataset used by the Power BI dashboard

Sources
  - NHS England: A&E Attendances and Emergency Admissions (Jan-Jun 2026)
  - NHS England ODS: NHS Trust list (etr)
  - ONS: ONS Postcode Directory (May 2026)
  - MHCLG: English Indices of Deprivation 2025
"""

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from scipy.stats import spearmanr

pd.set_option("display.width", None)
pd.set_option("display.max_columns", None)


# CONFIG
DATA_DIR = Path("C:/Users/DAVID/Downloads")

# Power BI reads the exported CSV from this folder. If you change it,
# re-point the Power BI data source to the new location.
OUTPUT_DIR = Path("C:/Users/DAVID/AppData/Roaming/JetBrains/PyCharm2025.3/scratches")

AE_FILES = {
    "2026-01": DATA_DIR / "January-2026-Provider-revised-lqb32.xls",
    "2026-02": DATA_DIR / "February-2026-AE-by-provider-lopYX.xls",
    "2026-03": DATA_DIR / "March-2026-AE-by-provider-sJh4x.xls",
    "2026-04": DATA_DIR / "April-26-AE-by-provider-v2-tKVXL.xls",
    "2026-05": DATA_DIR / "May-26-AE-by-provider-QVPHP.xls",
    "2026-06": DATA_DIR / "June-26-AE-by-provider-vVWw8.xls",
}
ODS_FILE = DATA_DIR / "etr (Include headers).csv"
ONSPD_FILE = DATA_DIR / "ONSPD_MAY_2026_UK.csv"
IMD_FILE = DATA_DIR / "File_2_IoD2025 Domains of Deprivation.csv"

PCT_COL = "Percentage in 4 hours or less (all)"
IMD_LAD_COL = "Local Authority District code (2024)"
IMD_DECILE_COL = (
    "Index of Multiple Deprivation (IMD) Decile (where 1 is most deprived 10% of LSOAs)"
)

TARGET = 0.76  # operational standard used for the "below target" flag

# Barnsley and Sheffield were given new local authority codes in April 2025.
# The ONS postcode file uses the new codes, the IMD file the old ones.
BOUNDARY_FIX = {
    "E08000038": "E08000016",  # Barnsley
    "E08000039": "E08000019",  # Sheffield
}

# The ODS file reflects trust status today. A trust that closed after the study
# started (e.g. North Bristol, closed 30 Jun 2026) was still reporting A&E data
# for all six months. False = drop every closed trust (matches the current
# dashboard). True = keep trusts that closed on/after STUDY_START.
KEEP_TRUSTS_CLOSED_DURING_STUDY = False
STUDY_START = 20260101

# 1. LOAD MONTHLY A&E FILES
def load_ae_month(path, month):
    """Read one monthly 'Provider Level Data' sheet and tidy it."""
    df = pd.read_excel(path, sheet_name="Provider Level Data", header=15, engine="xlrd")
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]  # drop spreadsheet padding
    df = df[(df["Code"] != "-") & (df["Name"].notna())].copy()  # drop England total + blank row
    df["Month"] = month
    return df

monthly = {month: load_ae_month(path, month) for month, path in AE_FILES.items()}
funnel = {}

# 2. SCOPE TO COMPARABLE ACUTE TRUSTS

# 2a. Keep only organisations that report in all six months. This removes
#     walk-in centres and urgent care sites that came and went during the year.
common_codes = set.intersection(*(set(df["Code"]) for df in monthly.values()))
panel = pd.concat(
    [df[df["Code"].isin(common_codes)] for df in monthly.values()], ignore_index=True
)
funnel["Report in all 6 months"] = panel["Code"].nunique()

# 2b. Drop organisations with no 4-hour percentage. These are specialist and
#     mental health trusts with no general A&E, so the metric does not apply.
panel[PCT_COL] = pd.to_numeric(panel[PCT_COL], errors="coerce")
panel = panel.dropna(subset=[PCT_COL])
funnel["Have a 4-hour % (general A&E)"] = panel["Code"].nunique()

# 2c. Keep only organisations that are real NHS trusts in the ODS list.
#     The rest are satellite sites that report under a parent trust.
ods = pd.read_csv(ODS_FILE)
is_open = ods["Close Date"].isna()
if KEEP_TRUSTS_CLOSED_DURING_STUDY:
    is_open |= ods["Close Date"] >= STUDY_START
locations = ods.loc[is_open, ["Organisation Code", "Postcode"]].rename(
    columns={"Organisation Code": "Code"}
)
panel = panel[panel["Code"].isin(locations["Code"])]
funnel["Matched to an ODS trust record"] = panel["Code"].nunique()

# 2d. A few trusts were renamed mid-year under the same code (e.g. RWW in
#     April). Use each code's most recent name so every trust appears once.
latest_name = panel.sort_values("Month").groupby("Code")["Name"].last()
panel["Name"] = panel["Code"].map(latest_name)


# 3. TRUST -> LOCAL AUTHORITY (via HQ postcode)
onspd = pd.read_csv(ONSPD_FILE, usecols=["pcds", "lad25cd"])  # 2 of 52 columns only
onspd = onspd[onspd["pcds"].isin(locations["Postcode"])]  # shrink 2.7m rows to ~200

locations = locations.merge(onspd, left_on="Postcode", right_on="pcds", how="left")
locations = locations[locations["Code"].isin(panel["Code"])].drop(columns="pcds")
assert locations["lad25cd"].notna().all(), "Some trusts have no local authority code"


# 4. LOCAL AUTHORITY -> DEPRIVATION
imd = pd.read_csv(IMD_FILE)
imd_by_la = (
    imd.groupby(IMD_LAD_COL)[IMD_DECILE_COL]
    .mean()  # average of LSOA deciles; 1 = most deprived, 10 = least deprived
    .rename("avg_imd_decile")
    .reset_index()
    .rename(columns={IMD_LAD_COL: "lad_code"})
)

locations["lad_for_imd_join"] = locations["lad25cd"].replace(BOUNDARY_FIX)
locations = locations.merge(
    imd_by_la, left_on="lad_for_imd_join", right_on="lad_code", how="left"
)
assert locations["avg_imd_decile"].notna().all(), "Some trusts have no IMD value"

final = panel.merge(
    locations[["Code", "lad_for_imd_join", "avg_imd_decile"]], on="Code", how="left"
)
assert len(final) == final["Code"].nunique() * len(AE_FILES), "Not 6 rows per trust"

# Flag trusts below target in every one of the six months
below = final.groupby("Code")[PCT_COL].apply(lambda x: (x < TARGET).all())
final["below_target_all_months"] = final["Code"].map(below)

# 5. ANALYSIS
monthly_avg = final.groupby("Month")[PCT_COL].mean()

# One row per trust, so each trust counts once in the correlation
trust_level = final.groupby(["Code", "Name"], as_index=False).agg(
    avg_performance=(PCT_COL, "mean"),
    worst_month=(PCT_COL, "min"),
    best_month=(PCT_COL, "max"),
    avg_imd_decile=("avg_imd_decile", "first"),
)

# Spearman because IMD deciles are ordinal ranks, not true measurements
rho, p_value = spearmanr(trust_level["avg_imd_decile"], trust_level["avg_performance"])
pearson = trust_level["avg_imd_decile"].corr(trust_level["avg_performance"])


# 6. OUTPUTS
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

fig, ax = plt.subplots()
ax.scatter(trust_level["avg_imd_decile"], trust_level["avg_performance"])
ax.set_xlabel("Average IMD decile (1 = most deprived)")
ax.set_ylabel("Average % seen within 4 hours")
ax.set_title("A&E performance vs local deprivation")
fig.savefig(OUTPUT_DIR / "deprivation_vs_performance.png", dpi=150, bbox_inches="tight")

# Column names are unchanged from earlier versions, so the Power BI model still works
final.to_csv(OUTPUT_DIR / "ae_deprivation_monthly.csv", index=False)


# SUMMARY
print("Trusts remaining at each step:")
for step, n in funnel.items():
    print(f"  {step}: {n}")
print(f"\nFinal dataset: {final.shape[0]} rows, {final['Code'].nunique()} trusts")

print("\nAverage 4-hour performance by month:")
print(monthly_avg.round(3).to_string())

print(f"\nTrusts below {TARGET:.0%} in every month: {below.sum()} of {len(below)}")

print("\n10 lowest-performing trusts (6-month average):")
print(trust_level.nsmallest(10, "avg_performance")[["Code", "Name", "avg_performance", "avg_imd_decile"]]
      .round(3).to_string(index=False))

print(f"\nDeprivation vs performance (n={len(trust_level)} trusts):")
print(f"  Spearman rho = {rho:.3f}, p = {p_value:.3f}")
print(f"  Pearson r    = {pearson:.3f}")

plt.show()