"""Scans the installed game's packages for skill-related objects (read-only). Writes dev/out/skill_scan.json."""
import json, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import upk

GAME = Path("C:/Program Files (x86)/Steam/steamapps/common/Borderlands 2")
WANT = ("SkillDefinition", "SkillTree", "Branch", "SkillTreeBranch", "ClassModDefinition")
found = {}
errors = {}
files = sorted(list((GAME / "WillowGame/CookedPCConsole").glob("*.upk")) + list((GAME / "DLC").rglob("*.upk")))
t = time.time()
for f in files:
    try:
        head = f.read_bytes()[:8]
        raw = upk.decompress_file(f)
        pkg = upk.Package(raw)
        counts = {}
        for e in pkg.exports:
            c = pkg.class_name(e)
            if any(w in c for w in WANT):
                counts[c] = counts.get(c, 0) + 1
        if counts:
            found[str(f.relative_to(GAME))] = counts
    except Exception as ex:  # noqa: BLE001
        errors[str(f.name)] = repr(ex)[:120]
print(len(files), "files", round(time.time() - t), "s", len(errors), "errors")
Path("dev/out/skill_scan.json").write_text(json.dumps({"found": found, "errors": errors}, indent=1))
for k, v in found.items():
    print(k, v)
