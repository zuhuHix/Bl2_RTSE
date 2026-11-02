"""Outer layer of Borderlands 2 item serials and Gibbed "BL2(...)" item codes.

What this does (pure stdlib, offline):
  * finds ``BL2(<base64>)`` codes in pasted text and converts them to and from bytes;
  * decrypts and encrypts a serial ("BogoDecrypt"/"BogoEncrypt") and checks or recomputes
    its 16-bit CRC32-derived checksum;
  * reads the fixed-width header (version, is_weapon, unique id, checksum, asset library
    set id) and rewrites the unique id.

What it does NOT do: decode the item's parts (type, balance, manufacturer, level, parts).
Those follow the header as asset references whose bit widths come from the game's asset
library tables, so even the 7-bit manufacturer grade and game stage fields sit behind
variable-width references. RTSE uses the game's own UnpackSerialNumber for that part.

Layout (bit reader/writer is LSB-first within each byte, values little-endian):
  bits 0-6   version (7 bits; Gibbed only accepts AssetLibraryManager.Version = 7)
  bit  7     is_weapon (1 bit)
  bits 8-39  unique_id (32 bits, signed Int32); bytes 1-4 are NOT encrypted
  bits 40-55 checksum slot (16 bits): written as 0xFFFF, then replaced by the checksum,
             stored big-endian in bytes 5-6
  bits 56-63 asset_library_set_id (8 bits)
  bits 64+   packed weapon/item data (asset-library dependent; not parsed here)
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
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib

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
