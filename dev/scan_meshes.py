"""Lists the meshes in every game package with UModel (read-only), saving one file per package.

    python -I dev/scan_meshes.py
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UMODEL = ROOT / "tools" / "umodel" / "umodel_64.exe"
GAME = Path("C:/Program Files (x86)/Steam/steamapps/common/Borderlands 2/WillowGame/CookedPCConsole")
OUT = ROOT / "work" / "lists"
OUT.mkdir(parents=True, exist_ok=True)


def scan(package: Path) -> tuple[str, int, str]:
    try:
        result = subprocess.run(
            [str(UMODEL), f"-path={GAME}", "-list", package.stem],
            capture_output=True, text=True, timeout=180, cwd=ROOT / "work", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return package.stem, -1, "timeout"
    lines = [ln for ln in result.stdout.splitlines() if any(k in ln for k in ("SkeletalMesh", "StaticMesh", "AnimSet"))]
    (OUT / f"{package.stem}.txt").write_text("\n".join(lines), encoding="utf-8")
    return package.stem, len(lines), ""


packages = sorted(GAME.glob("*.upk"))
print(f"scanning {len(packages)} packages", flush=True)
done = 0
with ThreadPoolExecutor(max_workers=4) as pool:
    for name, count, note in pool.map(scan, packages):
        done += 1
        if done % 100 == 0:
            print(f"  {done}/{len(packages)}", flush=True)
        if note:
            print(f"  {name}: {note}", flush=True)
print("done", flush=True)
