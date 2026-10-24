"""CAFFのキー・圧縮・全entry検査と損失のないXML更新の回帰。"""

import hashlib
import importlib.util
import io
import struct
import sys
from pathlib import Path
import zipfile
import zlib

import pytest
from PIL import Image
from path_input_fixture import fixture_path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "caff_identity_test_engine", ROOT / "scripts/identity_normalization.py"
)
ENGINE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ENGINE)
CAFF = ENGINE._CAFF_MODULE
DENIED = "caff-fixture@example.invalid"


def png(values=b"clean pixels", text=None):
    image = Image.frombytes("P", (len(values), 1), values)
    image.putpalette([v for x in range(256) for v in (x, x, x)])
    stream = io.BytesIO()
    from PIL.PngImagePlugin import PngInfo

    meta = PngInfo()
    if text is not None:
        meta.add_text("comment", text)
    image.save(stream, format="PNG", optimize=False, pnginfo=meta)
    return stream.getvalue()


def archive(
    xml=b"<root><name>ordinary</name></root>",
    key=0x12345678,
    compression=33,
    image=None,
    streaming=False,
    zip_damage=None,
):
    def integer(value, size):
        low = key & 0xFFFFFFFF
        mask = (
            ((0xFFFFFFFF if key < 0 else low) << 32) | low
            if size == 8
            else key & ((1 << (8 * size)) - 1)
        )
        return (value ^ mask).to_bytes(size, "big")

    def string(value):
        value = value.encode()
        assert len(value) < 128
        return integer(len(value), 1) + bytes(v ^ (key & 255) for v in value)

    entries = [("main.xml", "main_xml", xml, compression)]
    if image is not None:
        entries.insert(0, ("layer.png", "image", image, 16))
    bodies = []
    for name, tag, content, method in entries:
        if method != 16:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
                if zip_damage == "comment":
                    zf.comment = DENIED.encode()
                if zip_damage == "extra":
                    info = zipfile.ZipInfo("contents")
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.extra = (
                        struct.pack("<HH", 0x1234, len(DENIED)) + DENIED.encode()
                    )
                    zf.writestr(info, content)
                else:
                    zf.writestr("contents", content)
            content = stream.getvalue()
            if zip_damage == "eof":
                damaged = bytearray(content)
                damaged[38] &= 0xFE
                content = bytes(damaged)
            if streaming:
                compressor = zlib.compressobj(wbits=-15)
                packed = compressor.compress(xml) + compressor.flush()
                name = b"contents"
                content = (
                    struct.pack(
                        "<IHHHHHIIIHH",
                        0x04034B50,
                        20,
                        0x808,
                        8,
                        0,
                        0,
                        0,
                        0,
                        0,
                        len(name),
                        0,
                    )
                    + name
                    + packed
                    + struct.pack(
                        "<IIII", 0x08074B50, zlib.crc32(xml), len(packed), len(xml)
                    )
                )
        bodies.append(bytes(v ^ (key & 255) for v in content))
    table_size = 4 + sum(len(string(n)) + len(string(t)) + 22 for n, t, _, _ in entries)
    offset = 54 + table_size
    rows = []
    for (name, tag, _, method), body in zip(entries, bodies):
        rows.append(
            string(name)
            + string(tag)
            + integer(offset, 8)
            + integer(len(body), 4)
            + integer(1, 1)
            + integer(method, 1)
            + bytes(8)
        )
        offset += len(body)
    header = b"CAFF\0\0\0----\0\0\0" + struct.pack(">i", key) + bytes(36)
    return header + integer(len(entries), 4) + b"".join(rows) + b"".join(bodies) + b"bc"


@pytest.mark.parametrize("key", [0, 0x12345678, -816980164, -29])
@pytest.mark.parametrize("compression", [16, 33, 37])
def test_key_and_compression_are_per_archive(key, compression):
    raw = archive(key=key, compression=compression, image=png())
    assert ENGINE.scan_private_caff(raw, [DENIED])["status"] == "pass"


def test_xml_paths_are_detected():
    value = fixture_path("macos", "caff-fixture", "source.psd")
    raw = archive(xml=f"<root><source>{value}</source></root>".encode())
    assert ENGINE.scan_private_caff(raw, [DENIED])["local_path_match"]


def test_every_embedded_image_is_scanned():
    assert (
        ENGINE.scan_private_caff(archive(image=png(text=DENIED)), [DENIED])["status"]
        == "fail"
    )


@pytest.mark.parametrize(
    "damage", ["truncated", "guard", "version", "offset", "zip_crc"]
)
def test_invalid_archive_never_passes(damage):
    raw = bytearray(archive())
    entry = CAFF.Archive(bytes(raw)).entries[0]
    if damage == "truncated":
        raw = raw[:70]
    elif damage == "guard":
        raw[-1] ^= 1
    elif damage == "version":
        raw[4] = 99
    elif damage == "offset":
        raw[entry.start_field : entry.start_field + 8] = bytes(8)
    else:
        raw[entry.start + 45] ^= 1
    with pytest.raises(Exception):
        ENGINE.scan_private_caff(bytes(raw), [DENIED])


def test_dtd_and_external_entities_are_not_loaded():
    xml = b'<!DOCTYPE root [<!ENTITY x SYSTEM "file:secret">]><root>&x;</root>'
    with pytest.raises(ValueError, match="caff_unverified"):
        ENGINE.scan_private_caff(archive(xml=xml), [DENIED])


@pytest.mark.parametrize("key", [0x12345678, -816980164])
def test_xml_rewrite_preserves_every_other_entry_and_identity(key):
    raw = archive(image=png(), key=key)
    a = CAFF.Archive(raw)
    updated = a.rewrite_main_xml(
        lambda b: b.replace(b"ordinary", b"replacement is longer")
    )
    after = CAFF.Archive(updated)
    original = {e.name: hashlib.sha256(b).hexdigest() for e, b in a.contents()}
    changed = {e.name: hashlib.sha256(b).hexdigest() for e, b in after.contents()}
    assert original["layer.png"] == changed["layer.png"]
    assert original["main.xml"] != changed["main.xml"]
    assert a.key == after.key
    assert ENGINE.scan_private_caff(updated, [DENIED])["status"] == "pass"


def test_expansion_limit_is_checked_before_zip_read(monkeypatch):
    raw = archive()
    monkeypatch.setattr(CAFF, "MAX_EXPANDED", 8)
    with pytest.raises(ValueError, match="caff_size_limit"):
        ENGINE.scan_private_caff(raw, [DENIED])


def test_cubism_streaming_zip_validates_descriptor_and_crc():
    raw = archive(streaming=True)
    assert ENGINE.scan_private_caff(raw, [DENIED])["status"] == "pass"
    entry = CAFF.Archive(raw).entries[0]
    damaged = bytearray(raw)
    damaged[entry.start + entry.size - 12] ^= 1
    with pytest.raises(ValueError, match="caff_unverified"):
        ENGINE.scan_private_caff(bytes(damaged), [DENIED])


def test_xml_comments_cannot_hide_constructed_private_metadata():
    raw = archive(xml=b'<root><!-- "caff-fixture" + "@example.invalid" --></root>')
    assert ENGINE.scan_private_caff(raw, [DENIED])["status"] == "fail"


def test_image_cache_is_bound_to_policy():
    raw = archive(image=png(text=DENIED))
    cache = {}
    assert (
        ENGINE.scan_private_caff(raw, ["other-fixture-value"], cache)["status"]
        == "pass"
    )
    assert ENGINE.scan_private_caff(raw, [DENIED], cache)["status"] == "fail"


@pytest.mark.parametrize("damage", ["comment", "extra", "eof"])
def test_zip_metadata_and_missing_deflate_end_are_not_clean(damage):
    raw = archive(zip_damage=damage)
    with pytest.raises(ValueError, match="caff_unverified"):
        ENGINE.scan_private_caff(raw, [DENIED])


def test_cache_policy_boundaries_cannot_collide():
    raw = archive(image=png(text="beta"))
    cache = {}
    assert ENGINE.scan_private_caff(raw, ["alpha\nbeta"], cache)["status"] == "pass"
    assert ENGINE.scan_private_caff(raw, ["alpha", "beta"], cache)["status"] == "fail"


def test_xml_node_limit_applies_during_construction(monkeypatch):
    monkeypatch.setattr(CAFF, "MAX_XML_NODES", 3)
    with pytest.raises(ValueError, match="caff_size_limit"):
        ENGINE.scan_private_caff(archive(xml=b"<r><a/><b/><c/></r>"), [DENIED])
