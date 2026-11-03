"""rtse/item_serial.py against real published Gibbed BL2(...) codes: checksum, is_weapon, header fields,
encrypt/decrypt and code round trips, re-signing with a new UniqueId, and pulling codes out of messy text.

    python -I -m pytest dev/tests -k item_serial
"""

from __future__ import annotations

import pytest

from rtse import item_serial as s

# Real codes copied from public web pages (fetched 2026-11-02). Fields: name, code, is_weapon, set id.
# GT = https://gamertweak.com/borderlands-2-gibbed-codes/
# GP = https://gameplay.tips/guides/4378-borderlands-2.html ("Effervescent Rarity Gear Gibbed Codes")
REAL = [
    # Encrypted (non-zero UniqueId, so seed != 0): these exercise BogoDecrypt.
    ("Fabled Tortoise (shield, GT)", "BL2(B5RV2vkR7ysJhQXfl8v1nRLlvtqvzJp2ZBtfyYW/kw/XD2axxG8=)", False, 0),
    ("Best Sheriff's Badge (relic, GT)", "BL2(Bwn78CLBShj5njbmNSuvxZQD372ygtAXCCxlJVmUzLo=)", False, 0),
    ("Infinity (pistol, GT)", "BL2(h2/V3n+mGlXGeO8zxX8PVUyUhQuLayUPJYlikvHB65/Ell/prsQL)", True, 0),
    ("Corrosive Infinity (pistol, GT)", "BL2(h5QiV53rMvaq4vd55NURZZORv969Seh9yk4qeEjD2/wEtv4tVBmE)", True, 0),
    ("Fire Infinity (pistol, GT)", "BL2(h/fUO8e0BnEw3Ax/hxDuBhZkmxxN9Nf+E0EybfchIKw3uJS94WW1)", True, 0),
    # UniqueId 0 (seed 0, plaintext), DLC asset library sets.
    ("Bekah Rifle (GT)", "BL2(hwAAAAABKwqCRoAAB4FEJgAcxHAAkAEDIob//xUY/v8jACzDYADw)", True, 10),
    ("Toothpick (weapon, GP)", "BL2(hwAAAACGoxaBhgQOBaHVEgEsxDAEUAHDIQYHDBUY/v8LAbwAGATw)", True, 22),
    ("The Retainer (item, GP)", "BL2(BwAAAADSBhYoAgPQ0BBabQAiBeIGom3i/////////z8AInCibuI=)", False, 22),
    ("Antifection (item, GP)", "BL2(BwAAAAB6KRYFAVM4sBBabRxhAKEAYQIhDmEG4f8/CKEZ4f9/JeE=)", False, 22),
    ("Hard Carry (item, GP)", "BL2(BwAAAACEeRYdE2nwMBFaLQDj////////////vwxjBOM=)", False, 22),
]
IDS = [r[0] for r in REAL]
FABLED = REAL[0][1]


@pytest.mark.parametrize("name,code,is_weapon,set_id", REAL, ids=IDS)
def test_real_codes_verify(name, code, is_weapon, set_id):
    data = s.code_to_bytes(code)
    h = s.read_header(data)
    assert h["checksum_ok"] is True
    assert h["checksum"] == h["checksum_computed"]
    assert h["version"] == 7
    assert h["is_weapon"] is is_weapon
    assert h["asset_library_set_id"] == set_id
    assert h["length"] == len(data)
    assert h["seed"] == int.from_bytes(data[1:5], "big")
    assert bytes_roundtrip(code) == code


def bytes_roundtrip(code: str) -> str:
    return s.bytes_to_code(s.code_to_bytes(code))


def test_known_header_values():
    h = s.read_header(s.code_to_bytes(FABLED))
    assert h["unique_id"] == -103131756
    assert h["seed"] == 0x9455DAF9
    assert h["checksum"] == 42863
    assert s.read_header(s.code_to_bytes(REAL[2][1]))["unique_id"] == 2145310063


@pytest.mark.parametrize("code", [r[1] for r in REAL], ids=IDS)
def test_encrypt_decrypt_roundtrip(code):
    data = s.code_to_bytes(code)
    plain = s.decrypt(data)
    assert s.encrypt(plain) == data
    assert s.decrypt(s.encrypt(plain)) == plain
    assert int.from_bytes(plain[5:7], "big") == s.checksum(plain)


def test_seed_zero_is_plaintext():
    data = s.code_to_bytes(REAL[5][1])
    assert s.seed_of(data) == 0
    assert s.decrypt(data) == data == s.encrypt(data)


@pytest.mark.parametrize("new_id", [0, 1, -1, 123456789, -123456789, 2**31 - 1, -(2**31)])
@pytest.mark.parametrize("code", [REAL[0][1], REAL[2][1], REAL[5][1], REAL[7][1]], ids=IDS[:1] + [IDS[2], IDS[5], IDS[7]])
def test_with_unique_id_roundtrip(code, new_id):
    data = s.code_to_bytes(code)
    before = s.read_header(data)
    out = s.with_unique_id(data, new_id)
    after = s.read_header(out)
    assert after["unique_id"] == new_id
    assert after["checksum_ok"] is True
    assert len(out) == len(data)
    for key in ("version", "is_weapon", "asset_library_set_id", "first_byte", "length"):
        assert after[key] == before[key]
    # everything past the unique id and checksum is unchanged once decrypted
    assert s.decrypt(out)[7:] == s.decrypt(data)[7:]
    # and setting the original id back reproduces the original serial exactly
    assert s.with_unique_id(out, before["unique_id"]) == data


def test_with_unique_id_rejects_bad_input():
    data = s.code_to_bytes(FABLED)
    with pytest.raises(s.SerialError):
        s.with_unique_id(data, 2**31)
    with pytest.raises(s.SerialError):
        s.with_unique_id(data, -(2**31) - 1)
    corrupt = data[:-1] + bytes([data[-1] ^ 0x01])
    with pytest.raises(s.SerialError):
        s.with_unique_id(corrupt, 5)


def test_corrupt_checksum_reported_not_raised():
    data = bytearray(s.code_to_bytes(FABLED))
    data[20] ^= 0x40
    h = s.read_header(bytes(data))
    assert h["checksum_ok"] is False
    assert h["version"] == 7


@pytest.mark.parametrize(
    "data",
    [b"", b"\x07" * 7, b"\x07" * 41, b"\x06" + bytes(10), b"\x08" + bytes(10)],
    ids=["empty", "too-short", "too-long", "version-6", "version-8"],
)
def test_read_header_structural_errors(data):
    with pytest.raises(s.SerialError):
        s.read_header(data)


def test_code_parsing():
    data = s.code_to_bytes(FABLED)
    assert s.code_to_bytes(FABLED.rstrip(")").rstrip("=") + ")") == data  # missing padding
    assert s.code_to_bytes("bl2(" + FABLED[4:]) == data  # lower-case prefix
    assert s.code_to_bytes(FABLED[:20] + "\n  " + FABLED[20:]) == data  # wrapped
    for bad in ["", "BL2()", "BL2(abc!)", "BL3(" + FABLED[4:], FABLED[4:], "BL2(A)", None]:
        with pytest.raises(s.SerialError):
            s.code_to_bytes(bad)
    with pytest.raises(s.SerialError):
        s.bytes_to_code(b"")


def test_extract_codes_messy_text():
    a, b, c = REAL[0][1], REAL[2][1], REAL[7][1]
    text = (
        "My loadout!\n\n"
        f"* **Gun 1:** `{b}`\n"
        f"- Shield: {a[:25]}\n   {a[25:40]}\n{a[40:]} (wrapped)\n"
        f"[code]{c}[/code] and again {c}, plus {a.lower()[:4]}{a[4:]}\n"
        "```\n" + b + "\n```\n"
        "Not a code: BL2(), BL2(!!!), BL3(abcd)\n"
    )
    assert s.extract_codes(text) == [b, a, c, c, a, b]


def test_extract_codes_bare_tokens():
    a = REAL[0][1][4:-1]
    garbage = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY"  # valid base64, not a serial
    text = f"Shield code:\n  {a}\n{garbage}\nsentence with {a} inside is ignored\n- `{a.rstrip('=')}`\n"
    assert s.extract_codes(text) == [REAL[0][1], REAL[0][1]]


def test_extract_codes_none():
    assert s.extract_codes("") == []
    assert s.extract_codes("no codes here, just BL2 talk (and parentheses)") == []


# --- validate_assets: the packed tail after the header, bounds-checked against item_assets ---

INFINITY = REAL[2][1]  # weapon, set 0, 39 bytes
TOOTHPICK = REAL[6][1]  # weapon, set 22, uses set-22 references
HARD_CARRY = REAL[9][1]  # item, set 22, 32 bytes (trailing None parts stripped by the encoder)


def resign(plain: bytes) -> bytes:
    """Recompute the checksum of a decrypted serial and encrypt it: a 'crafted' serial."""
    plain = bytearray(plain)
    plain[5:7] = s.checksum(bytes(plain)).to_bytes(2, "big")
    return s.encrypt(bytes(plain))


def with_field(code: str, name: str, value: int) -> bytes:
    """Overwrite one packed field (raw index bits) of a real code and re-sign it."""
    data = s.code_to_bytes(code)
    plain = s.decrypt(data)
    layout = {f[0]: f for f in s.field_layout(s.read_header(data)["is_weapon"])}
    _, _, start, width = layout[name]
    assert 0 <= value < (1 << width)
    bits = int.from_bytes(plain, "little")
    bits = (bits & ~(((1 << width) - 1) << start)) | (value << start)
    return resign(bits.to_bytes(len(plain), "little"))


def ref(group: str, sublibrary: int, asset: int, use_set_id: bool = False) -> int:
    from rtse import item_assets

    sublibrary_bits, asset_bits = item_assets.CONFIGS[group]
    return asset | (sublibrary << asset_bits) | (int(use_set_id) << (asset_bits + sublibrary_bits - 1))


@pytest.mark.parametrize("name,code,is_weapon,set_id", REAL, ids=IDS)
def test_real_codes_pass_validate_assets(name, code, is_weapon, set_id):
    data = s.code_to_bytes(code)
    info = s.validate_assets(data)
    assert info["is_weapon"] is is_weapon
    assert info["set_id"] == set_id
    assert info["length"] == len(data)
    assert info["bits_read"] == (309 if is_weapon else 302)
    assert len(info["fields"]) == 14
    assert 0 <= info["manufacturer_grade"] < 128 and 0 <= info["game_stage"] < 128
    first = "weapon_type" if is_weapon else "item_type"
    assert info["fields"][first] is not None and info["fields"]["balance"] is not None


def test_validate_assets_known_values():
    info = s.validate_assets(s.code_to_bytes(INFINITY))
    assert info["manufacturer_grade"] == 50 and info["game_stage"] == 50
    assert info["fields"]["weapon_type"] == {"set": 0, "sublibrary": 30, "asset": 4}
    assert info["fields"]["stock"] is None
    # Toothpick (set 22) mixes base-set and use-set-id references
    tp = s.validate_assets(s.code_to_bytes(TOOTHPICK))["fields"]
    assert tp["balance"] == {"set": 22, "sublibrary": 28, "asset": 36}
    assert tp["weapon_type"]["set"] == 0
    # Hard Carry is 32 bytes: the parts past the end read as 0xFF, i.e. None (as in Gibbed)
    hc = s.validate_assets(s.code_to_bytes(HARD_CARRY))["fields"]
    assert hc["title"] is None and hc["alpha"] == {"set": 22, "sublibrary": 3, "asset": 0}


def test_asset_index_bounds_with_recomputed_checksum():
    from rtse import item_assets

    count = item_assets.SETS[0]["WeaponParts"][6]  # Infinity's parts live in base sublibrary 6
    ok = with_field(INFINITY, "body", ref("WeaponParts", 6, count - 1))
    assert s.read_header(ok)["checksum_ok"]
    assert s.validate_assets(ok)["fields"]["body"]["asset"] == count - 1
    bad = with_field(INFINITY, "body", ref("WeaponParts", 6, count))
    assert s.read_header(bad)["checksum_ok"]  # passes the outer checks...
    with pytest.raises(s.SerialError, match=r"body: asset \d+ out of range for WeaponParts sublibrary 6 in set 0"):
        s.validate_assets(bad)  # ...but not the tail


def test_sublibrary_index_bounds():
    n = len(s._assets.SETS[0]["BalanceDefs"])
    with pytest.raises(s.SerialError, match=r"balance: sublibrary \d+ out of range for BalanceDefs in set 0"):
        s.validate_assets(with_field(INFINITY, "balance", ref("BalanceDefs", n, 0)))
    # the use-set-id flag switches to the header's set (0 here, so same tables)
    assert s.validate_assets(with_field(INFINITY, "balance", ref("BalanceDefs", 128, 39, True)))
    # a set-22 sublibrary index that only exists in base set 0 is out of range for set 22
    with pytest.raises(s.SerialError, match=r"in set 22"):
        s.validate_assets(with_field(TOOTHPICK, "body", ref("WeaponParts", 3, 0, True)))


def test_empty_sublibrary_rejected():
    # set 0 WeaponParts sublibrary 0 has no assets in Gibbed's dump
    assert s._assets.SETS[0]["WeaponParts"][0] == 0
    with pytest.raises(s.SerialError, match=r"\(0 assets\)"):
        s.validate_assets(with_field(INFINITY, "grip", ref("WeaponParts", 0, 0)))


def test_none_sentinel_allowed():
    width = sum(s._assets.CONFIGS["WeaponParts"])
    info = s.validate_assets(with_field(INFINITY, "body", (1 << width) - 1))
    assert info["fields"]["body"] is None


@pytest.mark.parametrize("set_id", [7, 11, 23, 255])
def test_unknown_set_id_rejected(set_id):
    plain = bytearray(s.decrypt(s.code_to_bytes(INFINITY)))
    plain[7] = set_id
    data = resign(bytes(plain))
    assert s.read_header(data)["checksum_ok"]
    with pytest.raises(s.SerialError, match=rf"unknown asset library set {set_id}"):
        s.validate_assets(data)


def test_truncated_tail():
    data = s.code_to_bytes(INFINITY)
    with pytest.raises(s.SerialError, match="checksum"):
        s.validate_assets(data[:20])  # plain truncation breaks the checksum
    with pytest.raises(s.SerialError, match="length"):
        s.validate_assets(resign(s.decrypt(data)[:7]))
    # Truncated and re-signed: Gibbed reads the missing bytes as 0xFF, so the field cut in
    # half gets 1s in its high bits and falls outside the tables here.
    with pytest.raises(s.SerialError, match="out of range"):
        s.validate_assets(resign(s.decrypt(data)[:20]))
    # Header only: every field reads as all ones (None refs, grade/stage 127), as in Gibbed.
    info = s.validate_assets(resign(s.decrypt(data)[:8]))
    assert all(v is None for v in info["fields"].values()) and info["game_stage"] == 127


def test_bytes_past_last_field_and_pad_bits():
    plain = s.decrypt(s.code_to_bytes(INFINITY))
    with pytest.raises(s.SerialError, match="past the last field"):
        s.validate_assets(resign(plain + b"\x00"))
    item = s.decrypt(s.code_to_bytes(REAL[7][1]))  # 38-byte item: 302 bits fit in 38 bytes
    with pytest.raises(s.SerialError, match="past the last field"):
        s.validate_assets(resign(item + b"\xff"))
    flipped = bytearray(plain)
    flipped[-1] &= ~0x80  # bit 311 is padding (fields end at bit 309)
    with pytest.raises(s.SerialError, match="pad bits"):
        s.validate_assets(resign(bytes(flipped)))


@pytest.mark.parametrize("seed", range(40))
def test_garbage_tail_rejected(seed):
    import random

    rng = random.Random(seed)
    plain = s.decrypt(s.code_to_bytes(INFINITY))
    tail = bytes(rng.randrange(256) for _ in range(len(plain) - 8))
    data = resign(plain[:8] + tail)
    assert s.read_header(data)["checksum_ok"]
    with pytest.raises(s.SerialError, match="out of range|pad bits"):
        s.validate_assets(data)
