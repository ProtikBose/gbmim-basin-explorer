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
    p = argparse.ArgumentParser(description="Generate interactive reservoir map.")
    p.add_argument("--csv", default="Dataset/GBMIM_Reservoirs.csv")
    p.add_argument("--watersheds", default="Dataset/Merged_Catchment/GBMIM_reservoir_merged_watersheds_filled.gpkg")
    p.add_argument("--downstream_dir", default="Dataset/Downstream/Reservoir")
    p.add_argument("--output", default="map.html")
    p.add_argument("--geojson_dir", default="geojson/reservoir",
                   help="Output dir for GeoJSON files (must sit next to map.html)")
    p.add_argument("--watershed_layer", default=None)
    p.add_argument("--watershed_id_col", default="GDW_ID")
    p.add_argument("--simplify", type=float, default=0.005)
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalise_id(val) -> str:
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def load_reservoirs(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    required = {"id", "lat", "lng"}
    missing = required - set(df.columns.str.lower())
    if missing:
        sys.exit(f"[ERROR] CSV is missing columns: {missing}")
    df.columns = df.columns.str.lower()
    df["id"] = df["id"].apply(normalise_id)
    return df


def load_watersheds(gpkg_path: str, layer: str = None, id_col: str = None) -> gpd.GeoDataFrame:
    layers = fiona.listlayers(gpkg_path)
    layer = layer or layers[0]
    gdf = gpd.read_file(gpkg_path, layer=layer).to_crs(epsg=4326)

    print(f"Columns: {gdf.columns.tolist()}")
    print(f"Total features: {len(gdf)}")

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
            print(f"[INFO] No 'id' column found — using '{matched}'.")
            print(f"Pass --watershed_id_col <name> to specify explicitly.")

    print(f"Using column  : '{matched}'")
    raw_samples = gdf[matched].head(5).tolist()
    print(f"Raw id samples: {raw_samples}")

    gdf = gdf.rename(columns={matched: "id"})
    gdf["id"] = gdf["id"].apply(normalise_id)

    print(f"Normalised ids: {gdf['id'].head(5).tolist()}")
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

    output_dir  = os.path.dirname(os.path.abspath(args.output))
    geojson_abs = os.path.join(output_dir, args.geojson_dir)
    os.makedirs(geojson_abs, exist_ok=True)
    print(f"GeoJSON output dir : {geojson_abs}")

    # 1. Reservoirs
    print("\nLoading reservoirs CSV ...")
    reservoirs_df = load_reservoirs(args.csv)
    print(f"{len(reservoirs_df)} reservoirs found.")
    print(f"Sample ids: {reservoirs_df['id'].head(5).tolist()}")

    # 2. Watersheds
    print("\nLoading watershed polygons ...")
    watershed_gdf = load_watersheds(args.watersheds, args.watershed_layer, args.watershed_id_col)

    bar_coords_path = os.path.join("geojson", "reservoirs.json")
    with open(bar_coords_path, "w", encoding="utf-8") as f:
        json.dump(reservoirs_df[["id","lat","lng"]].to_dict(orient="records"), f, separators=(",",":"))

    # Overlap report — crucial for diagnosing mismatches
    res_ids = set(reservoirs_df["id"])
    ws_ids  = set(watershed_gdf["id"])
    overlap = res_ids & ws_ids
    print(f"\n === ID OVERLAP REPORT ===")
    print(f"Reservoir ids in CSV   : {len(res_ids)}")
    print(f"Watershed feature ids  : {len(ws_ids)}")
    print(f"Matched (overlap)      : {len(overlap)}")
    if overlap:
        print(f"Sample matches: {sorted(overlap)[:5]}")
    else:
        print(f"[WARNING] No matching ids! Check id column and format.")
        print(f"CSV sample       : {sorted(res_ids)[:5]}")
        print(f"Watershed sample : {sorted(ws_ids)[:5]}")

    watershed_index = {rid: grp for rid, grp in watershed_gdf.groupby("id")}

    # 3. Write per-reservoir GeoJSON
    print("\nWriting per-reservoir GeoJSON files ...")
    ws_written, ds_written, ds_missing = 0, 0, 0
    first_ws_path = None

    for rid in reservoirs_df["id"]:

        # Watershed
        if rid in watershed_index:
            ws_path = os.path.join(geojson_abs, f"watershed_{rid}.geojson")
            watershed_index[rid].to_file(ws_path, driver="GeoJSON")
            ws_written += 1
            if first_ws_path is None:
                first_ws_path = ws_path

        # Downstream
        ds_gdf = load_downstream_gdf(args.downstream_dir, rid)
        if ds_gdf is not None:
            if args.simplify > 0:
                ds_gdf = ds_gdf.copy()
                ds_gdf["geometry"] = ds_gdf["geometry"].simplify(
                    args.simplify, preserve_topology=True
                )
            ds_path = os.path.join(geojson_abs, f"downstream_{rid}.geojson")
            ds_gdf.to_file(ds_path, driver="GeoJSON")
            ds_written += 1
        else:
            ds_missing += 1

    print(f"{ws_written} watershed files written.")
    print(f"{ds_written} downstream files written, {ds_missing} missing.")

    if first_ws_path:
        print(f"Sample: {os.path.basename(first_ws_path)} ({os.path.getsize(first_ws_path)} bytes)")
    else:
        print("[WARNING] No watershed GeoJSON files written — check id overlap above.")


if __name__ == "__main__":
    main()