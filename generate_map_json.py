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
    p = argparse.ArgumentParser(description="Generate per-reservoir GeoJSON files.")
    p.add_argument("--csv",              default="Dataset/GBMIM_Barriers.csv")
    p.add_argument("--watersheds",       default="Dataset/Merged_Catchment/GBMIM_barrier_merged_watersheds_filled.gpkg")
    p.add_argument("--downstream_dir",   default="Dataset/Downstream/Barrier")
    p.add_argument("--geojson_dir",      default="geojson")
    p.add_argument("--barriers_csv",      default=None,
                   help="CSV for barriers (id, lat, lng). If provided, writes to geojson/barrier/")
    p.add_argument("--barriers_dir",      default=None,
                   help="Directory containing {id}_downstream_path.gpkg for barriers")
    p.add_argument("--watershed_layer",  default=None)
    p.add_argument("--watershed_id_col", default="GDW_ID")
    p.add_argument("--line_simplify",    type=float, default=0.001,
                   help="Simplification tolerance for downstream lines in degrees "
                        "(default 0.001 ≈ ~100m). Set 0 to disable.")
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

def normalise_col(c: str) -> str:
    return c.strip().lower().replace(" ", "_").replace("(", "").replace(")", "").replace("%", "")

def load_reservoirs(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    required = {"id", "lat", "lng"}
    missing = required - set(df.columns.str.lower())
    if missing:
        sys.exit(f"[ERROR] CSV is missing columns: {missing}")
    df.columns = [c for c in df.columns]
    df["id"] = df["id"].apply(normalise_id)
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
            print(f"      [INFO] No 'id' column — using '{matched}'.")

    print(f"      Using column  : '{matched}'")
    gdf = gdf.rename(columns={matched: "id"})
    gdf["id"] = gdf["id"].apply(normalise_id)
    print(f"      Sample ids    : {gdf['id'].head(5).tolist()}")
    return gdf


def load_downstream_gdf(gpkg_dir: str, reservoir_id: str):
    path = os.path.join(gpkg_dir, f"{reservoir_id}_downstream_path.gpkg")
    if not os.path.exists(path):
        return None
    try:
        return gpd.read_file(path).to_crs(epsg=4326)
    except Exception as e:
        print(f"[WARN] Could not read {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    # Subfolders: geojson/reservoir/ and geojson/barrier/
    res_dir = os.path.join(args.geojson_dir, "reservoir")
    bar_dir = os.path.join(args.geojson_dir, "barrier")
    os.makedirs(res_dir, exist_ok=True)
    os.makedirs(bar_dir, exist_ok=True)

    print(f"Output structure:")
    print(f"  {res_dir}/  <- reservoir GeoJSON files")
    print(f"  {bar_dir}/  <- barrier GeoJSON files")
    if args.line_simplify > 0:
        print(f"Downstream lines simplified at tolerance={args.line_simplify} degrees")

    # 1. Reservoirs
    print("\n[1/3] Loading reservoirs CSV ...")
    reservoirs_df = load_reservoirs(args.csv)
    print(f"      {len(reservoirs_df)} reservoirs found.")

    # Write coordinate JSON  →  geojson/reservoirs.json
    res_coords_path = os.path.join(args.geojson_dir, "reservoirs.json")
    with open(res_coords_path, "w", encoding="utf-8") as f:
        json.dump(reservoirs_df.to_dict(orient="records"), f, separators=(",",":"))
    print(f"      Coordinates  ->  {res_coords_path}  ({os.path.getsize(res_coords_path)/1024:.1f} KB)")

    # 2. Watersheds
    print("\n[2/3] Loading watershed polygons ...")
    watershed_gdf = load_watersheds(
        args.watersheds, args.watershed_layer, args.watershed_id_col
    )

    # ID overlap check
    res_ids = set(reservoirs_df["id"])
    ws_ids  = set(watershed_gdf["id"])
    overlap = res_ids & ws_ids
    print(f"      === ID OVERLAP ===")
    print(f"      CSV reservoirs : {len(res_ids)}")
    print(f"      Watershed ids  : {len(ws_ids)}")
    print(f"      Matched        : {len(overlap)}")
    if not overlap:
        print("      [WARNING] No matching ids — check id column and format.")

    watershed_index = {rid: grp for rid, grp in watershed_gdf.groupby("id")}

    # 3. Write reservoir GeoJSON → geojson/reservoir/
    print("\n[3/3] Writing GeoJSON files ...")
    ws_written = ds_written = ds_missing = 0
    ws_bytes   = ds_bytes   = 0

    for rid in reservoirs_df["id"]:
        # Watershed (no simplification)
        if rid in watershed_index:
            ws_path = os.path.join(res_dir, f"watershed_{rid}.geojson")
            watershed_index[rid].to_file(ws_path, driver="GeoJSON")
            ws_bytes += os.path.getsize(ws_path)
            ws_written += 1

        # Downstream (lines simplified)
        ds_gdf = load_downstream_gdf(args.downstream_dir, rid)
        if ds_gdf is not None:
            if args.line_simplify > 0:
                ds_gdf = ds_gdf.copy()
                ds_gdf["geometry"] = ds_gdf["geometry"].simplify(
                    args.line_simplify, preserve_topology=True
                )
            ds_path = os.path.join(res_dir, f"downstream_{rid}.geojson")
            ds_gdf.to_file(ds_path, driver="GeoJSON")
            ds_bytes += os.path.getsize(ds_path)
            ds_written += 1
        else:
            ds_missing += 1

    res_mb = (ws_bytes + ds_bytes) / (1024 * 1024)
    print(f"      Reservoir watershed : {ws_written} files")
    print(f"      Reservoir downstream: {ds_written} files  [{ds_missing} missing]")
    print(f"      Reservoir total     : {res_mb:.1f} MB  -> {res_dir}/")

    # 4. Write barrier GeoJSON → geojson/barrier/  (if provided)
    bar_ws_written = bar_ds_written = bar_ds_missing = 0
    bar_bytes = 0

    if args.barriers_csv and os.path.exists(args.barriers_csv):
        print(f"\n[4/4] Writing barrier GeoJSON files ...")
        barriers_df = load_reservoirs(args.barriers_csv)
        print(f"      {len(barriers_df)} barriers found.")

        # Write coordinate JSON  →  geojson/barriers.json
        bar_coords_path = os.path.join(args.geojson_dir, "barriers.json")
        with open(bar_coords_path, "w", encoding="utf-8") as f:
            json.dump(barriers_df.to_dict(orient="records"), f, separators=(",",":"))
        print(f"      Coordinates  ->  {bar_coords_path}  ({os.path.getsize(bar_coords_path)/1024:.1f} KB)")

        bar_downstream_dir = args.barriers_dir or args.downstream_dir

        # Barriers typically share the same watershed gpkg — load it
        bar_watershed_gdf = load_watersheds(
            args.watersheds, args.watershed_layer, args.watershed_id_col
        ) if os.path.exists(args.watersheds) else None

        bar_ws_index = {}
        if bar_watershed_gdf is not None:
            bar_ws_index = {rid: grp for rid, grp in bar_watershed_gdf.groupby("id")}

        for rid in barriers_df["id"]:
            if rid in bar_ws_index:
                ws_path = os.path.join(bar_dir, f"watershed_{rid}.geojson")
                bar_ws_index[rid].to_file(ws_path, driver="GeoJSON")
                bar_bytes += os.path.getsize(ws_path)
                bar_ws_written += 1

            ds_gdf = load_downstream_gdf(bar_downstream_dir, rid)
            if ds_gdf is not None:
                if args.line_simplify > 0:
                    ds_gdf = ds_gdf.copy()
                    ds_gdf["geometry"] = ds_gdf["geometry"].simplify(
                        args.line_simplify, preserve_topology=True
                    )
                ds_path = os.path.join(bar_dir, f"downstream_{rid}.geojson")
                ds_gdf.to_file(ds_path, driver="GeoJSON")
                bar_bytes += os.path.getsize(ds_path)
                bar_ds_written += 1
            else:
                bar_ds_missing += 1

        bar_mb = bar_bytes / (1024 * 1024)
        print(f"      Barrier watershed : {bar_ws_written} files")
        print(f"      Barrier downstream: {bar_ds_written} files  [{bar_ds_missing} missing]")
        print(f"      Barrier total     : {bar_mb:.1f} MB  -> {bar_dir}/")
    else:
        # Write empty barriers.json so the map always finds the file
        bar_coords_path = os.path.join(args.geojson_dir, "barriers.json")
        with open(bar_coords_path, "w") as f:
            f.write("[]")
        print("\n  No barriers CSV — wrote empty barriers.json.")
        print("  Pass --barriers_csv <path> to include barriers.")

    total_mb = (ws_bytes + ds_bytes + bar_bytes) / (1024 * 1024)
    print(f"\n  Grand total: {total_mb:.1f} MB")
    print(f"  Output: {os.path.abspath(args.geojson_dir)}/\n")


if __name__ == "__main__":
    main()