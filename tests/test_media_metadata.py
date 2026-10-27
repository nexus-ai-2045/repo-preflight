"""Regression coverage for bounded media metadata reading."""

import hashlib
import io
import os
import struct
import zlib

import pytest
from PIL import Image, PngImagePlugin

from scripts import media_metadata as reader


def png_bytes(marker=b"metadata-marker"):
    image = Image.new("RGB", (2, 2), "white")
    info = PngImagePlugin.PngInfo()
    info.add_itxt("Comment", marker.decode())
    output = io.BytesIO()
    image.save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def png_chunk(kind, data):
    body = kind + data
    return (
        struct.pack(">I", len(data))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )


def mp4_bytes(extra_boxes=b""):
    return struct.pack(">I4s", 16, b"ftyp") + b"isom0000" + extra_boxes


def video_probe_json():
    return b'{"format":{},"streams":[{"codec_type":"video","disposition":{"attached_pic":0}}],"chapters":[]}'


def jpeg_with_middle_exif_user_comment(user_comment, nested=False):
    image = Image.new("RGB", (2, 2), "white")
    base_output = io.BytesIO()
    image.save(base_output, format="JPEG")
    base = base_output.getvalue()

    profile_part_size = 32_750
    profile = bytearray(profile_part_size * 6)
    profile[:4] = len(profile).to_bytes(4, "big")
    profile[8:12] = b"\x04\x30\x00\x00"
    profile[12:16] = b"mntr"
    profile[16:20] = b"RGB "
    profile[20:24] = b"XYZ "
    profile[36:40] = b"acsp"
    profile[128:132] = b"\x00\x00\x00\x00"

    def app2(sequence):
        start = (sequence - 1) * profile_part_size
        data = (
            b"ICC_PROFILE\x00"
            + bytes((sequence, 6))
            + bytes(profile[start : start + profile_part_size])
        )
        return b"\xff\xe2" + (len(data) + 2).to_bytes(2, "big") + data

    exif = Image.Exif()
    if nested:
        exif[34665] = {reader.EXIF_USER_COMMENT_TAG: user_comment}
    else:
        exif[reader.EXIF_USER_COMMENT_TAG] = user_comment
    exif_data = exif.tobytes()
    app1 = b"\xff\xe1" + (len(exif_data) + 2).to_bytes(2, "big") + exif_data
    middle = (
        b"".join(app2(i) for i in range(1, 4))
        + app1
        + b"".join(app2(i) for i in range(4, 7))
    )
    return base[:2] + middle + base[2:]


def test_png_hash_metadata_and_lazy_pixel_access(tmp_path, monkeypatch):
    path = tmp_path / "sample.png"
    raw = png_bytes()
    path.write_bytes(raw)

    def forbidden(*_args, **_kwargs):
        pytest.fail("pixel decoding is outside metadata-only inspection")

    monkeypatch.setattr(Image.Image, "load", forbidden)
    result = reader.inspect_media_metadata(path, ("source.png",))
    assert result["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["metadata_views"] and any(
        b"metadata-marker" in x for x in result["metadata_views"]
    )
    assert result["payload_inspected"] is False


@pytest.mark.parametrize(
    "chunk_type,data",
    [
        (b"iTXt", b"Comment\x00\x00\x00\x00\x00trailing-marker"),
        (b"tEXt", b"Comment\x00trailing-marker"),
        (b"zTXt", b"Comment\x00\x00\x00"),
        (b"eXIf", b"trailing-marker"),
    ],
)
def test_png_metadata_after_idat_is_rejected_without_pixel_decode(
    tmp_path, monkeypatch, chunk_type, data
):
    raw = png_bytes()
    iend = png_chunk(b"IEND", b"")
    assert raw.endswith(iend)
    path = tmp_path / "trailing.png"
    path.write_bytes(raw[: -len(iend)] + png_chunk(chunk_type, data) + iend)
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("pixel decode")
    )
    with pytest.raises(reader.MediaMetadataError, match="^metadata_after_image_data$"):
        reader.inspect_media_metadata(path, ())


def test_middle_payload_marker_is_not_claimed_as_text_scan(tmp_path):
    path = tmp_path / "sample.mp4"
    marker = b"middle-payload-marker"
    payload = bytearray(reader.SAMPLE_SIZE * 3)
    payload[reader.SAMPLE_SIZE + 8 : reader.SAMPLE_SIZE + 8 + len(marker)] = marker
    raw = struct.pack(">I4s", 16, b"ftyp") + b"isom0000"
    raw += struct.pack(">I4s", 8 + len(payload), b"mdat") + payload
    path.write_bytes(raw)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(reader, "_mp4_metadata", lambda _path, _runner: ())
    try:
        result = reader.inspect_media_metadata(path, ())
    finally:
        monkeypatch.undo()
    assert marker not in result["head_sample"]
    assert marker not in result["tail_sample"]
    assert marker not in b"".join(result["metadata_views"])
    assert result["payload_inspected"] is False


@pytest.mark.parametrize("box_type", [b"free", b"uuid"])
def test_intermediate_non_mdat_box_marker_is_in_metadata_views(
    tmp_path, monkeypatch, box_type
):
    path = tmp_path / "container.mp4"
    marker = b"intermediate-container-marker"
    payload = bytearray(200_000)
    marker_offset = 100_000
    payload[marker_offset : marker_offset + len(marker)] = marker
    non_mdat = struct.pack(">I4s", 8 + len(payload), box_type) + payload
    mdat_payload = b"x" * 200_000
    raw = mp4_bytes(
        non_mdat + struct.pack(">I4s", 8 + len(mdat_payload), b"mdat") + mdat_payload
    )
    path.write_bytes(raw)
    monkeypatch.setattr(reader, "_mp4_metadata", lambda _path, _runner: ())
    result = reader.inspect_media_metadata(path, ())
    assert marker not in result["head_sample"]
    assert marker not in result["tail_sample"]
    assert any(marker in item for item in result["metadata_views"])


def test_non_mdat_container_metadata_total_is_bounded(tmp_path):
    path = tmp_path / "oversized-container-metadata.mp4"
    oversized = b"x" * reader.TEXT_LIMIT
    path.write_bytes(
        mp4_bytes(struct.pack(">I4s", 8 + len(oversized), b"free") + oversized)
    )
    with pytest.raises(reader.MediaMetadataError, match="^metadata_limit$"):
        reader.inspect_media_metadata(path, ())


def test_magic_mismatch_and_extension_rejected(tmp_path):
    bad = tmp_path / "wrong.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(reader.MediaMetadataError, match="^format_mismatch$"):
        reader.inspect_media_metadata(bad, ())
    with pytest.raises(reader.MediaMetadataError, match="^unsupported_extension$"):
        reader.inspect_media_metadata(tmp_path / "x.gif", ())


def test_file_change_detected_between_reads(tmp_path, monkeypatch):
    path = tmp_path / "sample.png"
    path.write_bytes(png_bytes())
    original = reader._snapshot
    calls = [0]

    def changing_snapshot(target):
        calls[0] += 1
        snapshot = original(target)
        if calls[0] == 2:
            return snapshot[:-1] + (snapshot[-1] + 1,)
        return snapshot

    monkeypatch.setattr(reader, "_snapshot", changing_snapshot)
    with pytest.raises(reader.MediaMetadataError, match="^input_changed$"):
        reader.inspect_media_metadata(path, ())


def test_same_size_metadata_only_change_is_caught_by_final_hash(tmp_path, monkeypatch):
    path = tmp_path / "sample.png"
    raw = png_bytes(b"same-size-marker")
    path.write_bytes(raw)
    stamp = path.stat().st_mtime_ns
    real_snapshot = reader._snapshot
    snapshot = real_snapshot(path)
    monkeypatch.setattr(reader, "_snapshot", lambda _path: snapshot)
    original_metadata = reader._image_metadata

    def mutate_after_metadata(target, ext, xml_reader):
        result = original_metadata(target, ext, xml_reader)
        changed = bytearray(target.read_bytes())
        marker_at = changed.index(b"same-size-marker")
        changed[marker_at] ^= 1
        target.write_bytes(changed)
        os.utime(target, ns=(stamp, stamp))
        return result

    monkeypatch.setattr(reader, "_image_metadata", mutate_after_metadata)
    with pytest.raises(reader.MediaMetadataError, match="^input_changed$"):
        reader.inspect_media_metadata(path, ())


def test_ffprobe_metadata_scrubs_filename_and_uses_bounded_metadata(
    tmp_path, monkeypatch
):
    path = tmp_path / "sample.mp4"
    path.write_bytes(
        struct.pack(">I4s", 16, b"ftyp") + b"isom0000" + struct.pack(">I4s", 8, b"free")
    )
    observed = {}

    def run(args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return (
            0,
            b'{"format":{"filename":"private-path","tags":{"title":"sample-marker"}},"streams":[{"codec_type":"video","disposition":{"attached_pic":0}}],"chapters":[]}',
            b"",
        )

    monkeypatch.setattr(
        reader.shutil, "which", lambda name: "/ffprobe" if name == "ffprobe" else None
    )
    result = reader.inspect_media_metadata(path, (), probe_runner=run)
    assert any(b"sample-marker" in value for value in result["metadata_views"])
    assert not any(b"private-path" in value for value in result["metadata_views"])
    assert (
        "-enable_drefs" in observed["args"] and "-use_absolute_path" in observed["args"]
    )
    assert "-protocol_whitelist" in observed["args"]
    assert observed["kwargs"]["limit"] == reader.TEXT_LIMIT


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("missing", "dependency_unavailable"),
        ("fail", "probe_failed"),
        ("large", "output_limit"),
    ],
)
def test_ffprobe_missing_failure_and_output_limit_are_errors(
    tmp_path, monkeypatch, fault, expected
):
    path = tmp_path / "sample.mp4"
    path.write_bytes(
        struct.pack(">I4s", 16, b"ftyp") + b"isom0000" + struct.pack(">I4s", 8, b"free")
    )
    monkeypatch.setattr(
        reader.shutil, "which", lambda _name: None if fault == "missing" else "/ffprobe"
    )

    def run(*_args, **_kwargs):
        if fault == "large":
            raise RuntimeError("output_limit")
        return 1, b"", b"failure details"

    if fault == "missing":
        monkeypatch.setattr(reader.shutil, "which", lambda _name: None)
    with pytest.raises(reader.MediaMetadataError, match=f"^{expected}$"):
        reader.inspect_media_metadata(path, (), probe_runner=run)


@pytest.mark.parametrize(
    "response,error,expected",
    [
        (b'{"streams":[],"format":{}}', b"", "format_invalid"),
        (b'{"streams":[{"codec_type":"audio"}],"format":{}}', b"", "format_invalid"),
        (video_probe_json(), b"warning", "probe_failed"),
    ],
)
def test_ffprobe_requires_video_stream_and_empty_stderr(
    tmp_path, monkeypatch, response, error, expected
):
    path = tmp_path / "sample.mp4"
    path.write_bytes(mp4_bytes(struct.pack(">I4s", 8, b"free")))
    monkeypatch.setattr(reader.shutil, "which", lambda _name: "/ffprobe")
    with pytest.raises(reader.MediaMetadataError, match=f"^{expected}$"):
        reader.inspect_media_metadata(
            path, (), probe_runner=lambda *_a, **_k: (0, response, error)
        )


def test_mp4_box_count_is_bounded(tmp_path):
    path = tmp_path / "many-boxes.mp4"
    path.write_bytes(mp4_bytes(struct.pack(">I4s", 8, b"free") * reader.NODE_LIMIT))
    with pytest.raises(reader.MediaMetadataError, match="^metadata_limit$"):
        reader.inspect_media_metadata(path, ())


def test_source_name_extensions_must_match_media_family(tmp_path):
    path = tmp_path / "sample.jpeg"
    image = Image.new("RGB", (2, 2), "white")
    image.save(path, format="JPEG")
    assert reader.inspect_media_metadata(path, ("alias.jpg",))["kind"] == "image"
    with pytest.raises(reader.MediaMetadataError, match="^source_name_mismatch$"):
        reader.inspect_media_metadata(path, ("alias.png",))


def test_metadata_views_stop_at_text_limit_without_consuming_rest(monkeypatch):
    consumed = []

    def values():
        yield b"x" * (reader.TEXT_LIMIT + 1)
        consumed.append(True)
        yield b"must-not-be-consumed"

    monkeypatch.setattr(reader, "_flatten", lambda _value: values())
    with pytest.raises(reader.MediaMetadataError, match="^metadata_limit$"):
        reader._views(None)
    assert consumed == []


def test_jpeg_metadata_is_read_without_loading_pixels(tmp_path, monkeypatch):
    image = Image.new("RGB", (2, 2), "white")
    output = io.BytesIO()
    image.save(output, format="JPEG", comment=b"jpeg-marker")
    path = tmp_path / "sample.jpg"
    raw = output.getvalue()
    path.write_bytes(raw)
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("unexpected pixel load")
    )
    result = reader.inspect_media_metadata(path, ())
    assert result["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["kind"] == "image"


@pytest.mark.parametrize(
    "user_comment",
    [
        b"ASCII\x00\x00\x00dummy-secret-ascii",
        b"UNICODE\x00" + "dummy-secret-unicode".encode("utf-16-be"),
    ],
)
def test_jpeg_middle_exif_user_comment_is_decoded_without_pixel_load(
    tmp_path, monkeypatch, user_comment
):
    marker = b"dummy-secret"
    raw = jpeg_with_middle_exif_user_comment(user_comment)
    exif_offset = raw.index(b"Exif\x00\x00")
    assert exif_offset > reader.SAMPLE_SIZE
    assert exif_offset < len(raw) - reader.SAMPLE_SIZE
    path = tmp_path / "middle-comment.jpg"
    path.write_bytes(raw)
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("unexpected pixel decode")
    )

    result = reader.inspect_media_metadata(path, ())
    views = b"\n".join(result["metadata_views"])
    assert marker in views
    assert marker not in result["head_sample"]
    assert marker not in result["tail_sample"]


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-16"])
@pytest.mark.parametrize("nested", [False, True])
def test_middle_exif_unicode_byte_orders(tmp_path, encoding, nested):
    marker = "dummy-secret-ending-z"
    raw = jpeg_with_middle_exif_user_comment(
        b"UNICODE\x00" + (marker + "\x00").encode(encoding), nested=nested
    )
    path = tmp_path / "nested-comment.jpg"
    path.write_bytes(raw)
    result = reader.inspect_media_metadata(path, ())
    assert marker.encode() in b"\n".join(result["metadata_views"])
    assert marker.encode() not in result["head_sample"] + result["tail_sample"]


@pytest.mark.parametrize("kind", [b"tEXt", b"zTXt", b"iTXt"])
def test_png_duplicate_text_keywords_cannot_hide_earlier_values(tmp_path, kind):
    if kind == b"tEXt":
        earlier = b"Comment\x00dummy-secret"
    elif kind == b"zTXt":
        earlier = b"Comment\x00\x00" + zlib.compress(b"dummy-secret")
    else:
        earlier = b"Comment\x00\x00\x00\x00\x00dummy-secret"
    info = PngImagePlugin.PngInfo()
    info.add_text("before-padding", "x" * 70000)
    info.add(kind, earlier)
    info.add_text("Comment", "harmless-last-value")
    info.add_text("after-padding", "x" * 70000)
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
    path = tmp_path / "duplicate.png"
    path.write_bytes(output.getvalue())
    with pytest.raises(reader.MediaMetadataError, match="^duplicate_metadata_keyword$"):
        reader.inspect_media_metadata(path, ())


@pytest.mark.parametrize("encoding", ["utf-16-be", "utf-16-le"])
def test_png_nested_unicode_exif_is_scanned_without_pixels(
    tmp_path, monkeypatch, encoding
):
    marker = "dummy-secret-png"
    exif = Image.Exif()
    exif[34665] = {
        reader.EXIF_USER_COMMENT_TAG: b"UNICODE\x00" + marker.encode(encoding)
    }
    info = PngImagePlugin.PngInfo()
    info.add_text("before-padding", "x" * 70000)
    info.add_text("after-padding", "x" * 70000)
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
    path = tmp_path / "exif.png"
    raw = output.getvalue()
    insertion = raw.index(b"after-padding") - 8
    raw = raw[:insertion] + png_chunk(b"eXIf", exif.tobytes()[6:]) + raw[insertion:]
    assert b"eXIf" in raw
    path.write_bytes(raw)
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("unexpected pixel decode")
    )
    result = reader.inspect_media_metadata(path, ())
    assert marker.encode() in b"\n".join(result["metadata_views"])
    assert marker.encode() not in result["head_sample"] + result["tail_sample"]


def test_jpeg_all_comment_occurrences_are_scanned(tmp_path, monkeypatch):
    raw = jpeg_with_middle_exif_user_comment(b"ASCII\x00\x00\x00harmless")
    offset = raw.index(b"Exif\x00\x00") - 4

    def comment(value):
        return b"\xff\xfe" + (len(value) + 2).to_bytes(2, "big") + value

    marker = b"dummy-secret-earlier-comment"
    raw = raw[:offset] + comment(marker) + comment(b"harmless-last") + raw[offset:]
    path = tmp_path / "comments.jpg"
    path.write_bytes(raw)
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("pixel decode")
    )
    result = reader.inspect_media_metadata(path, ())
    assert marker in b"\n".join(result["metadata_views"])
    assert marker not in result["head_sample"] + result["tail_sample"]


def test_jpeg_duplicate_exif_is_rejected(tmp_path):
    raw = jpeg_with_middle_exif_user_comment(b"ASCII\x00\x00\x00first")
    offset = raw.index(b"Exif\x00\x00") - 4
    size = int.from_bytes(raw[offset + 2 : offset + 4], "big") + 2
    raw = raw[:offset] + raw[offset : offset + size] + raw[offset:]
    path = tmp_path / "duplicate-exif.jpg"
    path.write_bytes(raw)
    with pytest.raises(reader.MediaMetadataError, match="^duplicate_exif$"):
        reader.inspect_media_metadata(path, ())


def test_camera_rational_exif_is_supported(tmp_path):
    from PIL.TiffImagePlugin import IFDRational

    exif = Image.Exif()
    exif[34665] = {33434: IFDRational(1, 125), 33437: IFDRational(28, 10)}
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="JPEG", exif=exif)
    path = tmp_path / "camera.jpg"
    path.write_bytes(output.getvalue())
    result = reader.inspect_media_metadata(path, ())
    assert b"1/125" in result["metadata_views"]
    assert b"14/5" in result["metadata_views"]


def test_progressive_jpeg_between_scan_metadata_is_rejected(tmp_path, monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (16, 16)).save(output, format="JPEG", progressive=True)
    raw = output.getvalue()
    first = raw.index(b"\xff\xda")
    second = raw.index(b"\xff\xda", first + 2)

    def comment(value):
        return b"\xff\xfe" + (len(value) + 2).to_bytes(2, "big") + value

    padding = comment(b"x" * 35000) * 2
    hidden = comment(b"dummy-secret-between-scans")
    path = tmp_path / "progressive.jpg"
    path.write_bytes(raw[:second] + padding + hidden + padding + raw[second:])
    # The fixture is an image accepted by the normal decoder.
    with Image.open(path) as image:
        image.load()
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("pixel decode")
    )
    with pytest.raises(reader.MediaMetadataError, match="^metadata_after_image_data$"):
        reader.inspect_media_metadata(path, ())


def test_progressive_jpeg_without_late_metadata_is_supported(tmp_path):
    path = tmp_path / "progressive-clean.jpg"
    Image.new("RGB", (16, 16)).save(path, format="JPEG", progressive=True)
    assert reader.inspect_media_metadata(path, ())["kind"] == "image"


def test_png_itxt_language_and_translated_keyword_are_scanned(tmp_path, monkeypatch):
    info = PngImagePlugin.PngInfo()
    info.add_text("before-padding", "x" * 70000)
    info.add_itxt(
        "Comment",
        "harmless",
        lang="dummy-secret-language",
        tkey="dummy-secret-translation",
    )
    info.add_text("after-padding", "x" * 70000)
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
    path = tmp_path / "international.png"
    path.write_bytes(output.getvalue())
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("pixel decode")
    )
    result = reader.inspect_media_metadata(path, ())
    views = b"\n".join(result["metadata_views"])
    for marker in [b"dummy-secret-language", b"dummy-secret-translation"]:
        assert marker in views
        assert marker not in result["head_sample"] + result["tail_sample"]


def test_png_text_survives_structural_key_collision(tmp_path, monkeypatch):
    info = PngImagePlugin.PngInfo()
    info.add_text("before-padding", "x" * 70000)
    info.add_text("transparency", "dummy-secret-collision")
    info.add_text("after-padding", "x" * 70000)
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
    raw = output.getvalue()
    insertion = raw.index(b"IDAT") - 4
    raw = raw[:insertion] + png_chunk(b"tRNS", b"\x00" * 6) + raw[insertion:]
    path = tmp_path / "collision.png"
    path.write_bytes(raw)
    with Image.open(path) as image:
        assert image.info["transparency"] == (0, 0, 0)
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("pixel decode")
    )
    result = reader.inspect_media_metadata(path, ())
    marker = b"dummy-secret-collision"
    assert marker in b"\n".join(result["metadata_views"])
    assert marker not in result["head_sample"] + result["tail_sample"]


def test_png_duplicate_icc_profile_cannot_hide_previous_values(tmp_path):
    raw = png_bytes()
    offset = raw.index(b"IDAT") - 4
    first = png_chunk(
        b"iCCP", b"profile\x00\x00" + zlib.compress(b"dummy-secret-earlier")
    )
    last = png_chunk(b"iCCP", b"profile\x00\x00" + zlib.compress(b"harmless-last"))
    path = tmp_path / "icc.png"
    path.write_bytes(raw[:offset] + first + last + raw[offset:])
    with pytest.raises(reader.MediaMetadataError, match="^duplicate_metadata_chunk$"):
        reader.inspect_media_metadata(path, ())


def test_png_ancillary_metadata_is_preserved_independently_of_info(tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("before-padding", "x" * 70000)
    info.add(b"vpAg", b"dummy-secret-ancillary")
    info.add_text("after-padding", "x" * 70000)
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
    path = tmp_path / "ancillary.png"
    path.write_bytes(output.getvalue())
    result = reader.inspect_media_metadata(path, ())
    marker = b"dummy-secret-ancillary"
    assert marker in b"\n".join(result["metadata_views"])
    assert marker not in result["head_sample"] + result["tail_sample"]


@pytest.mark.parametrize("kind", ["png", "jpeg", "mp4"])
def test_xmp_character_references_are_decoded_without_pixel_load(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(reader.shutil, "which", lambda _name: "/ffprobe")
    marker = "dummy-secret-xmp"
    entities = "".join("&#" + str(ord(c)) + ";" for c in marker)
    xml = (
        '<root creator="' + entities + '"><value>' + entities + "</value></root>"
    ).encode()
    if kind == "png":
        info = PngImagePlugin.PngInfo()
        info.add_itxt("XML:com.adobe.xmp", xml.decode(), zip=True)
        output = io.BytesIO()
        Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
        raw = output.getvalue()
        suffix = ".png"
    elif kind == "jpeg":
        raw = jpeg_with_middle_exif_user_comment(b"ASCII\x00\x00\x00public")
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + xml
        offset = raw.index(b"Exif\x00\x00") - 4
        raw = (
            raw[:offset]
            + b"\xff\xe1"
            + (len(payload) + 2).to_bytes(2, "big")
            + payload
            + raw[offset:]
        )
        suffix = ".jpg"
    else:
        payload = bytes.fromhex("be7acfcb97a942e89c71999491e3afac") + xml
        raw = mp4_bytes(struct.pack(">I4s", len(payload) + 8, b"uuid") + payload)
        suffix = ".mp4"
    path = tmp_path / ("xmp" + suffix)
    path.write_bytes(raw)
    monkeypatch.setattr(
        Image.Image, "load", lambda *_a, **_k: pytest.fail("pixel decode")
    )
    result = reader.inspect_media_metadata(
        path, (), probe_runner=lambda *_a, **_k: (0, video_probe_json(), b"")
    )
    assert marker.encode() in b"\n".join(result["metadata_views"])


@pytest.mark.parametrize(
    "xml", [b'<!DOCTYPE root [<!ENTITY x "secret">]><root>&x;</root>', b"<broken>"]
)
def test_xmp_dtd_and_malformed_xml_are_rejected(xml):
    with pytest.raises(reader.MediaMetadataError, match="^xmp_unverified$"):
        reader._xmp_values(xml, None)


@pytest.mark.parametrize("kind", ["jpeg", "mp4"])
def test_xmp_packets_stop_processing_at_aggregate_metadata_limit(
    tmp_path, monkeypatch, kind
):
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="JPEG")
    raw = output.getvalue()
    payload = b"http://ns.adobe.com/xap/1.0/\x00<root/>"
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    if kind == "jpeg":
        path = tmp_path / "many-xmp.jpg"
        path.write_bytes(raw[:2] + segment * 3 + raw[2:])
    else:
        payload = bytes.fromhex("be7acfcb97a942e89c71999491e3afac") + b"<root/>"
        box = struct.pack(">I4s", len(payload) + 8, b"uuid") + payload
        path = tmp_path / "many-xmp.mp4"
        path.write_bytes(mp4_bytes(box * 3))
    calls = []

    def xml_reader(data, output):
        calls.append(True)
        output.text("y" * 6000)

    monkeypatch.setattr(reader, "TEXT_LIMIT", 10000)
    monkeypatch.setattr(reader.shutil, "which", lambda _name: "/ffprobe")
    with pytest.raises(reader.MediaMetadataError, match="^metadata_limit$"):
        reader.inspect_media_metadata(
            path,
            (),
            xml_reader=xml_reader,
            probe_runner=lambda *_a, **_k: (0, video_probe_json(), b""),
        )
    assert len(calls) == 2
