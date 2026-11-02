"""Part labels and effect summaries: real game names first, token inference as the fallback.

Field names (WeaponNamePartDefinition.PartName, AttributeEffectData.AttributeToModify / ModifierType /
BaseModifierValue, AttributeInitializationData.BaseValueConstant / BaseValueScaleConstant) are verified
against the class data only. The runtime values these tests feed in are fake: nothing here is verified in game.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtse import gear_parts

META = Path(__file__).resolve().parents[2] / "rtse" / "web" / "meta.js"


# --- _humanize (token inference, the fallback) ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Title_Legendary_LogansGun", "Logans Gun"),  # the documented example: keep this behaviour
        ("Pistol_Barrel_Hyperion_4", "Pistol Barrel Hyperion"),  # numbers dropped
        ("Body_Torgue", "Body Torgue"),
        ("Unknown", "Unknown"),
    ],
)
def test_part_labels_humanize_keeps_current_behaviour(raw: str, expected: str) -> None:
    assert gear_parts._humanize(raw) == expected  # noqa: SLF001


# --- _effect_view: exact fields ------------------------------------------------------------------


def _exact_summary(constant: float, scale: float) -> dict:
    return {
        "AttributeToModify": "D_Attributes.Weapon.WeaponDamage",
        "ModifierType": "MT_Scale",
        "BaseModifierValue": {
            "BaseValueConstant": constant,
            "BaseValueScaleConstant": scale,
            "BaseValueAttribute": None,
            "InitializationDefinition": None,
        },
    }


def test_effect_view_reads_exact_fields() -> None:
    summary = _exact_summary(1.5, 2.0)
    view = gear_parts._effect_view(summary)  # noqa: SLF001
    assert view["fields_found"] is True
    assert view["verified"] is False  # the value is constant x scale, not confirmed in game
    assert view["constant"] == 1.5 and view["scale"] == 2.0
    assert view["attribute"] == "D_Attributes.Weapon.WeaponDamage"
    assert view["attribute_label"] == "Weapon damage"  # from the hand-written table
    assert view["type"] == "MT_Scale"
    assert view["value"] == pytest.approx(3.0)  # BaseValueConstant * BaseValueScaleConstant
    assert view["raw"] is summary  # the raw struct is kept for display


def test_effect_view_exact_value_none_when_scale_missing() -> None:
    summary = {
        "AttributeToModify": "D_Attributes.Weapon.FireRate",
        "ModifierType": "MT_PreAdd",
        "BaseModifierValue": {"BaseValueConstant": 4.0},
    }
    view = gear_parts._effect_view(summary)  # noqa: SLF001
    assert view["fields_found"] is True and view["verified"] is False
    assert view["constant"] == 4.0 and view["scale"] is None
    assert view["value"] is None  # not both plain numbers, so no value is guessed
    assert view["attribute_label"] == "Fire rate"


def test_effect_view_attribute_label_humanized_when_not_in_table() -> None:
    summary = _exact_summary(1.0, 1.0)
    summary["AttributeToModify"] = "D_Attributes.Weapon.ReloadSpeedBonus"
    view = gear_parts._effect_view(summary)  # noqa: SLF001
    assert view["fields_found"] is True
    assert view["attribute_label"] == "Reload Speed Bonus"


# --- _effect_view: fallback (exact fields missing) -----------------------------------------------


def test_effect_view_falls_back_to_key_scan() -> None:
    summary = {"SomeAttributeRef": "D_Attributes.Weapon.FireRate", "EffectType": "Scale", "ValueX": 1.25}
    view = gear_parts._effect_view(summary)  # noqa: SLF001
    assert view["fields_found"] is False and view["verified"] is False
    assert view["constant"] is None and view["scale"] is None
    assert view["attribute"] == "D_Attributes.Weapon.FireRate"
    assert view["attribute_label"] == "Fire rate"
    assert view["type"] == "Scale"
    assert view["value"] == 1.25


def test_effect_view_unknown_keys_give_nothing_but_still_fields_found_false() -> None:
    view = gear_parts._effect_view({"Mystery": 1})  # noqa: SLF001
    assert view["fields_found"] is False
    assert view["attribute"] is None
    assert view["attribute_label"] is None
    assert view["value"] is None
    assert view["raw"] == {"Mystery": 1}


def test_effect_view_not_exact_when_only_struct_is_partial() -> None:
    # ModifierType and AttributeToModify present but no value fields: the exact path must not claim success
    summary = {"AttributeToModify": "D_Attributes.Weapon.Spread", "ModifierType": "MT_PostAdd", "BaseModifierValue": {}}
    assert gear_parts._effect_view(summary)["fields_found"] is False  # noqa: SLF001


# --- _ref_dict: label present / absent ------------------------------------------------------------


def test_ref_dict_label_from_name_part(monkeypatch: pytest.MonkeyPatch) -> None:
    part = SimpleNamespace(Class=SimpleNamespace(Name="WeaponNamePartDefinition"), PartName="Logan's Gun")
    monkeypatch.setattr(gear_parts, "find_object", lambda cls, path: part)
    ref = gear_parts.PartRef("Pkg.Names.Logan", "WeaponNamePartDefinition")
    assert gear_parts._ref_dict(ref)["label"] == "Logan's Gun"  # noqa: SLF001


def test_ref_dict_label_none_when_name_part_text_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    part = SimpleNamespace(Class=SimpleNamespace(Name="ItemNamePartDefinition"), PartName="  ")
    monkeypatch.setattr(gear_parts, "find_object", lambda cls, path: part)
    ref = gear_parts.PartRef("Pkg.Names.Blank", "ItemNamePartDefinition")
    assert gear_parts._ref_dict(ref)["label"] is None  # noqa: SLF001


def test_ref_dict_label_none_for_gun_part_without_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_be_called(cls: str, path: str) -> None:
        raise AssertionError("gun parts have no display name, so the scan must not look them up")

    monkeypatch.setattr(gear_parts, "find_object", must_not_be_called)
    ref = gear_parts.PartRef("Pkg.Barrel_Hyperion", "WeaponPartDefinition", ("Barrel_Hyperion",))
    out = gear_parts._ref_dict(ref)  # noqa: SLF001
    assert out["label"] is None
    assert out["name"] == "Barrel_Hyperion"
    assert out["group"] == "Pkg"
    assert out["meshes"] == ["Barrel_Hyperion"]


def test_ref_dict_label_none_when_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(cls: str, path: str) -> None:
        raise RuntimeError("object is gone")

    monkeypatch.setattr(gear_parts, "find_object", broken)
    ref = gear_parts.PartRef("Pkg.Names.Gone", "WeaponNamePartDefinition")
    assert gear_parts._ref_dict(ref)["label"] is None  # noqa: SLF001


# --- meta.js: server label first, token fallback (only when node is installed) -------------------


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed; meta.js check skipped")
def test_meta_js_prefers_server_label_and_falls_back_to_tokens() -> None:
    script = f"""
    const m = await import({json.dumps(META.as_uri())});
    const out = {{
      fromGame: m.parsePart("Pistol_Barrel_Hyperion_LogansGun", "", "Logan's Gun").title,
      fromTokens: m.parsePart("Pistol_Barrel_Hyperion_LogansGun", "").title,
      partLabelGame: m.partLabel({{ name: "Pistol_Barrel_Hyperion_4", group: "Pkg", label: "Hyperion Gun" }}),
      partLabelTokens: m.partLabel({{ name: "Pistol_Barrel_Hyperion_4", group: "Pkg", label: null }}),
      objectArg: m.parsePart({{ name: "Pistol_Barrel_Hyperion_4", group: "", label: "Named" }}).title,
      effectExact: m.describeEffect({{ attribute: "D_Attributes.Weapon.WeaponDamage", attribute_label: "Weapon damage", type: "MT_Scale", value: 3, constant: 1.5, scale: 2, verified: false, fields_found: true }}, {{}}),
      effectFallback: m.describeEffect({{ attribute: "WeaponDamage", attribute_label: null, type: "MT_Scale", value: 1.25, constant: null, scale: null, verified: false, fields_found: false }}, {{}}),
      effectVerified: m.describeEffect({{ attribute: "WeaponDamage", attribute_label: null, type: "MT_Scale", value: 1.25, verified: true, fields_found: true }}, {{}}).reason,
    }};
    console.log(JSON.stringify(out));
    """
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["fromGame"] == "Logan's Gun"
    assert out["fromTokens"] == "Logans Gun"  # token fallback unchanged
    assert out["partLabelGame"] == "Hyperion Gun"
    assert out["partLabelTokens"] == "Hyperion Barrel 4"  # token title, as parsePart already produced
    assert out["objectArg"] == "Named"
    assert out["effectExact"]["text"] == "Weapon damage"
    assert out["effectExact"]["reason"] == "computed as constant x scale (1.5 x 2) from the part's data; not confirmed in game"
    assert out["effectFallback"]["text"] == "Weapon Damage"  # prettyAttribute fallback unchanged
    assert "guessing" in out["effectFallback"]["reason"]
    assert out["effectVerified"] == ""  # no badge once an effect is verified
