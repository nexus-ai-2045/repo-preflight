"""CAFFの有界読み取りと、XML以外を維持する部分更新。

形式の一次実装: MangoLion/stretchystudio の cmo3_decrypt.py と caff_packer.py。
ZIPの展開・CRC検証は標準ライブラリを再利用する。未知の形式は受理しない。
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import struct
import zipfile
import zlib

MAX_INPUT = 128 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_XML = 64 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_XML_NODES = 100000


def _error():
    return ValueError("private_identity_caff_unverified")


class Reader:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise _error()
        raw = self.data[self.pos : self.pos + size]
        self.pos += size
        return raw

    def integer(self, size, key=0):
        mask = key & ((1 << (size * 8)) - 1)
        if size == 8:
            low = key & 0xFFFFFFFF
            mask = ((0xFFFFFFFF if key < 0 else low) << 32) | low
        return int.from_bytes(self.take(size), "big") ^ mask

    def string(self, key):
        size = 0
        for _ in range(4):
            b = self.integer(1, key)
            size = (size << 7) | (b & 127)
            if b < 128:
                if size > 1024 * 1024:
                    raise _error()
                return self.take(size).translate(_table(key)).decode("utf-8", "strict")
        raise _error()


def _table(key):
    return bytes(v ^ (key & 255) for v in range(256))


def _streaming_zip(raw, limit):
    # Cubismは中央directoryのないZIP streamも保存する。局所header・終端・CRCを必須にする。
    if len(raw) < 38 or raw[:4] != b"PK\x03\x04":
        raise _error()
    fields = struct.unpack_from("<HHHHHIIIHH", raw, 4)
    (
        version,
        flags,
        method,
        _,
        _,
        crc_header,
        compressed,
        expanded,
        name_len,
        extra_len,
    ) = fields
    if flags not in (8, 0x808) or method != 8 or compressed or expanded or crc_header:
        raise _error()
    if extra_len:
        raise _error()
    offset = 30 + name_len + extra_len
    if raw[30 : 30 + name_len] != b"contents" or offset >= len(raw):
        raise _error()
    decoder = zlib.decompressobj(-15)
    try:
        value = decoder.decompress(raw[offset:], limit + 1)
    except zlib.error:
        raise _error() from None
    if len(value) > limit:
        raise ValueError("private_identity_caff_size_limit")
    if not decoder.eof:
        raise _error()
    tail = decoder.unused_data
    compressed_size = len(raw) - offset - len(tail)
    if tail.startswith(b"PK\x07\x08"):
        tail = tail[4:]
    if len(tail) != 12:
        raise _error()
    crc, stored_size, size = struct.unpack("<III", tail)
    if crc != zlib.crc32(value) or stored_size != compressed_size or size != len(value):
        raise _error()
    return value


def _regular_zip(raw, zf, info, limit):
    if (
        zf.comment
        or info.comment
        or info.extra
        or info.header_offset != 0
        or info.flag_bits not in (0, 8, 0x800, 0x808)
        or info.compress_type not in (0, 8)
        or len(raw) < 52
    ):
        raise _error()
    end = struct.unpack("<4s4H2LH", raw[-22:])
    if (
        end[0] != b"PK\x05\x06"
        or end[1:5] != (0, 0, 1, 1)
        or end[-1]
        or end[6] != zf.start_dir
        or end[5] + end[6] != len(raw) - 22
    ):
        raise _error()
    if raw[:4] != b"PK\x03\x04":
        raise _error()
    _, flags, method, _, _, crc, packed, expanded, name_len, extra_len = (
        struct.unpack_from("<HHHHHIIIHH", raw, 4)
    )
    if (
        flags != info.flag_bits
        or method != info.compress_type
        or extra_len
        or raw[30 : 30 + name_len] != b"contents"
    ):
        raise _error()
    offset = 30 + name_len
    payload_end = offset + info.compress_size
    if payload_end > zf.start_dir:
        raise _error()
    descriptor = raw[payload_end : zf.start_dir]
    if flags & 8:
        if descriptor.startswith(b"PK\x07\x08"):
            descriptor = descriptor[4:]
        if len(descriptor) != 12 or struct.unpack("<III", descriptor) != (
            info.CRC,
            info.compress_size,
            info.file_size,
        ):
            raise _error()
    elif descriptor or (crc, packed, expanded) != (
        info.CRC,
        info.compress_size,
        info.file_size,
    ):
        raise _error()
    if info.file_size > limit or limit <= 0:
        raise ValueError("private_identity_caff_size_limit")
    payload = raw[offset:payload_end]
    if method == 8:
        decoder = zlib.decompressobj(-15)
        value = decoder.decompress(payload, limit + 1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise _error()
    else:
        value = payload
    if len(value) > limit:
        raise ValueError("private_identity_caff_size_limit")
    if len(value) != info.file_size or zlib.crc32(value) != info.CRC:
        raise _error()
    return value


@dataclass
class Entry:
    name: str
    tag: str
    start: int
    size: int
    obfuscated: bool
    compression: int
    start_field: int
    size_field: int


class Archive:
    def __init__(self, data):
        self.data = data
        if len(data) > MAX_INPUT:
            raise ValueError("private_identity_caff_size_limit")
        if len(data) < 60 or data[:14] != b"CAFF\0\0\0----\0\0\0" or data[-2:] != b"bc":
            raise _error()
        self.key = int.from_bytes(data[14:18], "big", signed=True)
        self.preview_start = int.from_bytes(data[34:42], "big")
        self.preview_size = int.from_bytes(data[42:46], "big")
        r = Reader(data, 54)
        count = r.integer(4, self.key)
        if not 1 <= count <= MAX_ENTRIES:
            raise _error()
        entries = []
        for _ in range(count):
            name, tag = r.string(self.key), r.string(self.key)
            start_field = r.pos
            start = r.integer(8, self.key)
            size_field = r.pos
            size = r.integer(4, self.key)
            obfuscated, compression = r.integer(1, self.key), r.integer(1, self.key)
            r.take(8)
            if obfuscated not in (0, 1) or compression not in (16, 33, 37):
                raise _error()
            entries.append(
                Entry(
                    name,
                    tag,
                    start,
                    size,
                    bool(obfuscated),
                    compression,
                    start_field,
                    size_field,
                )
            )
        if len({e.name for e in entries}) != len(entries):
            raise _error()
        spans = [(e.start, e.start + e.size) for e in entries]
        if self.preview_size:
            spans.append((self.preview_start, self.preview_start + self.preview_size))
        previous = r.pos
        for start, end in sorted(spans):
            if start < previous or end <= start or end > len(data) - 2:
                raise _error()
            if any(data[previous:start]):
                raise _error()
            previous = end
        if any(data[previous:-2]):
            raise _error()
        self.entries = entries
        self.table_end = r.pos

    def stored(self, entry):
        raw = self.data[entry.start : entry.start + entry.size]
        return raw.translate(_table(self.key)) if entry.obfuscated else raw

    def contents(self):
        total = 0
        for entry in self.entries:
            raw = self.stored(entry)
            if entry.compression == 16:
                value = raw
            else:
                try:
                    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                        infos = zf.infolist()
                        if len(infos) != 1 or infos[0].filename != "contents":
                            raise _error()
                        value = _regular_zip(raw, zf, infos[0], MAX_EXPANDED - total)
                except zipfile.BadZipFile:
                    value = _streaming_zip(raw, MAX_EXPANDED - total)
                except (
                    RuntimeError,
                    EOFError,
                    OSError,
                    NotImplementedError,
                    zlib.error,
                ):
                    raise _error() from None
            total += len(value)
            if total > MAX_EXPANDED or (
                entry.tag == "main_xml" and len(value) > MAX_XML
            ):
                raise ValueError("private_identity_caff_size_limit")
            yield entry, value

    def rewrite_main_xml(self, transform):
        """全entryを検証し、main_xmlのZIPと後続offsetだけを更新する。"""
        decoded = list(self.contents())
        main = [(e, b) for e, b in decoded if e.tag == "main_xml"]
        if len(main) != 1:
            raise _error()
        entry, original = main[0]
        updated = transform(original)
        if updated == original:
            return self.data
        if len(updated) > MAX_XML:
            raise ValueError("private_identity_caff_size_limit")
        if entry.compression == 16:
            stored = updated
        else:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("contents", updated)
            stored = stream.getvalue()
        if entry.obfuscated:
            stored = stored.translate(_table(self.key))
        difference = len(stored) - entry.size
        result = bytearray(
            self.data[: entry.start] + stored + self.data[entry.start + entry.size :]
        )

        def put(pos, value, size, key):
            low = key & 0xFFFFFFFF
            mask = (
                (((0xFFFFFFFF if key < 0 else low) << 32) | low) if size == 8 else low
            )
            result[pos : pos + size] = (value ^ mask).to_bytes(size, "big")

        for item in self.entries:
            if item.start > entry.start:
                put(item.start_field, item.start + difference, 8, self.key)
        put(entry.size_field, len(stored), 4, self.key)
        if self.preview_size and self.preview_start > entry.start:
            put(34, self.preview_start + difference, 8, 0)
        rebuilt = Archive(bytes(result))
        after = list(rebuilt.contents())
        if len(after) != len(decoded):
            raise _error()
        for (before_entry, before), (after_entry, value) in zip(decoded, after):
            expected = updated if before_entry.tag == "main_xml" else before
            if (before_entry.name, before_entry.tag) != (
                after_entry.name,
                after_entry.tag,
            ) or value != expected:
                raise _error()
        return bytes(result)
