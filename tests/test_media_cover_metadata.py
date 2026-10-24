"""Synthetic cover metadata crosses the real media and credential boundaries."""

import io
import struct

import pytest
from PIL import Image
from mutagen.id3 import APIC, ID3

from test_media_identity import make_av, requires_decoder
from test_readiness_media import DENIED, setup_scan
from test_readiness_scan import MODULE, git
from test_png_identity import ENGINE, png, chunk

SECRET = "ghp_" + "B" * 24


def icc_profile(text):
    from PIL import ImageCms

    profile = bytearray(
        ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    )
    value = text.encode("utf-16-be")
    tag = (
        b"mluc"
        + b"\0" * 4
        + struct.pack(">II", 1, 12)
        + b"enUS"
        + struct.pack(">II", len(value), 28)
        + value
    )
    for index in range(struct.unpack_from(">I", profile, 128)[0]):
        entry = 132 + 12 * index
        if profile[entry : entry + 4] == b"cprt":
            profile.extend(b"\0" * (-len(profile) % 4))
            struct.pack_into(">II", profile, entry + 4, len(profile), len(tag))
            profile.extend(tag)
            struct.pack_into(">I", profile, 0, len(profile))
            break
    else:
        raise AssertionError("synthetic sRGB profile has no copyright tag")
    # Existing ICC decoder verifies the fixture; production gets no ICC parser.
    ImageCms.ImageCmsProfile(io.BytesIO(profile))
    return bytes(profile)


def cover(kind, text):
    output = io.BytesIO()
    image = Image.new("RGB", (4, 4), (12, 34, 56))
    if kind == "jpeg-icc-split":
        image.save(output, format="JPEG")
        raw = output.getvalue()
        profile = icc_profile(text)
        split = profile.index(text.encode("utf-16-be")) + 10
        blocks = []
        for number, part in enumerate((profile[:split], profile[split:]), 1):
            payload = b"ICC_PROFILE\0" + bytes((number, 2)) + part
            blocks.append(b"\xff\xe2" + struct.pack(">H", len(payload) + 2) + payload)
        return raw[:2] + b"".join(blocks) + raw[2:]
    if kind in {"png-icc", "jpeg-icc"}:
        image.save(
            output,
            format="JPEG" if kind.startswith("jpeg") else "PNG",
            icc_profile=icc_profile(text),
        )
        return output.getvalue()
    if kind.startswith("jpeg-xmp"):
        image.save(output, format="JPEG")
        raw = output.getvalue()
        header = (
            b"http://ns.adobe.com/xmp/extension/\0"
            if kind.endswith("extended")
            else b"http://ns.adobe.com/xap/1.0/\0"
        )
        xml = ("<x>" + "".join(f"&#{ord(c)};" for c in text) + "</x>").encode()
        payload = header + xml
        return (
            raw[:2]
            + b"\xff\xe1"
            + struct.pack(">H", len(payload) + 2)
            + payload
            + raw[2:]
        )
    if kind == "jpeg":
        image.save(output, format="JPEG")
        raw = output.getvalue()
        payload = text.encode("utf-16-le")
        return (
            raw[:2]
            + b"\xff\xfe"
            + struct.pack(">H", len(payload) + 2)
            + payload
            + raw[2:]
        )
    from PIL.PngImagePlugin import PngInfo

    metadata = PngInfo()
    if kind == "png-xmp":
        metadata.add_itxt(
            "XML:com.adobe.xmp",
            "<x>" + "".join(f"&#{ord(c)};" for c in text) + "</x>",
            zip=True,
        )
    elif kind == "png-ztxt":
        metadata.add_text("Comment", text, zip=True)
    else:
        metadata.add_itxt("Comment", text, zip=True)
    image.save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def audio_with_cover(tmp_path, kind, text):
    path = make_av(tmp_path, "mp3")
    tags = ID3(path)
    tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg" if kind.startswith("jpeg") else "image/png",
            type=3,
            desc="Public cover",
            data=cover(kind, text),
        )
    )
    tags.save(path)
    data = path.read_bytes()
    path.unlink()
    return data


@requires_decoder
@pytest.mark.parametrize("kind", ["png-ztxt", "png-itxt", "jpeg", "jpeg-icc"])
def test_actual_cover_callback_counts_decoded_secrets(tmp_path, monkeypatch, kind):
    repo = setup_scan(tmp_path, monkeypatch)
    scanner = MODULE.PrivateIdentityScan(repo, True)
    candidate = audio_with_cover(tmp_path, kind, SECRET)
    assert SECRET.encode() not in candidate
    scanner.check(candidate, source_names=("audio.mp3",))
    assert scanner.report["status"] == "pass"
    assert scanner.last_media_secret_matches >= 1


@requires_decoder
@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("kind", ["png-ztxt", "jpeg-icc"])
def test_compressed_cover_secret_reaches_worktree_and_history(
    tmp_path, monkeypatch, historical, kind
):
    repo = setup_scan(tmp_path, monkeypatch)
    target = repo / "audio.mp3"
    target.write_bytes(audio_with_cover(tmp_path, kind, SECRET))
    if historical:
        git(repo, "add", "audio.mp3")
        git(repo, "commit", "-m", "Synthetic compressed cover")
        target.unlink()
        git(repo, "add", "-u")
        git(repo, "commit", "-m", "Remove synthetic audio")
    checks = MODULE.scan(repo, require_private_identity=True)["checks"]
    assert checks["private_identity_scan"]["status"] == "pass"
    assert checks["secret_scan"]["status"] == "fail"
    assert checks["secret_scan"]["finding_count"] >= 1


@requires_decoder
@pytest.mark.parametrize("kind", ["png-ztxt", "png-itxt", "jpeg", "jpeg-icc"])
def test_cover_clean_and_private_identity_controls(tmp_path, monkeypatch, kind):
    repo = setup_scan(tmp_path, monkeypatch)
    scanner = MODULE.PrivateIdentityScan(repo, True)
    scanner.check(
        audio_with_cover(tmp_path, kind, "Public cover metadata"),
        source_names=("audio.mp3",),
    )
    assert scanner.report["status"] == "pass"
    assert scanner.last_media_secret_matches == 0
    scanner.check(audio_with_cover(tmp_path, kind, DENIED), source_names=("audio.mp3",))
    assert scanner.findings >= 1


@requires_decoder
@pytest.mark.parametrize("capability", ["legacy", "ignored"])
def test_image_provider_without_observer_capability_cannot_certify_cover(
    tmp_path, monkeypatch, capability
):
    repo = setup_scan(tmp_path, monkeypatch)
    scanner = MODULE.PrivateIdentityScan(repo, True)
    original = scanner.png_checker
    scanner.png_checker = (
        (lambda value, patterns: original(value, patterns))
        if capability == "legacy"
        else (lambda value, patterns, **kwargs: original(value, patterns))
    )
    scanner.check(
        audio_with_cover(tmp_path, "png-ztxt", SECRET), source_names=("audio.mp3",)
    )
    assert scanner.report["status"] == "tool_error"


def test_png_pixel_codewords_never_enter_metadata_observer():
    observed = []
    candidate = png(pixels=SECRET.encode())
    assert ENGINE.scan_private_png(candidate, [DENIED])["status"] == "pass"
    result = ENGINE.scan_private_png(
        candidate, [DENIED], metadata_observer=observed.append
    )
    assert result["status"] == "pass"
    assert result["metadata_observer_complete"] is True
    assert observed == []


@pytest.mark.parametrize("kind", ["png-ztxt", "jpeg"])
def test_image_observer_failure_cannot_return_a_clean_pass(kind):
    checker = ENGINE.scan_private_jpeg if kind == "jpeg" else ENGINE.scan_private_png

    def failure(value):
        raise ValueError("synthetic observer failure")

    with pytest.raises(ValueError):
        checker(cover(kind, "Public metadata"), [DENIED], metadata_observer=failure)


@requires_decoder
@pytest.mark.parametrize(
    "kind", ["png-icc", "png-xmp", "jpeg-xmp", "jpeg-xmp-extended", "jpeg-icc-split"]
)
@pytest.mark.parametrize("text", ["Public metadata", SECRET])
def test_unverified_icc_or_declared_xmp_cover_is_refused(
    tmp_path, monkeypatch, kind, text
):
    repo = setup_scan(tmp_path, monkeypatch)
    candidate = audio_with_cover(tmp_path, kind, text)
    assert text.encode() not in candidate
    scanner = MODULE.PrivateIdentityScan(repo, True)
    scanner.check(candidate, source_names=("audio.mp3",))
    assert scanner.report["status"] == "tool_error"


@pytest.mark.parametrize(
    "kind", ["png-icc", "png-xmp", "jpeg-xmp", "jpeg-xmp-extended"]
)
def test_unsupported_observer_metadata_preserves_two_argument_api(kind):
    candidate = cover(kind, SECRET)
    checker = (
        ENGINE.scan_private_jpeg if kind.startswith("jpeg") else ENGINE.scan_private_png
    )
    assert checker(candidate, [DENIED])["status"] == "pass"
    with pytest.raises(ValueError):
        checker(candidate, [DENIED], metadata_observer=lambda value: None)


@pytest.mark.parametrize("payload", [b"", b"profile\0\0broken", b"profile\0\0x\x9c"])
def test_malformed_or_truncated_iccp_never_certifies_observer(payload):
    candidate = png(metadata=chunk(b"iCCP", payload))
    with pytest.raises(ValueError):
        ENGINE.scan_private_png(
            candidate, [DENIED], metadata_observer=lambda value: None
        )


def test_truncated_jpeg_icc_never_certifies_observer():
    candidate = cover("jpeg-icc", SECRET)
    with pytest.raises(ValueError):
        ENGINE.scan_private_jpeg(
            candidate[:-4], [DENIED], metadata_observer=lambda value: None
        )


@pytest.mark.parametrize("header", [b"", b"\0\0", b"\1\2", b"\2\1"])
def test_jpeg_icc_incomplete_segment_framing_is_refused(header):
    raw = cover("jpeg", "Public metadata")
    payload = b"ICC_PROFILE\0" + header
    candidate = (
        raw[:2] + b"\xff\xe2" + struct.pack(">H", len(payload) + 2) + payload + raw[2:]
    )
    with pytest.raises(ValueError):
        ENGINE.scan_private_jpeg(
            candidate, [DENIED], metadata_observer=lambda value: None
        )


def test_duplicate_single_segment_icc_is_not_certified():
    candidate = cover("jpeg-icc", SECRET)
    first = candidate.index(b"ICC_PROFILE\0") - 4
    length = struct.unpack_from(">H", candidate, first + 2)[0] + 2
    changed = candidate[:first] + candidate[first : first + length] + candidate[first:]
    with pytest.raises(ValueError):
        ENGINE.scan_private_jpeg(
            changed, [DENIED], metadata_observer=lambda value: None
        )
