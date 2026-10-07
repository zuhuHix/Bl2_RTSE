"""Summarises weapon-part meshes found by scan_meshes.py (read-only analysis of work/lists)."""

import re
from collections import defaultdict
from pathlib import Path

LISTS = Path(__file__).resolve().parent.parent / "work" / "lists"
# Lines look like: "  4738   64D3ED     6127 SkeletalMesh Skel_Hyperion_WeaponLocker"
LINE = re.compile(r"^\s*(\d+)\s+([0-9A-F]+)\s+([0-9A-F]+)\s+(SkeletalMesh|StaticMesh|AnimSet)\s+(\S+)")
PART = re.compile(r"^(Pistol|SMG|AR|SG|Shotgun|Sniper|SR|L|Launcher|Rifle)_(Body|Grip|Barrel|Sight|Scope|Stock|Accessory|Acc|Mag|Magazine|Element|Pump|Bayonet)", re.I)

by_name: dict[str, dict] = defaultdict(lambda: {"class": "", "packages": {}})
for file in LISTS.glob("*.txt"):
    for line in file.read_text(encoding="utf-8").splitlines():
        match = LINE.match(line)
        if not match:
            continue
        index, offset, size, cls, name = match.groups()
        entry = by_name[name]
        entry["class"] = cls
        entry["packages"][file.stem] = int(size, 16)

parts = {n: e for n, e in by_name.items() if PART.match(n)}
print(f"total distinct mesh names: {len(by_name)}")
print(f"weapon-part-looking meshes: {len(parts)}")

kinds = defaultdict(int)
for name in parts:
    m = PART.match(name)
    kinds[(m.group(1).upper(), m.group(2).capitalize())] += 1
print("\nby gun type / part kind:")
for (gun, kind), n in sorted(kinds.items()):
    print(f"  {gun:9} {kind:10} {n}")

print("\nexamples:")
for name in sorted(parts)[:25]:
    e = parts[name]
    print(f"  {name:45} {e['class']:13} in {len(e['packages'])} package(s), e.g. {next(iter(e['packages']))}")

# how many distinct packages would be needed to cover all part meshes (greedy)
remaining = set(parts)
needed = []
pkg_cover = defaultdict(set)
for name, e in parts.items():
    for pkg in e["packages"]:
        pkg_cover[pkg].add(name)
while remaining and pkg_cover:
    best = max(pkg_cover, key=lambda p: len(pkg_cover[p] & remaining))
    gain = pkg_cover[best] & remaining
    if not gain:
        break
    needed.append((best, len(gain)))
    remaining -= gain
print(f"\npackages needed to cover every part mesh (greedy): {len(needed)}")
for pkg, gain in needed[:12]:
    print(f"  {pkg:35} +{gain}")
