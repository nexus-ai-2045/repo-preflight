"""PNG containerの局所回帰。個人値・実画像はrepositoryへ保存しない。"""

import importlib.util
from pathlib import Path, PurePosixPath
import struct
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_png_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = load("identity_normalization")
SCANNER = load("readiness_scan")
DENIED = "png-fixture-private@example.invalid"


def chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def png(metadata=b"", pixels=b'("alpha"+"beta";'):
    header = struct.pack(">IIBBBBB", len(pixels), 1, 8, 0, 0, 0, 0)
    return (
        ENGINE.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + metadata
        + chunk(b"IDAT", zlib.compress(b"\0" + pixels, level=0))
        + chunk(b"IEND", b"")
    )


def text_chunk(kind, text, compressed=False):
    if kind == b"tEXt":
        payload = b"Comment\0" + text.encode("latin-1")
    elif kind == b"zTXt":
        payload = b"Comment\0\0" + zlib.compress(text.encode("latin-1"))
    else:
        raw = text.encode("utf-8")
        payload = b"Comment\0" + bytes([int(compressed), 0]) + b"en\0translated\0"
        payload += zlib.compress(raw) if compressed else raw
    return chunk(kind, payload)


def test_png_validated_pixels_are_binary_under_accepted_contract():
    raw = png()
    with pytest.raises(ValueError, match="normalization_incomplete"):
        ENGINE.scan_private_text(raw.decode("utf-8", errors="replace"), [DENIED])
    assert ENGINE.scan_private_png(raw, [DENIED])["status"] == "pass"
    assert (
        ENGINE.scan_private_png(png(pixels=DENIED.encode()), [DENIED])["status"]
        == "pass"
    )


@pytest.mark.parametrize(
    "kind,compressed",
    [(b"tEXt", False), (b"zTXt", True), (b"iTXt", False), (b"iTXt", True)],
)
@pytest.mark.parametrize("reconstructed", [False, True])
def test_png_plain_and_compressed_metadata_preserve_identity_detection(
    kind, compressed, reconstructed
):
    text = '"png-fixture-" + "private@example.invalid"' if reconstructed else DENIED
    result = ENGINE.scan_private_png(png(text_chunk(kind, text, compressed)), [DENIED])
    assert result["status"] == "fail" and result["literal_match_count"] >= 1


def test_png_reconstructed_local_path_and_url_encoding_are_checked():
    # 明示的な合成path。実行hostの個人値を使わない。
    sample = str(PurePosixPath("/").joinpath("Users", "png-fixture", "work"))
    expression = repr(sample[:3]) + " + " + repr(sample[3:])
    result = ENGINE.scan_private_png(png(text_chunk(b"zTXt", expression)), [DENIED])
    assert result["local_path_match"] and result["reconstructed_path_match"]
    encoded = DENIED.replace("@", "%40")
    assert (
        ENGINE.scan_private_png(png(text_chunk(b"tEXt", encoded)), [DENIED])["status"]
        == "fail"
    )


@pytest.mark.parametrize(
    "mutation", ["crc", "truncated", "missing-end", "critical", "trailing"]
)
def test_png_invalid_container_is_not_accepted(mutation):
    raw = png()
    if mutation == "crc":
        raw = raw[:-1] + bytes([raw[-1] ^ 1])
    elif mutation == "truncated":
        raw = raw[:22]
    elif mutation == "missing-end":
        raw = raw[:-12]
    elif mutation == "critical":
        raw = png(chunk(b"ABCD", b"unknown"))
    else:
        raw += b"trailing"
    with pytest.raises(ValueError, match="private_identity_png_unverified"):
        ENGINE.scan_private_png(raw, [DENIED])


@pytest.mark.parametrize(
    "metadata",
    [
        chunk(b"zTXt", b"Comment\0\0invalid-zlib"),
        chunk(b"iTXt", b"Comment\0\2\0en\0translated\0bad"),
        chunk(b"iTXt", b"Comment\0\0\0en\0translated\0\xff"),
    ],
)
def test_png_unreadable_text_is_not_accepted(metadata):
    with pytest.raises(ValueError, match="private_identity_png_unverified"):
        ENGINE.scan_private_png(png(metadata), [DENIED])


def test_png_text_inflation_is_bounded(monkeypatch):
    monkeypatch.setattr(ENGINE, "PNG_TEXT_LIMIT", 100)
    raw = png(text_chunk(b"zTXt", "x" * 1000))
    with pytest.raises(ValueError, match="private_identity_png_text_limit"):
        ENGINE.scan_private_png(raw, [DENIED])


@pytest.mark.parametrize(
    "case,expected", [("clean", "pass"), ("private", "fail"), ("broken", "tool_error")]
)
def test_png_existing_private_scan_uses_canonical_parser_and_fails_closed(
    tmp_path, monkeypatch, case, expected
):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = tmp_path / "private-policy.txt"
    policy.write_text(DENIED + "\n")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    check = SCANNER.PrivateIdentityScan(repo, True)
    raw = png(text_chunk(b"zTXt", DENIED)) if case == "private" else png()
    if case == "broken":
        raw = raw[:-1]
    check.check(raw)
    result = check.result()
    assert result["status"] == expected
    if case == "broken":
        assert result["reason"] == "private_identity_png_unverified"


def test_png_unknown_ancillary_chunk_is_not_a_normalization_exemption():
    expression = 'const value = "png-fixture-" + "private@example.invalid";'
    result = ENGINE.scan_private_png(png(chunk(b"caBX", expression.encode())), [DENIED])
    assert result["status"] == "fail"
    with pytest.raises(ValueError, match="normalization_incomplete"):
        ENGINE.scan_private_png(png(chunk(b"caBX", b'("alpha"+"beta";')), [DENIED])


@pytest.mark.parametrize("has_metadata,expected", [(False, "pass"), (True, "fail")])
def test_png_raw_path_exception_never_waives_compressed_metadata(
    tmp_path, monkeypatch, has_metadata, expected
):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = tmp_path / "private-policy.txt"
    policy.write_text(DENIED + "\n")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    raw_path = str(PurePosixPath("/").joinpath("Users", "png-fixture", "reviewed"))
    other_path = str(
        PurePosixPath("/").joinpath("Users", "png-fixture", "not-reviewed")
    )
    metadata = text_chunk(b"zTXt", other_path) if has_metadata else b""
    check = SCANNER.PrivateIdentityScan(repo, True)
    check.check(png(metadata, pixels=raw_path.encode()), reviewed_raw_paths=True)
    assert check.result()["status"] == expected


@pytest.mark.parametrize(
    "case", ["not-zlib", "wrong-size", "bad-filter", "extra-stream"]
)
def test_png_pixel_exemption_requires_complete_valid_scanlines(case):
    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    if case == "not-zlib":
        payload = b'"png-fixture-" + "private@example.invalid"'
    elif case == "wrong-size":
        payload = zlib.compress(b"\0")
    elif case == "bad-filter":
        payload = zlib.compress(b"\5x")
    else:
        payload = zlib.compress(b"\0x") + zlib.compress(b"trailing")
    raw = (
        ENGINE.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", payload)
        + chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="private_identity_png_unverified"):
        ENGINE.scan_private_png(raw, [DENIED])


def test_png_adam7_scanline_layout_is_validated():
    header = struct.pack(">IIBBBBB", 9, 9, 8, 0, 0, 0, 1)
    # 9x9 grayscale8のAdam7 pass別のbyte数: 6,4,4,9,12,25,40。
    pixels = b"\0" * 100
    raw = (
        ENGINE.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )
    assert ENGINE.scan_private_png(raw, [DENIED])["status"] == "pass"


@pytest.mark.parametrize("kind", [b"acTL", b"fcTL", b"fdAT"])
def test_unverified_apng_frames_never_receive_pixel_exemption(kind):
    with pytest.raises(ValueError, match="private_identity_png_animation_unverified"):
        ENGINE.scan_private_png(png(chunk(kind, b"\0" * 26)), [DENIED])


def test_png_image_decompression_is_bounded(monkeypatch):
    monkeypatch.setattr(ENGINE, "PNG_IMAGE_LIMIT", 8)
    with pytest.raises(ValueError, match="private_identity_png_image_limit"):
        ENGINE.scan_private_png(png(pixels=b"x" * 9), [DENIED])


def indexed_png(depth, width, height, entries, scanlines, interlace=0):
    header = struct.pack(">IIBBBBB", width, height, depth, 3, 0, 0, interlace)
    return (
        ENGINE.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"PLTE", bytes(entries * 3))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize(
    "depth,entries,valid,invalid",
    [(1, 1, 127, 128), (2, 3, 191, 192), (4, 3, 47, 48), (8, 3, 2, 3)],
)
def test_indexed_samples_respect_actual_palette_and_ignore_padding(
    depth, entries, valid, invalid
):
    assert (
        ENGINE.scan_private_png(
            indexed_png(depth, 1, 1, entries, bytes([0, valid])), [DENIED]
        )["status"]
        == "pass"
    )
    with pytest.raises(ValueError, match="png_unverified"):
        ENGINE.scan_private_png(
            indexed_png(depth, 1, 1, entries, bytes([0, invalid])), [DENIED]
        )


@pytest.mark.parametrize(
    "filter_kind,valid,invalid",
    [
        (0, (1, 0), (2, 0)),
        (1, (1, 255), (2, 254)),
        (2, (0, 0), (1, 0)),
        (3, (1, 0), (2, 255)),
        (4, (0, 0), (1, 255)),
    ],
)
def test_indexed_palette_validation_unfilters_all_five_filters(
    filter_kind, valid, invalid
):
    first_row = bytes([0, 1, 0])
    assert (
        ENGINE.scan_private_png(
            indexed_png(8, 2, 2, 2, first_row + bytes([filter_kind, *valid])), [DENIED]
        )["status"]
        == "pass"
    )
    with pytest.raises(ValueError, match="png_unverified"):
        ENGINE.scan_private_png(
            indexed_png(8, 2, 2, 2, first_row + bytes([filter_kind, *invalid])),
            [DENIED],
        )


def test_indexed_palette_validation_resets_previous_row_per_adam7_pass():
    assert (
        ENGINE.scan_private_png(
            indexed_png(8, 1, 2, 2, bytes([0, 1, 2, 1]), 1), [DENIED]
        )["status"]
        == "pass"
    )
    with pytest.raises(ValueError, match="png_unverified"):
        ENGINE.scan_private_png(
            indexed_png(8, 1, 2, 2, bytes([0, 1, 2, 2]), 1), [DENIED]
        )


def test_invalid_indexed_pixels_cannot_hide_a_reconstructed_identity():
    expression = b'"png-fixture-" + "private@example.invalid"'
    raw = indexed_png(8, len(expression), 1, 1, b"\0" + expression)
    assert DENIED.encode() not in raw
    with pytest.raises(ValueError, match="png_unverified"):
        ENGINE.scan_private_png(raw, [DENIED])


@pytest.mark.parametrize(
    "payload", [DENIED.encode(), b'"png-fixture-" + "private@example.invalid"']
)
def test_png_unknown_ancillary_metadata_is_not_exempted(payload):
    result = ENGINE.scan_private_png(png(chunk(b"vpAg", payload)), [DENIED])
    assert result["status"] == "fail"


def test_png_private_literal_in_trailer_never_receives_binary_exemption():
    with pytest.raises(ValueError, match="png_unverified"):
        ENGINE.scan_private_png(png() + DENIED.encode(), [DENIED])


def test_png_literal_in_pixels_does_not_bypass_bad_crc_or_scanline_validation():
    raw = bytearray(png(pixels=DENIED.encode()))
    raw[-1] ^= 1
    with pytest.raises(ValueError, match="png_unverified"):
        ENGINE.scan_private_png(bytes(raw), [DENIED])


def test_canonical_inventory_name_context_does_not_change_body_path_checks(
    tmp_path, monkeypatch
):
    policy = tmp_path / "policy.txt"
    policy.write_text(DENIED)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    from path_input_fixture import fixture_relative

    name = fixture_relative("web", "app", "(group)", "home", "page.tsx").encode()
    named = SCANNER.PrivateIdentityScan(repo, True)
    named.check(name, repository_name=True)
    assert named.result()["status"] == "pass"
    body = SCANNER.PrivateIdentityScan(repo, True)
    body.check(name)
    assert body.result()["status"] == "fail"


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("prefix", [b"", b"x"])
def test_png_metadata_retains_utf16_both_byte_alignments(codec, prefix):
    payload = prefix + DENIED.encode(codec)
    result = ENGINE.scan_private_png(png(chunk(b"vpAg", payload)), [DENIED])
    assert result["status"] == "fail"
