from __future__ import annotations

import hashlib
import re
import struct
import tempfile
import zlib
from pathlib import Path, PurePosixPath

# Live2D Cubism の編集file (.cmo3) は XOR した zip entry に XML を deflate で
# 入れている。生byteには個人pathが現れないので、展開した XML を判定する。
# 形式は実物からの実測で、公開仕様ではない。
CMO3_SUFFIX = ".cmo3"
CMO3_ENTRY_NAME = b"contents"
_CMO3_XOR_TABLE = bytes(value ^ 0xE3 for value in range(256))
_ZIP_LOCAL_HEADER = re.compile(re.escape(b"PK\x03\x04"))


def media_kind(data: bytes, source_names=()):
    """Declared media and magic share one fail-closed dispatch in tree and blobs."""
    declared = {Path(name).suffix.lower().lstrip(".") for name in source_names}
    declared &= {"pdf", "mp3", "mp4"}
    magic = None
    if data.startswith(b"%PDF-"):
        magic = "pdf"
    elif data.startswith(b"ID3") or (
        len(data) >= 4
        and data[0] == 255
        and data[1] & 224 == 224
        and data[1] & 6 == 2  # MPEG Layer III, not an arbitrary FF/UTF-16 BOM.
        and data[1] & 24 != 8  # reserved MPEG version
        and data[2] & 12 != 12  # reserved sample rate
        and data[2] >> 4 != 15  # reserved bitrate
    ):
        magic = "mp3"
    elif len(data) >= 8 and data[4:8] == b"ftyp":
        magic = "mp4"
    if declared and (len(declared) != 1 or magic not in declared):
        raise ValueError("private_identity_media_format_mismatch")
    return magic or next(iter(declared), None)


def media_candidate(data: bytes, source_names=()):
    try:
        return media_kind(data, source_names) is not None
    except ValueError:
        return True


def _metadata_media_names(names) -> bool:
    suffixes = {PurePosixPath(name).suffix.lower() for name in names}
    return bool(suffixes) and (
        suffixes <= {".mp4"} or suffixes <= {".png"} or suffixes <= {".jpg", ".jpeg"}
    )


def _inspect_history_media(
    repo,
    object_id,
    size,
    names,
    identity,
    *,
    git,
    load_providers,
    secret_matches,
    personal_path_matches,
    decoded_budget_bytes,
):
    """非公開送信の明示policy。Git objectと検査済みmetadataを同じSHAへ結ぶ。"""
    if size <= 0 or size > 64_000_000:
        raise RuntimeError("media_metadata_size_limit")
    if identity.report.get("provider_kind") != "bundled_canonical":
        raise RuntimeError("media_metadata_provider_unverified")
    providers = load_providers(identity)
    with tempfile.TemporaryDirectory(prefix="preflight-media-") as directory:
        path = Path(directory) / ("asset" + PurePosixPath(names[0]).suffix.lower())
        with path.open("xb") as target:
            fetched = git.run_subprocess(
                ["git", "cat-file", "blob", object_id],
                cwd=repo,
                stdout=target,
                stderr=git.PIPE,
                timeout=60,
            )
        if fetched.returncode or path.stat().st_size != size:
            raise RuntimeError("git_history_inventory_failed")
        oid = hashlib.new(
            "sha1" if len(object_id) == 40 else "sha256", usedforsecurity=False
        )
        oid.update(f"blob {size}\0".encode("ascii"))
        sha = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                oid.update(chunk)
                sha.update(chunk)
        if oid.hexdigest() != object_id:
            raise RuntimeError("git_history_inventory_failed")
        result = providers.metadata.inspect_media_metadata(
            path,
            tuple(names),
            probe_runner=providers.probe._run,
            xml_reader=providers.probe._pdf_xml_metadata,
        )
        if (
            result["source_sha256"] != sha.hexdigest()
            or type(result["size_bytes"]) is not int
            or result["size_bytes"] != size
            or result.get("payload_inspected") is not False
        ):
            raise RuntimeError("media_metadata_source_changed")
    if not providers.unchanged():
        raise RuntimeError("media_metadata_provider_changed")
    # Decode each raw item before joining: byte order/alignment must not depend
    # on adjacent binary fields or the single-byte separator.
    decoded_views = []
    seen_views = set()
    decoded_size = 0
    raw_views = [
        *result["metadata_views"],
        result["head_sample"],
        result["tail_sample"],
    ]
    for raw in raw_views:
        candidates = [("utf-8", 0), ("latin-1", 0)]
        candidates.extend(
            (codec, offset) for codec in ("utf-16-le", "utf-16-be") for offset in (0, 1)
        )
        for codec, offset in candidates:
            value = raw[offset:].decode(codec, "replace").encode("utf-8")
            if value in seen_views:
                continue
            decoded_size += len(value) + 1
            if decoded_size > decoded_budget_bytes:
                raise RuntimeError("media_metadata_decoded_limit")
            seen_views.add(value)
            decoded_views.append(value)
    views = [b"\n".join(decoded_views)]
    secret_found = path_found = False
    for view in views:
        secret_found |= bool(secret_matches(view))
        path_found |= bool(personal_path_matches(view))
        identity.check(b"media metadata\n" + view)
        path_found |= identity.last_text_path_match
    if identity.report.get("status") == "tool_error":
        raise RuntimeError("media_metadata_callback_failed")
    record = {
        "git_object": object_id,
        "source_sha256": sha.hexdigest(),
        "size_bytes": size,
        "kind": result["kind"],
        "head_sample_bytes": len(result["head_sample"]),
        "tail_sample_bytes": len(result["tail_sample"]),
        "payload_inspected": False,
        "inspector_sha256": hashlib.sha256(providers.metadata_bytes).hexdigest(),
    }
    identity.report.setdefault("metadata_media_inspections", []).append(record)
    return secret_found, path_found


def changed_committed_media_entries(entries) -> dict[str, str]:
    return {
        new_oid: name
        for _, _, name, _, mode, _, new_oid in entries
        if mode in {b"100644", b"100755"}
        and PurePosixPath(name).suffix.lower() in {".pdf", ".mp3", ".mp4"}
    }


def cmo3_contents_xml(
    data: bytes, *, max_xml_bytes: int, load_caff_decoder
) -> bytes | None:
    """Return the ``contents`` XML inside a .cmo3, or None if it cannot be read.

    XOR-ing every byte with 0xE3 exposes a zip local file header.  Its flag
    0x0808 leaves the header sizes at zero, so the raw deflate stream right
    after the header is inflated until the stream itself ends, then checked
    against the CRC-32 and size in the data descriptor that follows it (raw
    deflate alone can decode damaged bytes into plausible garbage).  The header
    offset is not fixed, so every signature is tried.  A truncated or corrupt
    entry, or expanding past ``CMO3_MAX_XML_BYTES`` across all entries, makes
    the whole file unreadable rather than partly clean.
    """
    if data[:14] == b"CAFF\0\0\0----\0\0\0":
        # 実際のCAFFはヘッダにentry用キーを持つ。旧固定キーheuristicと混ぜない。
        try:
            decoder = load_caff_decoder()
            documents = [
                content
                for entry, content in decoder["Archive"](data).contents()
                if entry.tag == "main_xml"
            ]
            if len(documents) != 1 or len(documents[0]) > max_xml_bytes:
                return None
            return documents[0]
        except Exception:
            return None
    plain = data.translate(_CMO3_XOR_TABLE)
    documents: list[bytes] = []
    for match in _ZIP_LOCAL_HEADER.finditer(plain):
        offset = match.start()
        if len(plain) < offset + 30:
            continue
        (method,) = struct.unpack_from("<H", plain, offset + 8)
        name_length, extra_length = struct.unpack_from("<HH", plain, offset + 26)
        name_start = offset + 30
        if (
            method != 8
            or plain[name_start : name_start + name_length] != CMO3_ENTRY_NAME
        ):
            continue
        # 上限は全entryの合計に効かせる (0 は zlib で「無制限」の意味になる)
        remaining = max_xml_bytes - sum(map(len, documents))
        if remaining <= 0:
            return None
        inflater = zlib.decompressobj(-15)
        try:
            document = inflater.decompress(
                plain[name_start + name_length + extra_length :], remaining
            )
        except zlib.error:
            return None
        if not inflater.eof:
            return None
        descriptor = inflater.unused_data
        if descriptor.startswith(b"PK\x07\x08"):
            descriptor = descriptor[4:]
        if len(descriptor) < 12:
            return None
        crc, _compressed_size, size = struct.unpack_from("<III", descriptor)
        if crc != zlib.crc32(document) or size != len(document):
            return None
        documents.append(document)
    return b"\n".join(documents) if documents else None
