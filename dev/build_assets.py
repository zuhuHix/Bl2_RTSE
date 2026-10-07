"""Packs the exported gestalt meshes into single .glb files and writes the part-slice table.

    python -I dev/build_assets.py

Reads  work/gestalt/Startup/SkeletalMesh3/*.gltf/.bin  (UModel export)  and  rtse/gestalts.json
(dumped from the running game). Writes rtse/web/models/*.glb and rtse/web/models/parts.json.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESHES = ROOT / "work" / "gestalt" / "Startup" / "SkeletalMesh3"
OUT = ROOT / "rtse" / "web" / "models"

# key used by the front end -> (gestalt definition short name, game path of the ORIGINAL version)
TYPES = {
    "Pistol": "GestaltDef_Pistol",
    "SMG": "GestaltDef_SMG",
    "Assault Rifle": "GestaltDef_AssaultRifle",
    "Shotgun": "GestaltDef_Shotgun",
    "Sniper Rifle": "GestaltDef_SniperRifle",
    "Launcher": "GestaltDef_Launcher",
    "Shield": "GestaltDef_Shields",
    "Grenade": "GestaltDef_Grenades",
    "Relic": "GestaltDef_Artifact",
}
FILE_NAMES = {
    "Pistol": "pistol", "SMG": "smg", "Assault Rifle": "assault_rifle", "Shotgun": "shotgun",
    "Sniper Rifle": "sniper_rifle", "Launcher": "launcher", "Shield": "shield", "Grenade": "grenade", "Relic": "relic",
}


def pack_glb(gltf_path: Path) -> bytes:
    gltf = json.loads(gltf_path.read_text(encoding="utf-8"))
    bin_path = gltf_path.with_suffix(".bin")
    blob = bin_path.read_bytes()
    assert len(gltf["buffers"]) == 1, "expected a single buffer"
    gltf["buffers"][0] = {"byteLength": len(blob)}  # drop the external uri: the data is embedded

    json_bytes = json.dumps(gltf, separators=(",", ":")).encode()
    json_bytes += b" " * (-len(json_bytes) % 4)
    blob += b"\0" * (-len(blob) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(blob)
    return b"".join([
        struct.pack("<4sII", b"glTF", 2, total),
        struct.pack("<I4s", len(json_bytes), b"JSON"), json_bytes,
        struct.pack("<I4s", len(blob), b"BIN\0"), blob,
    ])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tables = json.loads((ROOT / "rtse" / "gestalts.json").read_text(encoding="utf-8"))
    originals = {p.split(".")[-1]: e for p, e in tables.items() if not p.startswith("Remaster_")}

    manifest: dict[str, dict] = {}
    for key, definition in TYPES.items():
        entry = originals[definition]
        glb = pack_glb(MESHES / f"{definition}_GestaltSkeletalMesh.gltf")
        file = f"{FILE_NAMES[key]}.glb"
        (OUT / file).write_bytes(glb)

        parts = entry["GestaltInfos"]["items"][0]["Parts"]["items"]
        bounds = {b["SkeletalMeshFragmentName"]: b["ReferencePoseBounds"] for b in entry["GestaltPartBounds"]["items"]}
        grouped: dict[str, dict] = {}
        for p in parts:
            name = p["SkeletalMeshFragmentName"]
            origin = bounds.get(name, {}).get("Origin")
            part = grouped.setdefault(name, {"slices": [], "origin": [round(origin[a], 4) for a in "XYZ"] if origin else None})
            part["slices"].append([p["FirstIndex"], p["NumPrimitives"], p["MaterialIndex"]])  # first index, triangles, material
        manifest[key] = {
            "file": file,
            "parts": grouped,
            "sockets": [
                {k: s[k] for k in ("SocketName", "BoneName", "RelativeLocation", "RelativeRotation", "RelativeScale") if k in s}
                for s in entry.get("mesh_sockets", []) if isinstance(s, dict)
            ],
        }
        print(f"{key:14} {file:18} {len(glb) / 1e6:5.2f} MB  {len(grouped):3} parts ({len(parts)} slices)")

    (OUT / "parts.json").write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    total = sum(p.stat().st_size for p in OUT.iterdir())
    print(f"\nwrote {len(manifest)} models, {total / 1e6:.1f} MB total in {OUT}")


main()
