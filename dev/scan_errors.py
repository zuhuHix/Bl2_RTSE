"""Re-lists every package with UModel and reports ones that failed to load or report problems."""

from __future__ import annotations

import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UMODEL = ROOT / "tools" / "umodel" / "umodel_64.exe"
GAME = Path("C:/Program Files (x86)/Steam/steamapps/common/Borderlands 2/WillowGame/CookedPCConsole")


def check(package: Path) -> tuple[str, list[str], int]:
    result = subprocess.run(
        [str(UMODEL), f"-path={GAME}", "-list", package.stem],
        capture_output=True, text=True, timeout=180, cwd=ROOT / "work", errors="replace",
    )
    text = result.stdout + result.stderr
    problems = [ln.strip() for ln in text.splitlines() if any(k in ln for k in ("ERROR", "unable", "Unsupported", "unsupported", "failed"))]
    objects = sum(1 for ln in text.splitlines() if ln[:6].strip().isdigit())
    return package.stem, problems, objects


packages = sorted(GAME.glob("*.upk"))
failed = []
empty = []
kinds: Counter[str] = Counter()
with ThreadPoolExecutor(max_workers=4) as pool:
    for name, problems, objects in pool.map(check, packages):
        if problems:
            failed.append((name, problems[0]))
            kinds[problems[0][:70]] += 1
        if objects == 0:
            empty.append(name)

print(f"{len(packages)} packages, {len(failed)} reported problems, {len(empty)} listed zero objects")
for text, n in kinds.most_common(8):
    print(f"  {n:4}x {text}")
print("examples with zero objects:", empty[:10])
print("examples with problems:", failed[:6])
