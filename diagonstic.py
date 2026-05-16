import os
import sys
import argparse
import pandas as pd


def normalise_id(val) -> str:
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def load_csv_ids(csv_path):
    if not os.path.exists(csv_path):
        return None
    df = pd.read_csv(csv_path)
    return set(df["id"].apply(normalise_id))


def list_runoff_ids(runoff_dir):
    # Return the set of IDs for which a GDWID_{id}.txt file exists
    if not os.path.isdir(runoff_dir):
        return None
    ids = set()
    for name in os.listdir(runoff_dir):
        if name.startswith("GDWID_") and name.endswith(".txt"):
            ids.add(name[len("GDWID_"):-len(".txt")])
    return ids


def list_timeseries_ids(out_dir):
    # Return the set of IDs for which a timeseries_{id}.json file exists
    if not os.path.isdir(out_dir):
        return None
    ids = set()
    for name in os.listdir(out_dir):
        if name.startswith("timeseries_") and name.endswith(".json"):
            ids.add(name[len("timeseries_"):-len(".json")])
    return ids


def diagnose(kind, csv_path, runoff_dir, out_dir):
    print(f"\n=== {kind.upper()} ===")
    csv_ids = load_csv_ids(csv_path)
    runoff_ids = list_runoff_ids(runoff_dir)
    ts_ids = list_timeseries_ids(out_dir)

    if csv_ids is None:
        print(f"  [ERROR] CSV not found: {csv_path}")
        return
    if runoff_ids is None:
        print(f"  [ERROR] Runoff dir not found: {runoff_dir}")
        return
    if ts_ids is None:
        print(f"  [WARN]  Output dir not found: {out_dir}")
        ts_ids = set()

    print(f"  CSV ids        : {len(csv_ids)}")
    print(f"  Runoff files   : {len(runoff_ids)}  ({runoff_dir})")
    print(f"  Timeseries JSON: {len(ts_ids)}  ({out_dir})")

    # IDs in CSV but with no source runoff file
    missing_source = sorted(csv_ids - runoff_ids, key=lambda x: int(x) if x.isdigit() else 1e18)
    # IDs in CSV but with no output JSON (should equal missing_source if the script worked)
    missing_output = sorted(csv_ids - ts_ids, key=lambda x: int(x) if x.isdigit() else 1e18)
    # IDs in runoff dir that aren't in the CSV (orphan source files)
    orphan_source = sorted(runoff_ids - csv_ids, key=lambda x: int(x) if x.isdigit() else 1e18)

    print(f"\n  CSV ids with NO runoff source file : {len(missing_source)}")
    if missing_source:
        for i in missing_source:
            print(f"     - {i}")

    print(f"\n  CSV ids with NO output JSON        : {len(missing_output)}")
    if set(missing_output) != set(missing_source):
        diff_only_output = sorted(set(missing_output) - set(missing_source), key=lambda x: int(x) if x.isdigit() else 1e18)
        if diff_only_output:
            print(f"     (these have a source file but no output - script failed to write them)")
            for i in diff_only_output:
                print(f"     - {i}")

    print(f"\n  Orphan runoff files (not in CSV)   : {len(orphan_source)}")
    if orphan_source and len(orphan_source) <= 50:
        for i in orphan_source:
            print(f"     - {i}")
    elif orphan_source:
        print(f"     (first 20): {orphan_source[:20]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reservoirs_csv",      default="Dataset/GBMIM_Reservoirs.csv")
    ap.add_argument("--barriers_csv",        default="Dataset/GBMIM_Barriers.csv")
    ap.add_argument("--runoff_reservoir_dir", default="Dataset/data/runoff_reservoir")
    ap.add_argument("--runoff_barrier_dir",   default="Dataset/data/runoff_barrier")
    ap.add_argument("--geojson_dir",          default="geojson")
    args = ap.parse_args()

    diagnose(
        "reservoir",
        args.reservoirs_csv,
        args.runoff_reservoir_dir,
        os.path.join(args.geojson_dir, "reservoir"),
    )
    diagnose(
        "barrier",
        args.barriers_csv,
        args.runoff_barrier_dir,
        os.path.join(args.geojson_dir, "barrier"),
    )


if __name__ == "__main__":
    main()