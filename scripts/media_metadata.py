"""Bounded metadata-only reader for private media identity checks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import struct
from numbers import Rational
from pathlib import Path

DATA_LIMIT = 64_000_000
SAMPLE_SIZE = 64 * 1024
TEXT_LIMIT = 2_000_000
NODE_LIMIT = 100_000
EXIF_USER_COMMENT_TAG = 37510


class MediaMetadataError(ValueError):
    """A sanitized, fixed-code error from the metadata reader."""


def _fail(code: str) -> None:
    raise MediaMetadataError(code)


def _decode_user_comment(value: bytes):
    if value.startswith(b"ASCII\x00\x00\x00"):
        return (value[8:].decode("ascii", "replace").rstrip("\x00"),)
    if value.startswith(b"UNICODE\x00"):
        payload = value[8:]
        if len(payload) % 2:
            _fail("metadata_encoding_invalid")
        encodings = (
            ("utf-16",)
            if payload.startswith((b"\xff\xfe", b"\xfe\xff"))
            else ("utf-16-be", "utf-16-le")
        )
        # Both byte orders are scanned when no BOM disambiguates the comment.
        return tuple(
            payload.decode(encoding, "replace").rstrip("\x00") for encoding in encodings
        )
    if value.startswith(b"JIS\x00\x00\x00\x00\x00"):
        return (value[8:].decode("shift_jis", "replace").rstrip("\x00"),)
    return None


def _flatten(value, *, depth=0, count=None):
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > NODE_LIMIT or depth > 32:
        _fail("metadata_limit")
    if isinstance(value, bytes):
        yield value
        yield b"base64:" + base64.b64encode(value)
    elif isinstance(value, str):
        yield value.encode("utf-8", "replace")
    elif value is None or isinstance(value, (bool, int, float)):
        yield str(value).encode("ascii", "replace")
    elif isinstance(value, Rational):
        yield f"{value.numerator}/{value.denominator}".encode("ascii")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _flatten(str(key), depth=depth + 1, count=count)
            yield from _flatten(item, depth=depth + 1, count=count)
            if key in (
                EXIF_USER_COMMENT_TAG,
                str(EXIF_USER_COMMENT_TAG),
                "UserComment",
            ) and isinstance(item, bytes):
                decoded = _decode_user_comment(item)
                if decoded is not None:
                    yield from _flatten(decoded, depth=depth + 1, count=count)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item, depth=depth + 1, count=count)
    else:
        _fail("unsupported_metadata")


def _views(value):
    values = []
    total = 0
    for item in _flatten(value):
        total += len(item)
        if total > TEXT_LIMIT:
            _fail("metadata_limit")
        values.append(item)
    return tuple(values)


def _snapshot(path: Path):
    try:
        st = path.lstat()
    except OSError:
        _fail("input_unavailable")
    if path.is_symlink() or not path.is_file() or not os.path.isfile(path):
        _fail("input_type")
    if st.st_size < 0 or st.st_size > DATA_LIMIT:
        _fail("size_limit")
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _digest_and_samples(path: Path, expected_size: int):
    digest = hashlib.sha256()
    head = bytearray()
    tail = bytearray()
    total = 0
    try:
        with path.open("rb") as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                digest.update(block)
                if len(head) < SAMPLE_SIZE:
                    head.extend(block[: SAMPLE_SIZE - len(head)])
                tail.extend(block)
                if len(tail) > SAMPLE_SIZE:
                    del tail[:-SAMPLE_SIZE]
    except OSError:
        _fail("input_unavailable")
    if total != expected_size:
        _fail("input_changed")
    return digest.hexdigest(), bytes(head), bytes(tail)


def _check_mp4_boxes(path: Path, size: int):
    metadata = []
    metadata_size = 0
    try:
        with path.open("rb") as source:
            header = source.read(16)
            if len(header) < 8 or header[4:8] != b"ftyp":
                _fail("format_mismatch")
            first_size = struct.unpack(">I", header[:4])[0]
            if first_size == 1:
                if len(header) < 16 or struct.unpack(">Q", header[8:16])[0] < 16:
                    _fail("format_invalid")
            elif first_size < 8:
                _fail("format_invalid")
            offset = 0
            boxes = 0
            while offset < size:
                boxes += 1
                if boxes > NODE_LIMIT:
                    _fail("metadata_limit")
                source.seek(offset)
                box = source.read(16)
                if len(box) < 8:
                    _fail("format_invalid")
                box_size = struct.unpack(">I", box[:4])[0]
                header_size = 8
                if box_size == 1:
                    if len(box) < 16:
                        _fail("format_invalid")
                    box_size = struct.unpack(">Q", box[8:16])[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or offset + box_size > size:
                    _fail("format_invalid")
                box_type = box[4:8]
                if box_type != b"mdat":
                    content_size = box_size - header_size
                    metadata_size += len(box_type) + content_size
                    if metadata_size > TEXT_LIMIT:
                        _fail("metadata_limit")
                    source.seek(offset + header_size)
                    content = source.read(content_size)
                    if len(content) != content_size:
                        _fail("format_invalid")
                    metadata.append(box_type + content)
                offset += box_size
    except OSError:
        _fail("input_unavailable")
    return tuple(metadata)


def _bounded_views(*groups):
    values = []
    total = 0
    for group in groups:
        for value in group:
            total += len(value)
            if total > TEXT_LIMIT:
                _fail("metadata_limit")
            values.append(value)
    return tuple(values)


def _mp4_metadata(path, probe_runner):
    probe = shutil.which("ffprobe")
    if not probe:
        _fail("dependency_unavailable")
    args = [
        probe,
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "mov",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-of",
        "json",
        str(path),
    ]
    try:
        code, raw, error = probe_runner(args, limit=TEXT_LIMIT)
    except Exception as exc:
        code = str(exc)
        if code in {"timeout", "output_limit"}:
            _fail(code)
        _fail("probe_failed")
    if code or error:
        _fail("probe_failed")
    try:
        data = json.loads(raw)
        streams = data.get("streams") if isinstance(data, dict) else None
        if not isinstance(streams, list) or not any(
            isinstance(stream, dict)
            and stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic")
            for stream in streams
        ):
            _fail("format_invalid")
        if isinstance(data.get("format"), dict):
            data["format"].pop("filename", None)
        return _views(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail("format_invalid")


def _png_metadata_chunks(path, size):
    """Reject trailing PNG metadata without decoding image pixels."""
    metadata_types = {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}
    try:
        with path.open("rb") as source:
            source.seek(8)
            offset = 8
            count = 0
            after_idat = False
            saw_iend = False
            text_keywords = set()
            saw_exif = False
            while offset < size:
                count += 1
                if count > NODE_LIMIT:
                    _fail("metadata_limit")
                header = source.read(8)
                if len(header) != 8:
                    _fail("format_invalid")
                length = struct.unpack(">I", header[:4])[0]
                chunk_type = header[4:]
                end = offset + 12 + length
                if end > size:
                    _fail("format_invalid")
                if after_idat and chunk_type in metadata_types:
                    _fail("metadata_after_image_data")
                if chunk_type in {b"tEXt", b"zTXt", b"iTXt"}:
                    prefix = source.read(min(length, 80))
                    keyword, separator, _ = prefix.partition(b"\x00")
                    if not separator or not 1 <= len(keyword) <= 79:
                        _fail("format_invalid")
                    if keyword in text_keywords:
                        _fail("duplicate_metadata_keyword")
                    text_keywords.add(keyword)
                elif chunk_type == b"eXIf":
                    if saw_exif:
                        _fail("duplicate_exif")
                    saw_exif = True
                if chunk_type == b"IDAT":
                    after_idat = True
                elif chunk_type == b"IEND":
                    if length != 0 or end != size:
                        _fail("format_invalid")
                    saw_iend = True
                source.seek(end)
                offset = end
                if saw_iend:
                    break
            if not saw_iend:
                _fail("format_invalid")
    except OSError:
        _fail("input_unavailable")


def _check_jpeg_metadata_boundaries(path):
    """Reject metadata after the first scan; Pillow's lazy view stops at SOS.

    Follow JPEG marker boundaries like the existing strict reader, without
    decoding pixels or treating entropy-coded image data as text.
    """
    with path.open("rb") as source:
        data = source.read(DATA_LIMIT + 1)
    if len(data) > DATA_LIMIT:
        _fail("size_limit")
    position = 2
    in_scan = seen_scan = False
    markers = 0
    while position < len(data):
        if in_scan:
            position = data.find(b"\xff", position)
            if position < 0:
                _fail("format_invalid")
            end = position + 1
            while end < len(data) and data[end] == 255:
                end += 1
            if end == len(data):
                _fail("format_invalid")
            if data[end] == 0 or 0xD0 <= data[end] <= 0xD7:
                position = end + 1
                continue
            in_scan = False
        if data[position] != 255:
            _fail("format_invalid")
        while position < len(data) and data[position] == 255:
            position += 1
        if position >= len(data):
            _fail("format_invalid")
        kind = data[position]
        position += 1
        markers += 1
        if markers > NODE_LIMIT:
            _fail("metadata_limit")
        if kind == 0xD9:
            if not seen_scan or position != len(data):
                _fail("format_invalid")
            return
        if kind not in {0xC0, 0xC2, 0xC4, 0xDA, 0xDB, 0xDD, 0xFE, *range(0xE0, 0xF0)}:
            _fail("format_invalid")
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            _fail("format_invalid")
        if seen_scan and (kind == 0xFE or 0xE0 <= kind <= 0xEF):
            _fail("metadata_after_image_data")
        position += length
        if kind == 0xDA:
            in_scan = seen_scan = True
    _fail("format_invalid")


def _image_metadata(path, ext):
    try:
        from PIL import Image
    except ImportError:
        _fail("dependency_unavailable")
    try:
        with Image.open(path) as image:
            if (ext == ".png" and image.format != "PNG") or (
                ext in {".jpg", ".jpeg"} and image.format != "JPEG"
            ):
                _fail("format_mismatch")
            metadata = {"format": image.format, "info": image.info}
            if image.format == "JPEG":
                segments = list(image.applist)
                if (
                    sum(
                        kind == "APP1" and data.startswith(b"Exif\x00\x00")
                        for kind, data in segments
                    )
                    > 1
                ):
                    _fail("duplicate_exif")
                # info keeps only selected/latest values; applist preserves each APP/COM occurrence.
                metadata["jpeg_segments"] = segments
            if ext in {".jpg", ".jpeg"} or "exif" in image.info:
                exif = image.getexif()
                if exif:
                    metadata["exif"] = dict(exif)
                    # Pillow exposes these IFDs separately from the top-level map.
                    for ifd_tag in (34665, 34853):
                        if ifd_tag in exif:
                            child = exif.get_ifd(ifd_tag)
                            metadata[f"exif_ifd_{ifd_tag}"] = dict(child)
                            if ifd_tag == 34665 and 40965 in child:
                                metadata["exif_interop"] = dict(exif.get_ifd(40965))
            return _views(metadata)
    except MediaMetadataError:
        raise
    except Exception:
        _fail("format_invalid")


def inspect_media_metadata(
    path: Path, source_names: tuple[str, ...], *, probe_runner=None
) -> dict:
    """Read bounded metadata and samples without decoding image pixels or video frames."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in {".mp4", ".png", ".jpg", ".jpeg"}:
        _fail("unsupported_extension")
    if not isinstance(source_names, tuple) or any(
        not isinstance(name, str) for name in source_names
    ):
        _fail("source_name_mismatch")
    expected_family = "jpeg" if ext in {".jpg", ".jpeg"} else ext
    for name in source_names:
        named_ext = Path(name).suffix.lower()
        named_family = "jpeg" if named_ext in {".jpg", ".jpeg"} else named_ext
        if named_family != expected_family:
            _fail("source_name_mismatch")
    before = _snapshot(path)
    digest, head, tail = _digest_and_samples(path, before[2])
    if ext == ".mp4":
        container_views = _check_mp4_boxes(path, before[2])
        if head[4:8] != b"ftyp":
            _fail("format_mismatch")
        if probe_runner is None:
            try:
                from .media_identity import MediaError, _run
            except ImportError:  # Direct execution with scripts/ on sys.path.
                from media_identity import MediaError, _run

            def probe_runner(arguments, *, limit):
                try:
                    return _run(arguments, limit=limit)
                except MediaError:
                    raise

        views = _bounded_views(container_views, _mp4_metadata(path, probe_runner))
        kind = "mp4"
    else:
        magic_ok = (
            head.startswith(b"\x89PNG\r\n\x1a\n")
            if ext == ".png"
            else head.startswith(b"\xff\xd8\xff")
        )
        if not magic_ok:
            _fail("format_mismatch")
        if ext == ".png":
            _png_metadata_chunks(path, before[2])
        else:
            _check_jpeg_metadata_boundaries(path)
        views = _image_metadata(path, ext)
        kind = "image"
    after = _snapshot(path)
    if after != before:
        _fail("input_changed")
    final_digest, _head, _tail = _digest_and_samples(path, before[2])
    if final_digest != digest or _snapshot(path) != before:
        _fail("input_changed")
    return {
        "source_sha256": digest,
        "size_bytes": before[2],
        "kind": kind,
        "metadata_views": views,
        "head_sample": head,
        "tail_sample": tail,
        "payload_inspected": False,
    }
