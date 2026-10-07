"""Minimal reader for Borderlands 2 .upk packages (read-only): block-compressed LZO wrapper, name/import/export
tables, and UE3 property parsing. Used to pull real data (skill trees) out of the installed game.

    python -I dev/upk.py <file.upk> [class-substring]     lists exports whose class matches
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Any

TAG = 0x9E2A83C1


def lzo1x_decompress(src: bytes | memoryview, expected: int) -> bytes:
    """Pure-Python LZO1X (the algorithm the Linux kernel uses); fast enough for a few dozen MB."""
    out = bytearray()
    ip = 0
    n = len(src)
    t = src[ip]
    state = "loop"
    if t > 17:
        ip += 1
        t -= 17
        out += src[ip:ip + t]
        ip += t
        state = "match_next" if t < 4 else "first_literal_run"
    while True:
        if state == "loop":
            t = src[ip]
            ip += 1
            if t >= 16:
                state = "match"
            else:
                if t == 0:
                    while src[ip] == 0:
                        t += 255
                        ip += 1
                    t += 15 + src[ip]
                    ip += 1
                out += src[ip:ip + t + 3]
                ip += t + 3
                state = "first_literal_run"
        if state == "first_literal_run":
            t = src[ip]
            ip += 1
            if t >= 16:
                state = "match"
            else:
                pos = len(out) - (1 + 0x0800) - (t >> 2) - (src[ip] << 2)
                ip += 1
                out += out[pos:pos + 3] if len(out) - pos >= 3 else _overlap(out, pos, 3)
                state = "match_done"
        if state == "match":
            if t >= 64:
                pos = len(out) - 1 - ((t >> 2) & 7) - (src[ip] << 3)
                ip += 1
                length = (t >> 5) - 1 + 2
            elif t >= 32:
                t &= 31
                if t == 0:
                    while src[ip] == 0:
                        t += 255
                        ip += 1
                    t += 31 + src[ip]
                    ip += 1
                pos = len(out) - 1 - ((src[ip] | (src[ip + 1] << 8)) >> 2)
                ip += 2
                length = t + 2
            else:  # t >= 16
                pos = len(out) - ((t & 8) << 11)
                t &= 7
                if t == 0:
                    while src[ip] == 0:
                        t += 255
                        ip += 1
                    t += 7 + src[ip]
                    ip += 1
                pos -= (src[ip] | (src[ip + 1] << 8)) >> 2
                ip += 2
                if pos == len(out):
                    break  # end of stream
                pos -= 0x4000
                length = t + 2
            out += out[pos:pos + length] if len(out) - pos >= length else _overlap(out, pos, length)
            state = "match_done"
        if state == "match_done":
            t = src[ip - 2] & 3
            state = "match_next"
        if state == "match_next":
            if t == 0:
                state = "loop"
                continue
            out += src[ip:ip + t]
            ip += t
            t = src[ip]
            ip += 1
            state = "match"
    if expected and len(out) != expected:
        raise ValueError(f"LZO produced {len(out)} bytes, expected {expected}")
    return bytes(out)


def _overlap(out: bytearray, pos: int, length: int) -> bytes:
    chunk = bytearray()
    for i in range(length):
        chunk.append(out[pos + i] if pos + i < len(out) else chunk[pos + i - len(out)])
    return bytes(chunk)


def _blocks(data: bytes | memoryview, pos: int) -> bytes:
    """One compressed chunk: tag, block size, totals, a (compressed, raw) table, then the LZO blocks."""
    tag, block_size, _total_comp, total_raw = struct.unpack_from("<IIII", data, pos)
    if tag != TAG:
        raise ValueError("bad chunk tag")
    count = -(-total_raw // block_size)
    table = [struct.unpack_from("<II", data, pos + 16 + 8 * i) for i in range(count)]
    pos += 16 + 8 * count
    out = bytearray()
    view = memoryview(data)
    for comp, raw in table:
        out += lzo1x_decompress(view[pos:pos + comp], raw)
        pos += comp
    return bytes(out)


def _chunked(data: bytes) -> bytes:
    """Packages whose header is plain but whose body is split into compressed chunks listed in the header."""
    version, = struct.unpack_from("<H", data, 4)
    pos = 12
    folder_len, = struct.unpack_from("<i", data, pos)
    pos += 4 + (folder_len if folder_len > 0 else -2 * folder_len)
    pos += 4 + 6 * 4 + 4 + 4 * 4  # flags, name/export/import counts+offsets, depends, guid offsets, thumbnail offset
    pos += 16  # guid
    generations, = struct.unpack_from("<i", data, pos)
    pos += 4 + 12 * generations + 8  # generations, engine + cooker version
    flags, chunk_count = struct.unpack_from("<II", data, pos)
    if flags == 0 or chunk_count == 0:
        return data
    chunks = [struct.unpack_from("<IIII", data, pos + 8 + 16 * i) for i in range(chunk_count)]
    total = max(u_off + u_size for u_off, u_size, _c, _s in chunks)
    out = bytearray(total)
    first = min(u_off for u_off, _u, _c, _s in chunks)
    out[:first] = data[:first]
    for u_off, u_size, c_off, _c_size in chunks:
        raw = _blocks(data, c_off)
        out[u_off:u_off + len(raw)] = raw
    return bytes(out)


def decompress_file(path: Path) -> bytes:
    data = path.read_bytes()
    tag, block_size = struct.unpack_from("<II", data, 0)
    if tag != TAG:
        raise ValueError("not an Unreal package")
    if block_size != 0x20000:
        return _chunked(data)
    total_comp, total_raw = struct.unpack_from("<II", data, 8)
    blocks = -(-total_raw // block_size)
    table = [struct.unpack_from("<II", data, 16 + 8 * i) for i in range(blocks)]
    pos = 16 + 8 * blocks
    view = memoryview(data)
    out = bytearray()
    for comp, raw in table:
        out += lzo1x_decompress(view[pos:pos + comp], raw)
        pos += comp
    return bytes(out)


class Package:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        tag, self.version, self.licensee, header_size = struct.unpack_from("<IHHi", raw, 0)
        if tag != TAG:
            raise ValueError("bad package tag")
        pos = 12
        folder_len, = struct.unpack_from("<i", raw, pos)
        pos += 4 + (folder_len if folder_len > 0 else -2 * folder_len)
        flags, self.name_count, name_off, self.export_count, export_off, self.import_count, import_off = struct.unpack_from("<7I", raw, pos)
        self.names = self._read_names(name_off)
        self.imports = self._read_imports(import_off)
        self.exports = self._read_exports(export_off)

    def _fstring(self, pos: int) -> tuple[str, int]:
        n, = struct.unpack_from("<i", self.raw, pos)
        pos += 4
        if n >= 0:
            return self.raw[pos:pos + n - 1].decode("latin-1"), pos + n
        return self.raw[pos:pos - 2 * n - 2].decode("utf-16-le"), pos - 2 * n

    def _read_names(self, pos: int) -> list[str]:
        names = []
        for _ in range(self.name_count):
            name, pos = self._fstring(pos)
            pos += 8  # flags
            names.append(name)
        return names

    def name(self, index: int, number: int = 0) -> str:
        base = self.names[index]
        return base if number == 0 else f"{base}_{number - 1}"

    def _read_imports(self, pos: int) -> list[dict[str, Any]]:
        imports = []
        for _ in range(self.import_count):
            pkg_i, pkg_n, cls_i, cls_n, outer, obj_i, obj_n = struct.unpack_from("<iiiiiii", self.raw, pos)
            pos += 28
            imports.append({"package": self.name(pkg_i, pkg_n), "class": self.name(cls_i, cls_n), "outer": outer, "name": self.name(obj_i, obj_n)})
        return imports

    def _read_exports(self, pos: int) -> list[dict[str, Any]]:
        exports = []
        for _ in range(self.export_count):
            cls, sup, outer, n_i, n_n, arch, flags, size, offset = struct.unpack_from("<iiiiiiQii", self.raw, pos)
            net_count, = struct.unpack_from("<i", self.raw, pos + 44)
            pos += 68 + 4 * net_count
            exports.append({"class": cls, "super": sup, "outer": outer, "name": self.name(n_i, n_n), "archetype": arch, "size": size, "offset": offset})
        return exports

    def object_name(self, ref: int) -> str:
        """Dotted path of an export (positive) or import (negative) reference."""
        if ref == 0:
            return "None"
        parts = []
        while ref != 0:
            if ref > 0:
                e = self.exports[ref - 1]
                parts.append(e["name"])
                ref = e["outer"]
            else:
                i = self.imports[-ref - 1]
                parts.append(i["name"])
                ref = i["outer"]
        return ".".join(reversed(parts))

    def class_name(self, export: dict[str, Any]) -> str:
        ref = export["class"]
        return "Class" if ref == 0 else self.object_name(ref)

    # ---- properties ----

    def properties(self, export: dict[str, Any]) -> dict[str, Any]:
        raw, pos, end = self.raw, export["offset"], export["offset"] + export["size"]
        pos += 4  # net index
        props, _ = self._prop_list(pos, end)
        return props

    def _prop_list(self, pos: int, end: int) -> tuple[dict[str, Any], int]:
        raw = self.raw
        props: dict[str, Any] = {}
        while pos + 8 <= end:
            ni, nn = struct.unpack_from("<ii", raw, pos)
            name = self.name(ni, nn)
            pos += 8
            if name == "None":
                break
            ti, tn, size, array_index = struct.unpack_from("<iiii", raw, pos)
            kind = self.name(ti, tn)
            pos += 16
            extra = None
            if kind == "StructProperty":
                extra = self.name(*struct.unpack_from("<ii", raw, pos))
                pos += 8
            elif kind == "ByteProperty":
                extra = self.name(*struct.unpack_from("<ii", raw, pos))
                pos += 8
            elif kind == "BoolProperty":
                value: Any = raw[pos] != 0
                pos += 1
                self._store(props, name, array_index, value)
                continue
            value = self._value(kind, extra, pos, size)
            self._store(props, name, array_index, value)
            pos += size
        return props, pos

    @staticmethod
    def _store(props: dict[str, Any], name: str, index: int, value: Any) -> None:
        if index == 0 and name not in props:
            props[name] = value
        else:
            current = props.setdefault(name, {})
            if not isinstance(current, dict) or "__idx" not in current:
                props[name] = current = {"__idx": True, 0: current}
            current[index] = value

    def _value(self, kind: str, extra: str | None, pos: int, size: int) -> Any:
        raw = self.raw
        if kind == "IntProperty":
            return struct.unpack_from("<i", raw, pos)[0]
        if kind == "FloatProperty":
            return round(struct.unpack_from("<f", raw, pos)[0], 6)
        if kind == "ByteProperty":
            return self.name(*struct.unpack_from("<ii", raw, pos)) if size == 8 else raw[pos]
        if kind == "NameProperty":
            return self.name(*struct.unpack_from("<ii", raw, pos))
        if kind == "ObjectProperty":
            return self.object_name(struct.unpack_from("<i", raw, pos)[0])
        if kind == "StrProperty":
            return self._fstring(pos)[0]
        if kind == "ArrayProperty":
            count, = struct.unpack_from("<i", raw, pos)
            return self._array(count, pos + 4, pos + size)
        if kind == "StructProperty":
            if extra in {"Vector", "Rotator", "Color", "LinearColor", "Guid"} or size in {4, 8, 12, 16} and extra not in {None} and extra.startswith("Attribute") is False and False:
                return raw[pos:pos + size].hex()
            try:
                inner, _ = self._prop_list(pos, pos + size)
                return {"__struct": extra, **inner}
            except Exception:  # noqa: BLE001
                return raw[pos:pos + size].hex()
        return raw[pos:pos + size].hex()

    def _array(self, count: int, pos: int, end: int) -> Any:
        raw = self.raw
        size = end - pos
        if count == 0:
            return []
        stride = size // count if count else 0
        if stride == 1 and size == count:
            return {"__bytes": list(raw[pos:end])}  # bool / byte arrays
        if stride == 4 and size == 4 * count:
            ints = struct.unpack_from(f"<{count}i", raw, pos)
            # object refs and plain ints look the same here; keep ints, resolve later if they point at objects
            return {"__ints": list(ints)}
        # element-by-element property lists (structs)
        items = []
        p = pos
        try:
            for _ in range(count):
                inner, p = self._prop_list(p, end)
                items.append(inner)
            return items
        except Exception:  # noqa: BLE001
            return {"__raw": raw[pos:end].hex()[:200]}


def load(path: Path) -> Package:
    return Package(decompress_file(path))


if __name__ == "__main__":
    pkg = load(Path(sys.argv[1]))
    needle = sys.argv[2].lower() if len(sys.argv) > 2 else ""
    print(f"v{pkg.version} names={len(pkg.names)} exports={len(pkg.exports)} imports={len(pkg.imports)}")
    shown = 0
    for i, e in enumerate(pkg.exports, start=1):
        cls = pkg.class_name(e)
        if needle in cls.lower():
            print(cls, pkg.object_name(i))
            shown += 1
            if shown > 40:
                break
