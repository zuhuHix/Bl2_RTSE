"""Outer layer of Borderlands 2 item serials and Gibbed "BL2(...)" item codes.

What this does (pure stdlib, offline):
  * finds ``BL2(<base64>)`` codes in pasted text and converts them to and from bytes;
  * decrypts and encrypts a serial ("BogoDecrypt"/"BogoEncrypt") and checks or recomputes
    its 16-bit CRC32-derived checksum;
  * reads the fixed-width header (version, is_weapon, unique id, checksum, asset library
    set id) and rewrites the unique id.

  * validate_assets: walks the packed weapon/item data after the header exactly as Gibbed's
    PackedWeapon.Read / PackedItem.Read do and bounds-checks every asset reference against
    compact copies of Gibbed's asset library tables (rtse/item_assets.py), so a crafted serial
    with a recomputed checksum cannot hand the game's UnpackSerialNumber an index that is
    outside the tables. See its docstring for what is still trusted to the game.

What it does NOT do: resolve references to names or object paths (the tables keep counts only).
RTSE still uses the game's own UnpackSerialNumber to build the item, after validate_assets.

Layout (bit reader/writer is LSB-first within each byte, values little-endian):
  bits 0-6   version (7 bits; Gibbed only accepts AssetLibraryManager.Version = 7)
  bit  7     is_weapon (1 bit)
  bits 8-39  unique_id (32 bits, signed Int32); bytes 1-4 are NOT encrypted
  bits 40-55 checksum slot (16 bits): written as 0xFFFF, then replaced by the checksum,
             stored big-endian in bytes 5-6
  bits 56-63 asset_library_set_id (8 bits)
  bits 64+   packed weapon/item data: asset references and two 7-bit ints (validate_assets)
The plaintext is padded with 0xFF to 40 bytes for the CRC32 (bytes 5-6 counted as 0xFF);
checksum = (crc >> 16) ^ (crc & 0xFFFF). Bytes 5.. are encrypted with
seed = big-endian uint32 of bytes 1-4 (so the seed IS the unique id, byte-swapped):
rotate by (seed % 32) % length, XOR with a stream x = x * 0x10A860C1 % 0xFFFFFFFB
starting at x = (int32)seed >> 5. Seed 0 means no encryption.

Sources (read for this implementation, Gibbed.Borderlands2 master, Gibbed.Gearbox cdb03b0):
  https://github.com/gibbed/Gibbed.Borderlands2/blob/master/projects/Gibbed.Borderlands2.FileFormats/Items/PackedDataHelper.cs
  https://github.com/gibbed/Gibbed.Borderlands2/blob/master/projects/Gibbed.Borderlands2.SaveEdit/Tabs/BackpackViewModel.cs
      (CopySelectedSlotCode / PasteCode / _CodeSignature regex)
  https://github.com/gibbed/Gibbed.Gearbox/blob/cdb03b048e4989c2272162ebc40f5f34f14712fd/Gibbed.Gearbox.Common/BitReader.cs
  https://github.com/gibbed/Gibbed.Gearbox/blob/cdb03b048e4989c2272162ebc40f5f34f14712fd/Gibbed.Gearbox.Common/BitWriter.cs
  https://github.com/gibbed/Gibbed.Gearbox/blob/cdb03b048e4989c2272162ebc40f5f34f14712fd/Gibbed.Gearbox.Common/CRC32.cs
      (standard reflected CRC32, identical to zlib.crc32)
  https://github.com/gibbed/Borderlands2Dumps "Asset Library Manager.json" ("version": 7)
Read for validate_assets (Gibbed.Borderlands2 master e7afdaf45265024725d55d9d19dfee5ce077ff2b):
  .../projects/Gibbed.Borderlands2.FileFormats/Items/PackedWeapon.cs, PackedItem.cs (field order)
  .../projects/Gibbed.Borderlands2.FileFormats/AssetLibraryManagerHelpers.cs (Decode, Lookup)
  .../projects/Gibbed.Borderlands2.GameInfo/AssetLibraryConfiguration.cs (NoneIndex and masks)
  .../projects/Gibbed.Borderlands2.GameInfo/AssetSublibraryDefinition.cs (GetAsset bounds)
  .../projects/Gibbed.Borderlands2.GameInfo/Resources/Platform Configurations.json (X360 only)
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib

from . import item_assets as _assets

SERIAL_VERSION = 7
MIN_LENGTH = 8  # the fixed header is 64 bits
MAX_LENGTH = 40  # Gibbed rejects longer serials and pads to 40 for the checksum

_B64 = "A-Za-z0-9+/"
_CODE_RE = re.compile(r"bl2\(([" + _B64 + r"=\s]*)\)", re.IGNORECASE)
_BARE_RE = re.compile(r"^[" + _B64 + r"]{11,}={0,2}$")


class SerialError(ValueError):
    """Raised for input that cannot be a BL2 item serial or code."""


def _keystream(seed: int, length: int) -> bytes:
    signed = seed - (1 << 32) if seed & 0x80000000 else seed
    x = (signed >> 5) & 0xFFFFFFFF  # C#: (uint)((int)seed >> 5), arithmetic shift
    out = bytearray(length)
    for i in range(length):
        x = (x * 0x10A860C1) % 0xFFFFFFFB
        out[i] = x & 0xFF
    return bytes(out)


def _check_length(data: bytes) -> None:
    if not isinstance(data, (bytes, bytearray)):
        raise SerialError("serial must be bytes")
    if not MIN_LENGTH <= len(data) <= MAX_LENGTH:
        raise SerialError(f"serial length {len(data)} not in {MIN_LENGTH}..{MAX_LENGTH}")


def seed_of(data: bytes) -> int:
    """Return the encryption seed: bytes 1-4 read as a big-endian uint32."""
    _check_length(data)
    return int.from_bytes(data[1:5], "big")


def decrypt(data: bytes) -> bytes:
    """Undo Gibbed's BogoEncrypt on bytes 5.. (bytes 0-4 are plaintext). Returns bytes."""
    _check_length(data)
    seed = seed_of(data)
    if seed == 0:
        return bytes(data)
    body = bytes(a ^ b for a, b in zip(data[5:], _keystream(seed, len(data) - 5)))
    right = (seed % 32) % len(body)
    left = len(body) - right
    return bytes(data[:5]) + body[left:] + body[:left]


def encrypt(plain: bytes) -> bytes:
    """Apply BogoEncrypt to bytes 5.. with the seed taken from bytes 1-4. Inverse of decrypt."""
    _check_length(plain)
    seed = seed_of(plain)
    if seed == 0:
        return bytes(plain)
    body = plain[5:]
    right = (seed % 32) % len(body)
    rotated = body[right:] + body[:right]
    body = bytes(a ^ b for a, b in zip(rotated, _keystream(seed, len(body))))
    return bytes(plain[:5]) + body


def checksum(plain: bytes) -> int:
    """16-bit checksum of a decrypted serial (bytes 5-6 treated as 0xFFFF, 0xFF-padded to 40)."""
    _check_length(plain)
    buf = bytes(plain[:5]) + b"\xff\xff" + bytes(plain[7:])
    buf += b"\xff" * (MAX_LENGTH - len(buf))
    crc = zlib.crc32(buf) & 0xFFFFFFFF
    return (crc >> 16) ^ (crc & 0xFFFF)


def read_header(data: bytes) -> dict:
    """Decrypt an (encrypted) serial and parse its fixed-width header.

    Returns a dict with keys: ``first_byte`` (raw byte 0), ``version`` (7 bits),
    ``is_weapon`` (1 bit), ``unique_id`` (32 bits, signed), ``seed`` (uint32 key),
    ``checksum`` (16 bits as stored), ``checksum_computed``, ``checksum_ok``,
    ``asset_library_set_id`` (8 bits), ``length`` (bytes). A bad checksum gives
    ``checksum_ok`` False; raises SerialError for a wrong length or unknown version.
    """
    _check_length(data)
    version = data[0] & 0x7F
    if version != SERIAL_VERSION:
        raise SerialError(f"unknown serial version {version} (expected {SERIAL_VERSION})")
    plain = decrypt(data)
    stored = int.from_bytes(plain[5:7], "big")
    computed = checksum(plain)
    return {
        "first_byte": data[0],
        "version": version,
        "is_weapon": bool(data[0] >> 7),
        "unique_id": int.from_bytes(plain[1:5], "little", signed=True),
        "seed": seed_of(data),
        "checksum": stored,
        "checksum_computed": computed,
        "checksum_ok": stored == computed,
        "asset_library_set_id": plain[7],
        "length": len(data),
    }


def with_unique_id(data: bytes, unique_id: int) -> bytes:
    """Return the same serial with a new UniqueId and a recomputed checksum.

    The seed is derived from the unique id bytes, so the result is re-encrypted with the
    NEW seed (as Gibbed's Encode does); the length and all other bits are unchanged.
    Raises SerialError if the input's checksum does not verify or unique_id is out of range.
    """
    if not -(1 << 31) <= unique_id < (1 << 31):
        raise SerialError("unique_id must fit in a signed 32-bit int")
    if not read_header(data)["checksum_ok"]:
        raise SerialError("checksum mismatch; refusing to re-sign a corrupt serial")
    plain = bytearray(decrypt(data))
    plain[1:5] = unique_id.to_bytes(4, "little", signed=True)
    plain[5:7] = checksum(plain).to_bytes(2, "big")
    return encrypt(bytes(plain))


def code_to_bytes(code: str) -> bytes:
    """Decode ``BL2(<base64>)`` (case-insensitive prefix, whitespace and missing padding ok)."""
    m = _CODE_RE.fullmatch(code.strip()) if isinstance(code, str) else None
    if not m:
        raise SerialError("not a BL2(...) code")
    b64 = re.sub(r"\s+", "", m.group(1)).rstrip("=")
    if not b64 or len(b64) % 4 == 1:
        raise SerialError("bad base64 length in code")
    try:
        return base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
    except binascii.Error as exc:
        raise SerialError(f"bad base64 in code: {exc}") from None


def bytes_to_code(data: bytes) -> str:
    """Encode serial bytes as ``BL2(<padded base64>)``, the form Gibbed copies."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SerialError("serial must be non-empty bytes")
    return "BL2(" + base64.b64encode(bytes(data)).decode("ascii") + ")"


def _plausible(data: bytes) -> bool:
    try:
        return read_header(data)["checksum_ok"]
    except SerialError:
        return False


def extract_codes(text: str) -> list[str]:
    """Return every BL2 code in ``text`` as normalised ``BL2(<base64>)``, in order.

    Tolerates labels, markdown, backticks, bullets, [code] tags and whitespace or line
    breaks inside a code. Codes are kept even if their checksum fails (duplicates too).
    A bare base64 token alone on a line is accepted only if it decodes to a version-7
    serial whose checksum verifies.
    """
    found, spans = [], []
    for m in _CODE_RE.finditer(text):
        spans.append(m.span())
        try:
            found.append((m.start(), bytes_to_code(code_to_bytes(m.group(0)))))
        except SerialError:
            continue
    for m in re.finditer(r"^[ \t>*`\-]*([^\s`*]+)[ \t`*]*$", text, re.MULTILINE):
        token = m.group(1)
        if not _BARE_RE.match(token) or any(a <= m.start(1) < b for a, b in spans):
            continue
        try:
            data = base64.b64decode(token + "=" * (-len(token) % 4), validate=True)
        except binascii.Error:
            continue
        if _plausible(data):
            found.append((m.start(1), bytes_to_code(data)))
    return [code for _, code in sorted(found, key=lambda p: p[0])]


# Field order after the 64-bit header (PackedWeapon.Read / PackedItem.Read). A group name is an
# asset reference of CONFIGS[group] bits; None is a plain 7-bit int (ReadInt32(7)).
_WEAPON_FIELDS = (
    ("weapon_type", "WeaponTypes"), ("balance", "BalanceDefs"), ("manufacturer", "Manufacturers"),
    ("manufacturer_grade", None), ("game_stage", None),
    ("body", "WeaponParts"), ("grip", "WeaponParts"), ("barrel", "WeaponParts"),
    ("sight", "WeaponParts"), ("stock", "WeaponParts"), ("elemental", "WeaponParts"),
    ("accessory1", "WeaponParts"), ("accessory2", "WeaponParts"), ("material", "WeaponParts"),
    ("prefix", "WeaponParts"), ("title", "WeaponParts"),
)
_ITEM_FIELDS = (
    ("item_type", "ItemTypes"), ("balance", "BalanceDefs"), ("manufacturer", "Manufacturers"),
    ("manufacturer_grade", None), ("game_stage", None),
    ("alpha", "ItemParts"), ("beta", "ItemParts"), ("gamma", "ItemParts"), ("delta", "ItemParts"),
    ("epsilon", "ItemParts"), ("zeta", "ItemParts"), ("eta", "ItemParts"), ("theta", "ItemParts"),
    ("material", "ItemParts"), ("prefix", "ItemParts"), ("title", "ItemParts"),
)
_HEADER_BITS = 64
_INT_BITS = 7


def _field_bits(group: str | None) -> int:
    if group is None:
        return _INT_BITS
    sublibrary_bits, asset_bits = _assets.CONFIGS[group]
    return sublibrary_bits + asset_bits


def field_layout(is_weapon: bool) -> list[tuple[str, str | None, int, int]]:
    """(name, asset group or None for a 7-bit int, start bit, width) of each packed field."""
    out, pos = [], _HEADER_BITS
    for name, group in _WEAPON_FIELDS if is_weapon else _ITEM_FIELDS:
        width = _field_bits(group)
        out.append((name, group, pos, width))
        pos += width
    return out


def validate_assets(data: bytes) -> dict:
    """Parse and bounds-check the whole packed tail of an (encrypted) serial, offline.

    Call this BEFORE handing a serial to the game. Raises SerialError on any failure.

    Checked here (Gibbed.Borderlands2 PackedDataHelper.Decode, PackedWeapon/PackedItem.Read,
    AssetLibraryManagerHelpers.Decode/Lookup, AssetSublibraryDefinition.GetAsset):
      * length 8..40, version 7, checksum matches (read_header);
      * the header's asset library set id is one of the sets in Gibbed's dump (0-6, 8-10,
        19-22). Gibbed itself only fails when a reference uses an unknown set; RTSE refuses
        the whole serial so the game never sees a set id nobody has tables for;
      * every field is read in Gibbed's order with the bit width of its asset group
        (sublibrary_bits + asset_bits). The all-ones index is Gibbed's None sentinel and is
        allowed for every reference (Gibbed's Lookup returns "None" for any field). Otherwise
        the top sublibrary bit is the use-set-id flag (set = header set id, else base set 0),
        the sublibrary index must exist in that set's library for that group, and the asset
        index must be below that sublibrary's asset count (an empty sublibrary has none);
      * manufacturer grade and game stage are read as 7-bit ints (any 0..127; Gibbed does not
        range-check them);
      * the bits are read from the decrypted serial padded with 0xFF to 40 bytes, exactly as
        Gibbed's Decode does, so a shorter serial reads its missing fields as None. Beyond
        Gibbed's decoder (which ignores them), the serial must not carry bytes past the last
        field (at most 39 bytes for a weapon, 38 for an item) and the pad bits after the last
        field must be 1s, which is what Gibbed's encoder writes (BitWriter fills with 0xFF).

    Still trusted to the game's UnpackSerialNumber:
      * the tables themselves: they are Gibbed's dump of the PC game (Borderlands2Dumps
        08c9a885), with no X360 sublibrary remapping. A game whose asset libraries differ from
        that dump (another platform, a mod that edits the asset library manager) is not covered;
      * meaning and consistency: that a part belongs to the balance's part lists, that the
        balance matches the type and manufacturer, sane grade/stage values, and how the game
        treats None references. Only that each referenced index exists is checked here.

    Returns ``{"is_weapon", "set_id", "length", "bits_read", "manufacturer_grade",
    "game_stage", "fields"}``; ``fields`` maps each reference field name (weapon_type or
    item_type, balance, manufacturer, then the 11 part slots) to None for the None sentinel or
    to ``{"set": int, "sublibrary": int, "asset": int}``.
    """
    header = read_header(data)
    if not header["checksum_ok"]:
        raise SerialError("checksum mismatch")
    set_id = header["asset_library_set_id"]
    if set_id not in _assets.SETS:
        raise SerialError(
            f"unknown asset library set {set_id} (not in Gibbed's asset tables; known sets "
            f"{sorted(_assets.SETS)}): newer DLC or a crafted serial"
        )
    is_weapon = header["is_weapon"]
    plain = decrypt(data)
    bits = int.from_bytes(plain + b"\xff" * (MAX_LENGTH - len(plain)), "little")

    out: dict = {"is_weapon": is_weapon, "set_id": set_id, "length": len(data), "fields": {}}
    end = _HEADER_BITS
    for name, group, start, width in field_layout(is_weapon):
        value = (bits >> start) & ((1 << width) - 1)
        end = start + width
        if group is None:
            out[name] = value
            continue
        if value == (1 << width) - 1:  # AssetLibraryConfiguration.NoneIndex
            out["fields"][name] = None
            continue
        sublibrary_bits, asset_bits = _assets.CONFIGS[group]
        asset = value & ((1 << asset_bits) - 1)
        sublibrary = (value >> asset_bits) & ((1 << (sublibrary_bits - 1)) - 1)
        use_set_id = (value >> (asset_bits + sublibrary_bits - 1)) & 1
        ref_set = set_id if use_set_id else 0
        counts = _assets.SETS[ref_set][group]
        if sublibrary >= len(counts):
            raise SerialError(
                f"{name}: sublibrary {sublibrary} out of range for {group} in set {ref_set} "
                f"({len(counts)} sublibraries)"
            )
        if asset >= counts[sublibrary]:
            raise SerialError(
                f"{name}: asset {asset} out of range for {group} sublibrary {sublibrary} "
                f"in set {ref_set} ({counts[sublibrary]} assets)"
            )
        out["fields"][name] = {"set": ref_set, "sublibrary": sublibrary, "asset": asset}

    used = (end + 7) // 8
    if len(data) > used:
        raise SerialError(f"{len(data) - used} byte(s) past the last field (fields end at bit {end})")
    pad = len(data) * 8 - end
    if pad > 0 and (bits >> end) & ((1 << pad) - 1) != (1 << pad) - 1:
        raise SerialError("pad bits after the last field are not all 1s")
    out["bits_read"] = end
    return out
