"""JPEGの画素とmetadataを分離する局所回帰。合成画像のみ使用する。"""

import importlib.util
import io
from pathlib import Path, PurePosixPath
import struct
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_jpeg_test", ROOT / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = load("identity_normalization")
SCANNER = load("readiness_scan")
DENIED = "jpeg-fixture-private@example.invalid"


def jpeg(progressive=False):
    output = io.BytesIO()
    Image.new("RGB", (4, 4), (12, 34, 56)).save(
        output, format="JPEG", progressive=progressive
    )
    return output.getvalue()


def comment(raw, text, at_end=False):
    marker = b"\xff\xfe" + struct.pack(">H", len(text) + 2) + text
    return raw[:-2] + marker + raw[-2:] if at_end else raw[:2] + marker + raw[2:]


@pytest.mark.parametrize("progressive", [False, True])
def test_valid_jpeg_decode_including_progressive_is_checked(progressive):
    assert ENGINE.scan_private_jpeg(jpeg(progressive), [DENIED])["status"] == "pass"


@pytest.mark.parametrize(
    "payload",
    [
        DENIED.encode(),
        b'"jpeg-fixture-" + "private@example.invalid"',
        DENIED.replace("@", "%40").encode(),
        DENIED.encode("utf-16-le"),
        b"x" + DENIED.encode("utf-16-be"),
    ],
)
@pytest.mark.parametrize("at_end", [False, True])
def test_jpeg_metadata_identity_in_all_scan_positions_is_not_exempted(payload, at_end):
    assert (
        ENGINE.scan_private_jpeg(comment(jpeg(True), payload, at_end), [DENIED])[
            "status"
        ]
        == "fail"
    )


def test_jpeg_metadata_path_is_not_exempted_by_reviewed_raw_path(tmp_path, monkeypatch):
    policy = tmp_path / "policy.txt"
    policy.write_text(DENIED)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    scanner = SCANNER.PrivateIdentityScan(repo, True)
    path = str(PurePosixPath("/").joinpath("Users", "jpeg-fixture", "unreviewed"))
    scanner.check(comment(jpeg(), repr(path).encode()), reviewed_raw_paths=True)
    assert scanner.result()["status"] == "fail"


@pytest.mark.parametrize(
    "mutation", ["truncated", "trailing", "bad-length", "no-pixels", "entropy"]
)
def test_jpeg_invalid_structure_and_decode_never_receive_pixel_exemption(mutation):
    raw = jpeg()
    if mutation == "truncated":
        raw = raw[:-3]
    elif mutation == "trailing":
        raw += b"unverified"
    elif mutation == "bad-length":
        raw = raw[:4] + b"\xff\xff" + raw[6:]
    elif mutation == "no-pixels":
        raw = b"\xff\xd8\xff\xd9"
    else:
        start = raw.index(b"\xff\xda")
        size = int.from_bytes(raw[start + 2 : start + 4], "big")
        raw = raw[: start + size + 2] + b"invalid entropy" + raw[-2:]
    with pytest.raises(ValueError, match="jpeg_unverified"):
        ENGINE.scan_private_jpeg(raw, [DENIED])


@pytest.mark.parametrize(
    "returncode,stdout,stderr,code",
    [
        (86, b"", b"", "decoder_unavailable"),
        (0, b"", b"native decoder warning", "unverified"),
        (0, b"unexpected", b"", "unverified"),
        (87, b"", b"", "unverified"),
    ],
)
def test_jpeg_decoder_failure_and_native_warnings_fail_closed(
    monkeypatch, returncode, stdout, stderr, code
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        ),
    )
    with pytest.raises(ValueError, match="jpeg_" + code):
        ENGINE.scan_private_jpeg(jpeg(), [DENIED])


def test_jpeg_decoder_timeout_fails_closed(monkeypatch):
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("decoder", 15)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ValueError, match="jpeg_unverified"):
        ENGINE.scan_private_jpeg(jpeg(), [DENIED])


@pytest.mark.parametrize(
    "limit,code",
    [("JPEG_DATA_LIMIT", "image_limit"), ("JPEG_METADATA_LIMIT", "text_limit")],
)
def test_jpeg_processing_is_bounded(monkeypatch, limit, code):
    monkeypatch.setattr(ENGINE, limit, 1)
    with pytest.raises(ValueError, match="jpeg_" + code):
        ENGINE.scan_private_jpeg(jpeg(), [DENIED])


def test_jpeg_canonical_scanner_reads_valid_data_without_source_parsing(
    tmp_path, monkeypatch
):
    policy = tmp_path / "policy.txt"
    policy.write_text(DENIED)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    scanner = SCANNER.PrivateIdentityScan(repo, True)
    scanner.check(jpeg())
    assert scanner.result()["status"] == "pass"
    assert scanner.result()["objects_scanned"] == 1


def test_jpeg_missing_decoder_is_reported_as_unverified(tmp_path, monkeypatch):
    policy = tmp_path / "policy.txt"
    policy.write_text(DENIED)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    scanner = SCANNER.PrivateIdentityScan(repo, True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=86, stdout=b"", stderr=b""),
    )
    scanner.check(jpeg())
    result = scanner.result()
    assert result["status"] == "tool_error"
    assert result["reason"] == "private_identity_jpeg_decoder_unavailable"


def test_jpeg_decoder_does_not_inherit_caller_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "environment-only-synthetic")
    monkeypatch.setenv("PRIVATE_IMAGE_SENTINEL", "environment-only-synthetic")

    def checked_run(*args, **kwargs):
        assert "GH_TOKEN" not in kwargs["env"]
        assert "PRIVATE_IMAGE_SENTINEL" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", checked_run)
    assert ENGINE.scan_private_jpeg(jpeg(), [DENIED])["status"] == "pass"


def test_truncated_jpeg_signature_does_not_fall_through_as_text(tmp_path, monkeypatch):
    policy = tmp_path / "policy.txt"
    policy.write_text(DENIED)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    scanner = SCANNER.PrivateIdentityScan(repo, True)
    scanner.check(b"\xff\xd8")
    assert scanner.result()["status"] == "tool_error"
    assert scanner.result()["reason"] == "private_identity_jpeg_unverified"


def replace_first_segment(raw, marker, payload):
    offset = raw.index(b"\xff" + bytes([marker]))
    length = int.from_bytes(raw[offset + 2 : offset + 4], "big")
    return (
        raw[: offset + 2]
        + struct.pack(">H", len(payload) + 2)
        + payload
        + raw[offset + length + 2 :]
    )


@pytest.mark.parametrize(
    "text,expected", [(b'"a"+"b"/*', "pass"), (DENIED.encode(), "fail")]
)
def test_jpeg_valid_coding_table_is_not_source_but_raw_identity_stays_checked(
    text, expected
):
    quantization = b"\0" + text + bytes([1]) * (64 - len(text))
    raw = replace_first_segment(jpeg(), 0xDB, quantization)
    assert ENGINE.scan_private_jpeg(raw, [DENIED])["status"] == expected


@pytest.mark.parametrize(
    "text,expected", [(b'"a"+"b"/*', "pass"), (DENIED.encode(), "fail")]
)
def test_jpeg_binary_app_metadata_is_not_source_but_raw_identity_stays_checked(
    text, expected
):
    payload = b"Exif\0\0" + text
    marker = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    raw = jpeg()
    assert (
        ENGINE.scan_private_jpeg(raw[:2] + marker + raw[2:], [DENIED])["status"]
        == expected
    )


def test_jpeg_xmp_text_keeps_source_reconstruction():
    body = b'<x:xmpmeta>"jpeg-fixture-" + "private@example.invalid"</x:xmpmeta>'
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + body
    marker = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    raw = jpeg()
    assert (
        ENGINE.scan_private_jpeg(raw[:2] + marker + raw[2:], [DENIED])["status"]
        == "fail"
    )


@pytest.mark.parametrize(
    "text,expected", [(b'"a"+"b"/*', "pass"), (DENIED.encode(), "fail")]
)
def test_jpeg_jfif_thumbnail_is_validated_as_pixels_and_raw_identity_is_checked(
    text, expected
):
    width = (len(text) + 2) // 3
    pixels = text.ljust(width * 3, b"\0")
    payload = b"JFIF\0\x01\x01\0\0\x01\0\x01" + bytes([width, 1]) + pixels
    raw = replace_first_segment(jpeg(), 0xE0, payload)
    assert ENGINE.scan_private_jpeg(raw, [DENIED])["status"] == expected


def test_jpeg_jfif_extra_data_cannot_receive_a_binary_exemption():
    payload = b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0unverified"
    with pytest.raises(ValueError, match="jpeg_unverified"):
        ENGINE.scan_private_jpeg(replace_first_segment(jpeg(), 0xE0, payload), [DENIED])


@pytest.mark.parametrize(
    "flags,pattern,expected", [(b'"a"+ ', DENIED, "pass"), (b"\0zzz", "zzz", "fail")]
)
def test_jpeg_adobe_numeric_fields_are_not_source_but_keep_raw_matching(
    flags, pattern, expected
):
    flags = flags[:4]
    payload = b"Adobe\0d" + flags + b"\x01"
    marker = b"\xff\xee" + struct.pack(">H", len(payload) + 2) + payload
    raw = jpeg()
    assert (
        ENGINE.scan_private_jpeg(raw[:2] + marker + raw[2:], [pattern])["status"]
        == expected
    )
