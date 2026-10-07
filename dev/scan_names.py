"""Lists distinct GD_Weap* package names mentioned in the game's GUID cache (read-only)."""

import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
names: dict[str, int] = {}
for cache in ("GuidCache_maingamerefs.upk", "GuidCache.upk"):
    data = (root / cache).read_bytes()
    for match in re.finditer(rb"(GD_Weap[A-Za-z0-9_]*|GD_[A-Za-z0-9_]*Weap[A-Za-z0-9_]*)", data):
        name = match.group(1).decode()
        names[name] = names.get(name, 0) + 1

print(f"{len(names)} distinct names")
for name in sorted(names):
    print(name)
