from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


api = Path("rtse/api.py")
a = api.read_text(encoding="utf-8")

a = replace_once(
    a,
    '''class PartRef(NamedTuple):
    """A part identified by class and path. Never a live object: those can be freed on map change."""

    id: str
    class_name: str
''',
    '''class PartRef(NamedTuple):
    """A part identified by class and path. Never a live object: those can be freed on map change."""

    id: str
    class_name: str
    meshes: tuple[str, ...] = ()  # names of the gestalt mesh pieces this part draws
''',
)

a = replace_once(
    a,
    '''def _ref(part: unreal.UObject) -> PartRef:
    return PartRef(part._path_name(), part.Class.Name)  # noqa: SLF001
''',
    '''def _mesh_names(part: unreal.UObject) -> tuple[str, ...]:
    """The gestalt mesh pieces a part draws, e.g. ("Pistol_Barrel_Dahl",). Empty for parts without one."""
    names: list[str] = []
    try:
        main = part.GestaltModeSkeletalMeshName
        if main is not None:
            names.append(str(main))
    except AttributeError:
        pass
    try:
        names.extend(str(n) for n in part.AdditionalGestaltModeSkeletalMeshNames)
    except (AttributeError, TypeError):
        pass
    return tuple(n for n in names if n and n != "None")


def _ref(part: unreal.UObject) -> PartRef:
    return PartRef(part._path_name(), part.Class.Name, _mesh_names(part))  # noqa: SLF001
''',
)

a = replace_once(
    a,
    '''    return {"id": ref.id, "name": name or ref.id, "group": package, "class": ref.class_name}''',
    '''    return {"id": ref.id, "name": name or ref.id, "group": package, "class": ref.class_name, "meshes": list(ref.meshes)}''',
)

# bones the weapon type hides on its mesh (e.g. a moon clip)
a = replace_once(
    a,
    "def _detail(item: unreal.UObject, location: str, mode: str) -> dict[str, Any]:\n    data = item.DefinitionData\n    return {\n",
    '''def _hidden_bones(item: unreal.UObject) -> list[str]:
    """Bones the game hides on the built weapon mesh, e.g. a moon clip. Empty for non-weapons."""
    if not _is_weapon(item):
        return []
    weapon_type = item.DefinitionData.WeaponTypeDefinition
    names: list[str] = []
    for field in ("BoneToHideOnMesh", "AdditionalBoneToHideOnMesh"):
        try:
            value = str(getattr(weapon_type, field))
        except AttributeError:
            continue
        if value and value != "None":
            names.append(value)
    return names


def _detail(item: unreal.UObject, location: str, mode: str) -> dict[str, Any]:
    data = item.DefinitionData
    return {
        "hide_bones": _hidden_bones(item),
''',
)
api.write_text(a, encoding="utf-8")
print("patched")
