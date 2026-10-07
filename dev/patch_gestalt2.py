from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


api = Path("rtse/api.py")
a = api.read_text(encoding="utf-8")

start = a.index("def _value_summary(")
end = a.index("def debug_gestalt(")
a = a[:start] + '''def _value_summary(value: Any, depth: int = 0) -> Any:
    """Any property value as JSON: scalars, object paths, structs and (bounded) arrays."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, unreal.UObject):
        return value._path_name()  # noqa: SLF001
    if isinstance(value, Enum):
        return value.name
    kind = type(value).__name__
    if kind == "WrappedStruct":  # check before arrays: arrays also expose a `_type`
        return _struct_summary(value) if depth < 4 else "<struct>"
    if kind == "WrappedArray":
        items = list(value)
        return {
            "length": len(items),
            "items": [_value_summary(v, depth + 1) for v in items[:MAX_ARRAY_ITEMS]],
        }
    return f"<{kind}>"


def _deep_summary(obj: Any) -> dict[str, Any]:
    """Every data property of an object, arrays included, without the built-in functions."""
    out: dict[str, Any] = {}
    for prop in obj.Class._fields():  # noqa: SLF001
        try:
            value = _value_summary(getattr(obj, prop.Name))
        except Exception as ex:  # noqa: BLE001
            value = f"<error {ex!r}>"
        if isinstance(value, str) and value.startswith(("<BoundFunction", "<UnrealEnum", "<WrappedMultiMap")):
            continue
        out[prop.Name] = value
    return out


''' + a[end:]

# struct summaries should also recurse into nested arrays/structs, not just scalars
a = replace_once(
    a,
    '''        if isinstance(value, unreal.UObject):
            out[prop.Name] = value.Name
        elif isinstance(value, Enum):
            out[prop.Name] = value.name
        elif value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
    return out


def _effect_view(''',
    '''        if isinstance(value, unreal.UObject):
            out[prop.Name] = value.Name
        elif isinstance(value, Enum):
            out[prop.Name] = value.name
        elif value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
        elif type(value).__name__ in ("WrappedStruct", "WrappedArray"):
            out[prop.Name] = _value_summary(value, depth=2)
    return out


def _effect_view(''',
)
api.write_text(a, encoding="utf-8")
print("patched")
