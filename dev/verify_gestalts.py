"""Checks every gestalt table against its exported glTF: ranges must tile the index buffer exactly."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tables = json.loads((ROOT / "rtse" / "gestalts.json").read_text(encoding="utf-8"))
mesh_dir = ROOT / "work" / "gestalt" / "Startup" / "SkeletalMesh3"

ok = True
for path, entry in sorted(tables.items()):
    if path.startswith("Remaster_"):
        continue  # the exported meshes are the original versions
    name = path.split(".")[-1]
    parts = entry["GestaltInfos"]["items"][0]["Parts"]["items"]
    ranges = sorted((p["FirstIndex"], p["NumPrimitives"] * 3, p["SkeletalMeshFragmentName"], p["MaterialIndex"]) for p in parts)
    gaps = [(a[2], b[2]) for a, b in zip(ranges, ranges[1:]) if a[0] + a[1] != b[0]]
    table_total = ranges[-1][0] + ranges[-1][1]

    gltf_path = mesh_dir / f"{name}_GestaltSkeletalMesh.gltf"
    gltf = json.loads(gltf_path.read_text(encoding="utf-8"))
    prims = gltf["meshes"][0]["primitives"]
    prim_counts = [gltf["accessors"][p["indices"]]["count"] for p in prims]
    gltf_total = sum(prim_counts)
    # the table's material index must pick the same primitive the slice lands in
    boundary = prim_counts[0]
    mismatches = [
        r[2] for r in ranges
        if (r[3] == 0) != (r[0] + r[1] <= boundary) and len(prims) == 2
    ]
    status = "OK " if (not gaps and table_total == gltf_total and not mismatches) else "BAD"
    ok &= status == "OK "
    print(f"{status} {name:24} parts={len(parts):3} table_total={table_total:7} gltf_total={gltf_total:7} prims={prim_counts} gaps={len(gaps)} material_mismatch={len(mismatches)}")

print("\nall verified" if ok else "\nPROBLEMS FOUND")
