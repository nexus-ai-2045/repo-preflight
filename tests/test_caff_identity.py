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

pytest.importorskip(
    "PIL", minversion="13.0", reason="Pillow媒体検査はPython 3.11以上とPillow 13が必要"
)
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


def png(values=b"clean pixels", text=None, compressed_text=False, compress_level=6):
    image = Image.frombytes("P", (len(values), 1), values)
    image.putpalette([v for x in range(256) for v in (x, x, x)])
    stream = io.BytesIO()
    from PIL.PngImagePlugin import PngInfo

    meta = PngInfo()
    if text is not None:
        meta.add_text("comment", text, zip=compressed_text)
    image.save(
        stream,
        format="PNG",
        optimize=False,
        pnginfo=meta,
        compress_level=compress_level,
    )
    return stream.getvalue()


def archive(
    xml=b"<root><name>ordinary</name></root>",
    key=0x12345678,
    compression=33,
    image=None,
    streaming=False,
    zip_damage=None,
    preview=None,
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
            if zip_damage == "directory_name":
                damaged = bytearray(content)
                central = damaged.index(b"PK\x01\x02")
                suffix = b"\0" + DENIED.encode()
                length = struct.unpack_from("<H", damaged, central + 28)[0]
                damaged[central + 46 + length : central + 46 + length] = suffix
                struct.pack_into("<H", damaged, central + 28, length + len(suffix))
                size = struct.unpack_from("<I", damaged, len(damaged) - 10)[0]
                struct.pack_into("<I", damaged, len(damaged) - 10, size + len(suffix))
                content = bytes(damaged)
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
    header = bytearray(b"CAFF\0\0\0----\0\0\0" + struct.pack(">i", key) + bytes(36))
    if preview is not None:
        header[34:42] = offset.to_bytes(8, "big")
        header[42:46] = len(preview).to_bytes(4, "big")
    return (
        bytes(header)
        + integer(len(entries), 4)
        + b"".join(rows)
        + b"".join(bodies)
        + (preview or b"")
        + b"bc"
    )


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


def test_preview_metadata_keeps_its_decoded_path_classification():
    value = fixture_path("macos", "preview-fixture", "source.psd")
    raw = archive(preview=png(text=value, compressed_text=True))
    result = ENGINE.scan_private_caff(raw, [DENIED])
    assert result["local_path_match"] and result["metadata_path_match"]


def test_raw_path_review_does_not_waive_preview_metadata(tmp_path):
    value = fixture_path("macos", "preview-fixture", "source.psd")
    raw = archive(preview=png(text=value, compressed_text=True))
    spec = importlib.util.spec_from_file_location(
        "caff_readiness_test", ROOT / "scripts/readiness_scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    repo.mkdir()
    scan = module.PrivateIdentityScan(repo, True)
    assert scan.checker is not None
    scan.check(raw, reviewed_raw_paths=True)
    assert scan.result()["finding_count"] == 1
    assert scan.result()["status"] == "fail"


@pytest.mark.parametrize("preview", [True, False])
def test_validated_png_pixels_keep_their_non_text_role(preview):
    image = png(values=DENIED.encode(), compress_level=0)
    assert DENIED.encode() in image
    assert ENGINE.scan_private_png(image, [DENIED])["status"] == "pass"
    raw = archive(key=0, **({"preview": image} if preview else {"image": image}))
    assert ENGINE.scan_private_caff(raw, [DENIED])["status"] == "pass"


def test_xml_entities_then_url_decoding_detects_identity():
    raw = archive(xml=b"<r>caff-fixture&#64;example%2Einvalid</r>")
    assert ENGINE.scan_private_caff(raw, [DENIED])["status"] == "fail"


def test_xml_entities_then_url_decoding_detects_home_path():
    from urllib.parse import quote

    value = quote(fixture_path("macos", "encoded-fixture", "work"), safe="").replace(
        "%", "&#37;"
    )
    raw = archive(xml=f"<r>{value}</r>".encode())
    result = ENGINE.scan_private_caff(raw, [DENIED])
    assert result["local_path_match"] and result["metadata_path_match"]


def test_unknown_reserved_metadata_is_not_silently_ignored():
    raw = bytearray(archive())
    raw[18:22] = b"name"
    with pytest.raises(ValueError, match="caff_unverified"):
        ENGINE.scan_private_caff(bytes(raw), [DENIED])


def test_original_zip_directory_name_is_verified_before_truncation():
    raw = archive(zip_damage="directory_name")
    with pytest.raises(ValueError, match="caff_unverified"):
        ENGINE.scan_private_caff(raw, [DENIED])


@pytest.mark.parametrize("name", [b"CAFF.md", b"CAFF/assets/model.cmo3"])
def test_repository_names_are_not_dispatched_as_container_data(tmp_path, name):
    spec = importlib.util.spec_from_file_location(
        "caff_name_readiness", ROOT / "scripts/readiness_scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    repo.mkdir()
    scan = module.PrivateIdentityScan(repo, True)
    assert scan.checker is not None
    scan.check(name, repository_name=True)
    assert scan.result()["status"] == "pass"
    assert scan.checker is not None


def test_builtin_macos_shared_directory_is_not_a_personal_home():
    shared = fixture_path("macos", "Shared", "neku-live2d")
    assert not ENGINE.hit_local_absolute_path(shared)
    assert ENGINE.scan_private_text(shared, [DENIED])["status"] == "pass"
    raw = archive(xml=f"<r>{shared}</r>".encode())
    assert ENGINE.scan_private_caff(raw, [DENIED])["status"] == "pass"


@pytest.mark.parametrize(
    "flavour,username",
    [
        ("windows", "Shared"),
        ("linux", "Shared"),
        ("macos", "Shared-person"),
        ("macos", "Shared person"),
    ],
)
def test_shared_directory_rule_never_exempts_actual_user_homes(flavour, username):
    value = fixture_path(flavour, username, "work")
    assert ENGINE.hit_local_absolute_path(value)


@pytest.mark.parametrize(
    "xml",
    [
        b'<r><!-- "caff-fixture%40" + "example.invalid" --></r>',
        b'<r><?metadata "caff-fixture%40" + "example.invalid"?></r>',
    ],
)
def test_url_decoded_xml_source_gets_the_full_text_normalizer(xml):
    assert ENGINE.scan_private_caff(archive(xml=xml), [DENIED])["status"] == "fail"


@pytest.mark.parametrize(
    "suffix",
    [
        "/../personal/work.psd",
        "/nested/../../personal/work.psd",
        '/with"quote/../../personal/work.psd',
        "/with\nnewline/../../personal/work.psd",
    ],
)
def test_shared_parent_reference_never_waives_personal_home(suffix):
    from html import escape

    value = ENGINE.MACOS_SHARED_DIRECTORY + suffix
    if not any(c in value for c in ('"', "\n")):
        assert ENGINE.scan_private_text(value, [DENIED])["local_path_match"]
    raw = archive(xml=f"<r>{escape(value)}</r>".encode())
    assert ENGINE.scan_private_caff(raw, [DENIED])["metadata_path_match"]


def test_path_and_name_gates_share_parent_reference_rule():
    value = ENGINE.MACOS_SHARED_DIRECTORY + "/../personal/work.psd"
    spec = importlib.util.spec_from_file_location(
        "caff_shared_readiness", ROOT / "scripts/readiness_scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.personal_path_matches(value.encode())
    assert module.text_has(module.PATH_PATTERNS, value.encode())
    gate_spec = importlib.util.spec_from_file_location(
        "caff_shared_gate", ROOT / "scripts/identity_gate.py"
    )
    gate = importlib.util.module_from_spec(gate_spec)
    sys.modules[gate_spec.name] = gate
    gate_spec.loader.exec_module(gate)
    assert "実ホームパスの形" in gate.shape_findings(value)


def test_shared_reference_is_not_joined_with_unrelated_relative_path():
    shared = ENGINE.MACOS_SHARED_DIRECTORY + "/assets"
    text = shared + "\nSee docs/../README.md for details."
    assert ENGINE.scan_private_text(text, [DENIED])["status"] == "pass"
    raw = archive(xml=f"<r><a>{shared}</a><b>See docs/../README.md</b></r>".encode())
    assert ENGINE.scan_private_caff(raw, [DENIED])["status"] == "pass"


def test_parent_reference_that_remains_inside_shared_is_not_personal():
    value = ENGINE.MACOS_SHARED_DIRECTORY + "/assets/../source.psd"
    assert ENGINE.scan_private_text(value, [DENIED])["status"] == "pass"


@pytest.mark.parametrize("codec", ["utf-16-be", "utf-16-le", "utf-32-be", "utf-32-le"])
def test_unsupported_xml_encoding_never_splits_parser_and_source_views(codec):
    xml = '<?xml version="1.0" encoding="UTF-16"?><r><!-- "caff-fixture" + "@example.invalid" --></r>'.encode(
        codec
    )
    with pytest.raises(ValueError, match="caff_unverified"):
        ENGINE.scan_private_caff(archive(xml=xml), [DENIED])


def test_non_utf8_xml_declaration_is_rejected_even_when_ascii_decodes():
    xml = b'<?xml version="1.0" encoding="ISO-8859-1"?><r>clean</r>'
    with pytest.raises(ValueError, match="caff_unverified"):
        ENGINE.scan_private_caff(archive(xml=xml), [DENIED])
