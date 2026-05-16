import argparse
import json
import os
import sys

import fiona
import pandas as pd
import geopandas as gpd


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Generate per-feature GeoJSON files for reservoirs and barriers.")
    # Reservoir inputs
    p.add_argument("--reservoirs_csv",            default="Dataset/GBMIM_Reservoirs.csv",
                   help="CSV for reservoirs (id, lat, lng, ...)")
    p.add_argument("--reservoirs_downstream_dir", default="Dataset/Downstream/Reservoir",
                   help="Directory containing {id}_downstream_path.gpkg for reservoirs")
    # Barrier inputs
    p.add_argument("--barriers_csv",              default="Dataset/GBMIM_Barriers.csv",
                   help="CSV for barriers (id, lat, lng, ...)")
    p.add_argument("--barriers_downstream_dir",   default="Dataset/Downstream/Barrier",
                   help="Directory containing {id}_downstream_path.gpkg for barriers")
    # Shared inputs
    p.add_argument("--watersheds",       default="Dataset/Merged_Catchment/GBMIM_barrier_merged_watersheds_filled.gpkg")
    p.add_argument("--watershed_layer",  default=None)
    p.add_argument("--watershed_id_col", default="GDW_ID")
    # Runoff time series
    p.add_argument("--runoff_reservoir_dir", default="Dataset/data/runoff_reservoir",
                   help="Directory containing GDWID_{id}.txt time series for reservoirs")
    p.add_argument("--runoff_barrier_dir",   default="Dataset/data/runoff_barrier",
                   help="Directory containing GDWID_{id}.txt time series for barriers")
    # Output
    p.add_argument("--geojson_dir",      default="geojson")
    p.add_argument("--line_simplify",    type=float, default=0.001,
                   help="Simplification tolerance for downstream lines in degrees "
                        "(default 0.001 ~ ~100m). Set 0 to disable.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalise_id(val) -> str:
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


# Extra columns included in coordinate JSON if present in CSV
EXTRA_COLS = ["country", "Basin", "Areal_Impact (%)"]


def load_features_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    required = {"id", "lat", "lng"}
    missing = required - set(df.columns.str.lower())
    if missing:
        sys.exit(f"[ERROR] {csv_path} is missing columns: {missing}")
    df["id"]  = df["id"].apply(normalise_id)
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lng"] = pd.to_numeric(df["lng"], errors="coerce")
    df = df.dropna(subset=["lat", "lng"])
    keep = ["id", "lat", "lng"] + [c for c in EXTRA_COLS if c in df.columns]
    found_extra = [c for c in EXTRA_COLS if c in df.columns]
    if found_extra:
        print(f"      Extra columns included: {found_extra}")
    return df[keep]


def load_watersheds(gpkg_path: str, layer: str = None, id_col: str = None) -> gpd.GeoDataFrame:
    layers = fiona.listlayers(gpkg_path)
    layer = layer or layers[0]
    gdf = gpd.read_file(gpkg_path, layer=layer).to_crs(epsg=4326)

    print(f"      Columns       : {gdf.columns.tolist()}")
    print(f"      Total features: {len(gdf)}")

    if id_col:
        if id_col not in gdf.columns:
            sys.exit(f"[ERROR] Column '{id_col}' not found. Available: {gdf.columns.tolist()}")
        matched = id_col
    else:
        matched = next((c for c in gdf.columns if c.lower() == "id"), None)
        if matched is None:
            non_geom = [c for c in gdf.columns if c.lower() != "geometry"]
            if not non_geom:
                sys.exit("[ERROR] No usable id column found.")
            matched = non_geom[0]
            print(f"      [INFO] No 'id' column - using '{matched}'.")

    print(f"      Using column  : '{matched}'")
    gdf = gdf.rename(columns={matched: "id"})
    gdf["id"] = gdf["id"].apply(normalise_id)
    print(f"      Sample ids    : {gdf['id'].head(5).tolist()}")
    return gdf


def load_downstream_gdf(gpkg_dir: str, feature_id: str):
    path = os.path.join(gpkg_dir, f"{feature_id}_downstream_path.gpkg")
    if not os.path.exists(path):
        return None
    try:
        return gpd.read_file(path).to_crs(epsg=4326)
    except Exception as e:
        print(f"[WARN] Could not read {path}: {e}")
        return None


# Columns expected in the runoff CSVs (Date is the index)
RUNOFF_METRICS = ["Surface", "Sub_Surface", "Precip"]

def load_runoff_series(runoff_dir: str, feature_id: str):
    path = os.path.join(runoff_dir, f"GDWID_{feature_id}.txt")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
    except Exception as e:
        print(f"[WARN] Could not read {path}: {e}")
        return None

    if "Date" not in df.columns:
        print(f"[WARN] {path} missing 'Date' column")
        return None

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date")
    dates = df["Date"].dt.strftime("%Y-%m").tolist()

    out = {"dates": dates}
    for col in RUNOFF_METRICS:
        if col in df.columns:
            out[col] = [None if pd.isna(v) else float(f"{v:.6g}") for v in df[col]]
        else:
            out[col] = []
    return out


# ---------------------------------------------------------------------------
# Per-type processor
# ---------------------------------------------------------------------------

def process_features(kind, csv_path, downstream_dir, runoff_dir,
                     watershed_index, out_subdir, coords_filename,
                     geojson_dir, line_simplify):
    print(f"\n[Processing {kind}s]")
    if not os.path.exists(csv_path):
        print(f"  [SKIP] CSV not found: {csv_path}")
        # Write empty list so the front-end always finds the file
        coords_path = os.path.join(geojson_dir, coords_filename)
        with open(coords_path, "w") as f:
            f.write("[]")
        print(f"  Wrote empty {coords_path}")
        return 0

    print(f"  Loading {kind} CSV ...")
    features_df = load_features_csv(csv_path)
    print(f"  {len(features_df)} {kind}s found.")

    # Coordinate JSON
    coords_path = os.path.join(geojson_dir, coords_filename)
    with open(coords_path, "w", encoding="utf-8") as f:
        json.dump(features_df.to_dict(orient="records"), f, separators=(",", ":"))
    print(f"  Coordinates -> {coords_path}  ({os.path.getsize(coords_path)/1024:.1f} KB)")

    # ID overlap check (against shared watershed gpkg)
    feat_ids = set(features_df["id"])
    overlap = feat_ids & set(watershed_index.keys())
    print(f"  Watershed match: {len(overlap)} / {len(feat_ids)}")

    # Per-feature GeoJSON + time series
    ws_written = ds_written = ds_missing = ts_written = ts_missing = 0
    ws_bytes   = ds_bytes   = ts_bytes   = 0

    for rid in features_df["id"]:
        # Watershed
        if rid in watershed_index:
            ws_path = os.path.join(out_subdir, f"watershed_{rid}.geojson")
            watershed_index[rid].to_file(ws_path, driver="GeoJSON")
            ws_bytes += os.path.getsize(ws_path)
            ws_written += 1

        # Downstream
        ds_gdf = load_downstream_gdf(downstream_dir, rid)
        if ds_gdf is not None:
            if line_simplify > 0:
                ds_gdf = ds_gdf.copy()
                ds_gdf["geometry"] = ds_gdf["geometry"].simplify(
                    line_simplify, preserve_topology=True
                )
            ds_path = os.path.join(out_subdir, f"downstream_{rid}.geojson")
            ds_gdf.to_file(ds_path, driver="GeoJSON")
            ds_bytes += os.path.getsize(ds_path)
            ds_written += 1
        else:
            ds_missing += 1

        # Time series (runoff)
        ts_series = load_runoff_series(runoff_dir, rid)
        if ts_series is not None:
            ts_path = os.path.join(out_subdir, f"timeseries_{rid}.json")
            with open(ts_path, "w", encoding="utf-8") as f:
                json.dump(ts_series, f, separators=(",", ":"))
            ts_bytes += os.path.getsize(ts_path)
            ts_written += 1
        else:
            ts_missing += 1

    total_mb = (ws_bytes + ds_bytes + ts_bytes) / (1024 * 1024)
    print(f"  {kind.capitalize()} watershed : {ws_written} files")
    print(f"  {kind.capitalize()} downstream: {ds_written} files  [{ds_missing} missing]")
    print(f"  {kind.capitalize()} timeseries: {ts_written} files  [{ts_missing} missing]")
    print(f"  {kind.capitalize()} total     : {total_mb:.1f} MB  -> {out_subdir}/")
    return total_mb


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    res_dir = os.path.join(args.geojson_dir, "reservoir")
    bar_dir = os.path.join(args.geojson_dir, "barrier")
    os.makedirs(res_dir, exist_ok=True)
    os.makedirs(bar_dir, exist_ok=True)

    print(f"Output structure:")
    print(f"  {args.geojson_dir}/reservoir.json  <- reservoir coordinates")
    print(f"  {args.geojson_dir}/barrier.json    <- barrier coordinates")
    print(f"  {res_dir}/                          <- per-reservoir GeoJSON + timeseries")
    print(f"  {bar_dir}/                          <- per-barrier GeoJSON + timeseries")
    if args.line_simplify > 0:
        print(f"Downstream lines simplified at tolerance={args.line_simplify} degrees")

    # Shared watershed layer (loaded once, used for both reservoirs and barriers)
    print("\n[Loading watershed polygons]")
    if not os.path.exists(args.watersheds):
        sys.exit(f"[ERROR] Watersheds GPKG not found: {args.watersheds}")
    watershed_gdf = load_watersheds(args.watersheds, args.watershed_layer, args.watershed_id_col)
    watershed_index = {rid: grp for rid, grp in watershed_gdf.groupby("id")}

    # Reservoirs
    res_mb = process_features(
        kind="reservoir",
        csv_path=args.reservoirs_csv,
        downstream_dir=args.reservoirs_downstream_dir,
        runoff_dir=args.runoff_reservoir_dir,
        watershed_index=watershed_index,
        out_subdir=res_dir,
        coords_filename="reservoir.json",
        geojson_dir=args.geojson_dir,
        line_simplify=args.line_simplify,
    )

    # Barriers
    bar_mb = process_features(
        kind="barrier",
        csv_path=args.barriers_csv,
        downstream_dir=args.barriers_downstream_dir,
        runoff_dir=args.runoff_barrier_dir,
        watershed_index=watershed_index,
        out_subdir=bar_dir,
        coords_filename="barrier.json",
        geojson_dir=args.geojson_dir,
        line_simplify=args.line_simplify,
    )

    print(f"\n  Grand total: {(res_mb + bar_mb):.1f} MB")
    print(f"  Output: {os.path.abspath(args.geojson_dir)}/\n")


if __name__ == "__main__":
    main()