from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


api = Path("rtse/api.py")
a = api.read_text(encoding="utf-8")
a = replace_once(
    a,
    "        out[definition.Name] = entry\n    GESTALTS_FILE.write_text(",
    "        # Keyed by full path: the game holds more than one version (e.g. Weap_Pistol and\n        # Remaster_Weap_Pistol) with the same short name and different geometry.\n        out[definition._path_name()] = entry  # noqa: SLF001\n    GESTALTS_FILE.write_text(",
)
api.write_text(a, encoding="utf-8")
print("patched")
