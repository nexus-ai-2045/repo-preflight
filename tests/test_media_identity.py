"""Synthetic fixtures only; original user media never enters this repository."""

import importlib.util
import base64
import io
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zlib

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    EncodedStreamObject,
    ArrayObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)

requires_pdf = pytest.mark.skipif(
    sys.version_info < (3, 10),
    reason="PDF inspection requires Python 3.10+; core remains available",
)

requires_font = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="Pinned fontTools requires Python 3.11+"
)

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MEDIA = load("media_identity")
ENGINE = load("identity_normalization")
DENIED = "media-fixture-private@example.invalid"


def scan(data, kind, patterns=None):
    def image(value, patterns):
        return (
            ENGINE.scan_private_png
            if value.startswith(ENGINE.PNG_SIGNATURE)
            else ENGINE.scan_private_jpeg
        )(value, patterns)

    return MEDIA.scan_private_media(
        data,
        [DENIED] if patterns is None else patterns,
        kind=kind,
        check_text=ENGINE.scan_private_text,
        check_image=image,
    )


def pdf(
    *,
    title="Public fixture",
    body=None,
    hidden=None,
    attachment=False,
    active=False,
    encrypted=False,
    unknown_stream=False,
):
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    writer.add_metadata({"/Title": title})
    if body:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        content = DecodedStreamObject()
        content.set_data(("BT /F1 12 Tf 10 10 Td (" + body + ") Tj ET").encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    if hidden:
        writer._add_object(TextStringObject(hidden))
    if attachment:
        writer.add_attachment("fixture.txt", b"public attachment")
    if active:
        writer.add_js("app.alert('synthetic')")
    if unknown_stream:
        stream = DecodedStreamObject()
        stream.set_data(b"unknown binary payload")
        writer._add_object(stream)
    if encrypted:
        writer.encrypt("synthetic-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@requires_pdf
def test_pdf_readable_metadata_and_body_preserve_original_bytes():
    data = pdf(body="Public text")
    original = bytes(data)
    result = scan(data, "pdf")
    assert result["status"] == "pass"
    assert data == original
    assert result["decoder_versions"]["pypdf"]
    assert len(result["adapter_sha256"]) == 64


def ascii85_pdf(body="Public text", *, transform=lambda value: value):
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    content = EncodedStreamObject()
    content._data = transform(
        base64.a85encode(zlib.compress(("(" + body + ") Tj").encode())) + b"~>"
    )
    content[NameObject("/Filter")] = ArrayObject(
        [NameObject("/ASCII85Decode"), NameObject("/FlateDecode")]
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@requires_pdf
@pytest.mark.parametrize("body,expected", [("Public text", "pass"), (DENIED, "fail")])
def test_pdf_ascii85_flate_chain_checks_decoded_text(body, expected):
    original = ascii85_pdf(body)
    assert scan(original, "pdf")["status"] == expected


@requires_pdf
@pytest.mark.parametrize(
    "transform",
    [
        lambda value: value[:-2],
        lambda value: value + b"hidden-tail",
        lambda value: b"<~" + value,
        lambda value: b"!~>",
        lambda value: b"!z~>",
        lambda value: base64.a85encode(zlib.compress(b"() Tj") + b"tail") + b"~>",
    ],
)
def test_pdf_ascii85_chain_rejects_unconsumed_and_malformed_bytes(transform):
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(ascii85_pdf(transform=transform), "pdf")


def test_pdf_ascii85_expansion_is_bounded_before_decode(monkeypatch):
    monkeypatch.setattr(MEDIA, "DATA_LIMIT", 7)
    stream = EncodedStreamObject()
    stream._data = b"zz~>"
    with pytest.raises(MEDIA.MediaError, match="decoded_limit"):
        MEDIA._pdf_validate_stream_encoding(stream, ["/ASCII85Decode", "/FlateDecode"])


def test_pdf_ascii85_zero_groups_do_not_allocate_an_unbounded_list(monkeypatch):
    import tracemalloc

    monkeypatch.setattr(MEDIA, "DATA_LIMIT", 7)
    stream = EncodedStreamObject()
    stream._data = b"z" * 1_000_000 + b"~>"
    tracemalloc.start()
    try:
        with pytest.raises(MEDIA.MediaError, match="decoded_limit"):
            MEDIA._pdf_validate_stream_encoding(
                stream, ["/ASCII85Decode", "/FlateDecode"]
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * len(stream._data)


@requires_pdf
@pytest.mark.parametrize("location", ["title", "body", "hidden"])
def test_pdf_all_string_locations_use_identity_callback(location):
    result = scan(pdf(**{location: DENIED}), "pdf")
    assert result["status"] == "fail" and result["literal_match_count"] > 0


@requires_pdf
@pytest.mark.parametrize(
    "option", ["encrypted", "active", "attachment", "unknown_stream"]
)
def test_pdf_unreadable_or_unknown_regions_do_not_pass(option):
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(pdf(**{option: True}), "pdf")


@requires_pdf
@pytest.mark.parametrize(
    "data", [b"not a PDF", b"%PDF-1.7\ninvalid\n%%EOF", pdf() + b"trailing"]
)
def test_pdf_malformed_and_trailing_bytes_fail_closed(data):
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(data, "pdf")


def test_media_absent_dependency_and_limits_do_not_pass(monkeypatch):
    monkeypatch.setattr(MEDIA, "_run", lambda *a, **kw: (86, b"", b""))
    with pytest.raises(ValueError, match="dependency_unavailable"):
        scan(pdf(), "pdf")
    monkeypatch.setattr(MEDIA, "DATA_LIMIT", 2)
    with pytest.raises(ValueError, match="data_limit"):
        scan(b"123", "pdf")


@requires_pdf
def test_callback_inconsistent_result_is_not_accepted():
    with pytest.raises(ValueError, match="callback_unverified"):
        MEDIA.scan_private_media(
            pdf(),
            [DENIED],
            kind="pdf",
            check_text=lambda *_: {
                "status": "pass",
                "literal_match_count": 1,
                "local_path_match": False,
            },
            check_image=lambda *_: {},
        )


def test_subprocess_timeout_and_output_limits_are_real():
    with pytest.raises(MEDIA.MediaError, match="timeout"):
        MEDIA._run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.05,
            group=True,
        )
    with pytest.raises(MEDIA.MediaError, match="output_limit"):
        MEDIA._run([sys.executable, "-c", "print('x'*100000)"], limit=100, group=True)


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
requires_decoder = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="external FFmpeg tools unavailable; runtime must fail closed",
)


def make_av(tmp_path, kind):
    path = tmp_path / ("fixture." + kind)
    source = (
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.2"]
        if kind == "mp3"
        else ["-f", "lavfi", "-i", "color=c=blue:s=16x16:d=0.2"]
    )
    codec = ["-c:a", "libmp3lame"] if kind == "mp3" else ["-c:v", "mpeg4"]
    subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", *source, *codec, str(path)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return path


@requires_decoder
def test_valid_audio_video_decode_and_metadata_detection(tmp_path):
    kind = "mp3"
    path = make_av(tmp_path, kind)
    assert scan(path.read_bytes(), kind)["status"] == "pass"
    if kind == "mp3":
        from mutagen.id3 import ID3, TIT2

        tags = ID3(path)
        tags.add(TIT2(encoding=3, text=[DENIED]))
        tags.save(path)
    assert scan(path.read_bytes(), kind)["status"] == "fail"


@requires_decoder
def test_unknown_mp4_atom_is_not_silently_ignored(tmp_path):
    path = make_av(tmp_path, "mp4")
    with pytest.raises(ValueError, match="unsupported_stream"):
        scan(path.read_bytes() + b"\x00\x00\x00\x0czzzzdata", "mp4")


@requires_decoder
@pytest.mark.parametrize("kind", ["mp3", "mp4"])
def test_invalid_audio_video_is_not_exempted(tmp_path, kind):
    path = make_av(tmp_path, kind)
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(path.read_bytes()[:25], kind)


def test_ffmpeg_absence_is_fixed_safe_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(MEDIA.shutil, "which", lambda *_: None)
    with pytest.raises(MEDIA.MediaError, match="dependency_unavailable"):
        MEDIA._audio_video(tmp_path / "unused.mp3", "mp3", MEDIA._Extracted())


@requires_pdf
def test_pdf_next_action_array_is_not_an_unchecked_region():
    from pypdf.generic import ArrayObject

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    launch = DictionaryObject(
        {
            NameObject("/S"): NameObject("/Launch"),
            NameObject("/F"): TextStringObject("synthetic.exe"),
        }
    )
    action = DictionaryObject(
        {
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject("https://example.invalid"),
            NameObject("/Next"): ArrayObject([launch]),
        }
    )
    writer._root_object[NameObject("/A")] = action
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="unsupported_active_or_attachment"):
        scan(output.getvalue(), "pdf")


@requires_decoder
@pytest.mark.parametrize("box", [b"wide", b"mvhd"])
def test_known_mp4_box_name_cannot_hide_an_unknown_payload(tmp_path, box):
    path = make_av(tmp_path, "mp4")
    payload = DENIED.encode()
    atom = (8 + len(payload)).to_bytes(4, "big") + box + payload
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(path.read_bytes() + atom, "mp4")


@requires_decoder
@pytest.mark.parametrize("kind,box", [("mp3", None), ("mp4", b"mdat")])
def test_unreferenced_media_bytes_are_not_coding_payload(tmp_path, kind, box):
    path = make_av(tmp_path, kind)
    payload = DENIED.encode()
    trailer = (8 + len(payload)).to_bytes(4, "big") + box + payload if box else payload
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(path.read_bytes() + trailer, kind)


def font_pdf(family):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A"])
    builder.setupCharacterMap({65: "A"})
    builder.setupGlyf({key: TTGlyphPen(None).glyph() for key in [".notdef", "A"]})
    builder.setupHorizontalMetrics({key: (500, 0) for key in [".notdef", "A"]})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {
            "familyName": family,
            "styleName": "Regular",
            "uniqueFontIdentifier": "SyntheticFixture",
            "fullName": family,
            "psName": "SyntheticFixture",
        }
    )
    builder.setupOS2(
        sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200
    )
    builder.setupPost()
    builder.setupMaxp()
    font = io.BytesIO()
    builder.save(font)
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    stream = DecodedStreamObject()
    stream.set_data(font.getvalue())
    descriptor = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontFile2"): writer._add_object(stream),
        }
    )
    writer._root_object[NameObject("/FixtureFont")] = writer._add_object(descriptor)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


@requires_font
@pytest.mark.parametrize(
    "family,expected", [("Synthetic Fixture", "pass"), (DENIED, "fail")]
)
def test_pdf_embedded_font_is_decoded_and_font_metadata_checked(family, expected):
    assert scan(font_pdf(family), "pdf")["status"] == expected


def private_cover():
    import struct
    import zlib

    def chunk(kind, value):
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    return (
        ENGINE.PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\0" + DENIED.encode())
        + chunk(b"IDAT", zlib.compress(b"\0\x10"))
        + chunk(b"IEND", b"")
    )


@requires_decoder
def test_cover_image_metadata_is_not_hidden_by_media_decoder(tmp_path):
    kind = "mp3"
    path = make_av(tmp_path, kind)
    if kind == "mp3":
        from mutagen.id3 import ID3, APIC

        tags = ID3(path)
        tags.add(
            APIC(
                encoding=3,
                mime="image/png",
                type=3,
                desc="Synthetic cover",
                data=private_cover(),
            )
        )
        tags.save(path)
    assert scan(path.read_bytes(), kind)["status"] == "fail"


@requires_decoder
def test_mp4_text_subtitle_is_checked(tmp_path):
    original = make_av(tmp_path, "mp4")
    subtitle = tmp_path / "fixture.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:00,100\n" + DENIED + "\n", encoding="utf-8"
    )
    output = tmp_path / "subtitled.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(original),
            "-i",
            str(subtitle),
            "-c:v",
            "copy",
            "-c:s",
            "mov_text",
            str(output),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    with pytest.raises(ValueError, match="unsupported_stream"):
        scan(output.read_bytes(), "mp4")


def test_cover_advisory_is_not_a_general_warning_exception():
    attached = [{"disposition": {"attached_pic": 1}}]
    with pytest.raises(MEDIA.MediaError, match="unverified"):
        MEDIA._av_diagnostics(b"unknown decoder warning", attached)
    warning = b"[in#0 @ 0x123abc] stream 0, timescale not set\n"
    with pytest.raises(MEDIA.MediaError, match="unverified"):
        MEDIA._av_diagnostics(warning, [])
    assert MEDIA._av_diagnostics(warning, attached)


@requires_decoder
def test_id3v1_is_inspected_even_with_public_id3v2_title(tmp_path):
    from mutagen.id3 import ID3, TIT2
    from mutagen.id3._id3v1 import MakeID3v1

    path = make_av(tmp_path, "mp3")
    legacy = "legacy-media-private"
    tags = ID3()
    tags.add(TIT2(encoding=3, text=[legacy]))
    with path.open("ab") as target:
        target.write(MakeID3v1(tags))
    assert scan(path.read_bytes(), "mp3", [legacy])["status"] == "fail"


@requires_pdf
def test_pdf_previous_revision_is_not_hidden_by_latest_metadata():
    writer = PdfWriter(io.BytesIO(pdf(title=DENIED)), incremental=True)
    writer.add_metadata({"/Title": "Public replacement"})
    output = io.BytesIO()
    writer.write(output)
    assert scan(output.getvalue(), "pdf")["status"] == "fail"


def test_decoder_process_does_not_receive_caller_credentials(monkeypatch):
    monkeypatch.setenv("MEDIA_PRIVATE_SENTINEL", "synthetic-test-only")
    code, out, error = MEDIA._run(
        [
            sys.executable,
            "-I",
            "-c",
            "import os;print(os.environ.get('MEDIA_PRIVATE_SENTINEL', 'absent'))",
        ],
        group=True,
    )
    assert code == 0 and out.strip() == b"absent" and not error


def test_unknown_child_error_cannot_disclose_extracted_private_text(monkeypatch):
    raw = json.dumps({"error": DENIED}).encode()
    monkeypatch.setattr(MEDIA, "_run", lambda *a, **kw: (87, raw, b""))
    with pytest.raises(ValueError) as error:
        scan(pdf(), "pdf")
    assert DENIED not in str(error.value)
    assert str(error.value) == "private_identity_media_unverified"


@requires_pdf
def test_pdf_opening_view_uses_decoder_destination_validation():
    from pypdf.generic import ArrayObject

    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    writer._root_object[NameObject("/OpenAction")] = ArrayObject(
        [page.indirect_reference, NameObject("/Fit")]
    )
    output = io.BytesIO()
    writer.write(output)
    assert scan(output.getvalue(), "pdf")["status"] == "pass"


@requires_decoder
def test_mp4_normal_and_mvhd_trailer_both_reject_before_callbacks(tmp_path):
    from mutagen.mp4 import Atoms

    path = make_av(tmp_path, "mp4")
    with path.open("rb") as handle:
        atoms = Atoms(handle)
        moov = next(a for a in atoms.atoms if a.name == b"moov")
        mvhd = next(a for a in moov.children if a.name == b"mvhd")
    original = path.read_bytes()
    data = bytearray(original)
    payload = DENIED.encode()
    data[mvhd.offset + mvhd.length : mvhd.offset + mvhd.length] = payload
    for atom in (moov, mvhd):
        data[atom.offset : atom.offset + 4] = (atom.length + len(payload)).to_bytes(
            4, "big"
        )

    def forbidden_callback(*args):
        pytest.fail("unsupported MP4 must stop before callbacks")

    for candidate in (original, bytes(data)):
        with pytest.raises(
            ValueError, match="private_identity_media_unsupported_stream"
        ):
            MEDIA.scan_private_media(
                candidate,
                [DENIED],
                kind="mp4",
                check_text=forbidden_callback,
                check_image=forbidden_callback,
            )


def test_mp4_process_failure_is_safe(tmp_path, monkeypatch):
    path = tmp_path / "malformed.mp4"
    path.write_bytes(b"not a valid media container")
    monkeypatch.setattr(MEDIA.shutil, "which", lambda name: "/synthetic/" + name)
    monkeypatch.setattr(MEDIA, "_run", lambda *a, **kw: (1, b"", b"private stderr"))
    with pytest.raises(MEDIA.MediaError, match="^unverified$"):
        MEDIA._mp4(path, MEDIA._Extracted())


def test_mp4_child_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(MEDIA, "_run", lambda *a, **kw: (1, b"", b"private stderr"))
    with pytest.raises(ValueError, match="^private_identity_media_unverified$"):
        scan(b"not a valid media container", "mp4")


def test_mp4_missing_decoder_stops_before_process(tmp_path, monkeypatch):
    path = tmp_path / "malformed.mp4"
    path.write_bytes(b"not a valid media container")
    monkeypatch.setattr(MEDIA.shutil, "which", lambda name: None)

    def unexpected_process(*args, **kwargs):
        pytest.fail("missing decoder must stop before process execution")

    monkeypatch.setattr(MEDIA, "_run", unexpected_process)
    with pytest.raises(MEDIA.MediaError, match="^dependency_unavailable$"):
        MEDIA._mp4(path, MEDIA._Extracted())


def coverage_pdf(*, header=False, comment=False, free=False):
    from pypdf.generic import ArrayObject

    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    if header:
        writer._header = b"%PDF-1.3\n% " + DENIED.encode()
    references = []
    for _ in range(2):
        stream = DecodedStreamObject()
        stream.set_data(b"q\nQ\n")
        references.append(writer._add_object(stream.flate_encode()))
    page[NameObject("/Contents")] = ArrayObject(references)
    hidden = writer._add_object(TextStringObject(DENIED)) if free else None
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    if free:
        start = data.index(b"xref\n")
        lines = data[start:].splitlines(keepends=True)
        index = 2 + hidden.idnum
        assert lines[index].endswith(b"n \n")
        lines[index] = lines[index][:-3] + b"f \n"
        data = data[:start] + b"".join(lines)
    if comment:
        marker = b"% " + DENIED.encode() + b"\n"
        start = data.index(b"xref\n")
        data = data[:start] + marker + data[start:]
        data = data.replace(
            b"startxref\n" + str(start).encode() + b"\n",
            b"startxref\n" + str(start + len(marker)).encode() + b"\n",
        )
    return data


@requires_pdf
@pytest.mark.parametrize("location", ["header", "comment", "free"])
def test_pdf_complete_source_regions_detect_prior_coverage_gaps(location):
    assert scan(coverage_pdf(**{location: True}), "pdf")["status"] == "fail"


@requires_pdf
def test_pdf_duplicate_compressed_streams_bind_distinct_original_ranges():
    assert scan(coverage_pdf(), "pdf")["status"] == "pass"


@requires_pdf
def test_pdf_corrected_stream_length_cannot_gain_binary_exemption():
    data = coverage_pdf().replace(b"/Length 12", b"/Length 14", 1)
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(data, "pdf")


@requires_pdf
def test_pdf_metadata_uses_authoritative_odd_hex_and_source_positions():
    output = MEDIA._Extracted()
    MEDIA._pdf_metadata(b"% ignored comment\n<41f> /escaped#40name (public) ", output)
    assert "A\u00f0" in output.texts
    assert "/escaped@name" in output.texts


@requires_pdf
@pytest.mark.parametrize("value", [b"(unfinished", b"<4142", b"<414g>"])
def test_pdf_metadata_malformed_primitives_cannot_be_silently_dropped(value):
    with pytest.raises(Exception):
        MEDIA._pdf_metadata(value, MEDIA._Extracted())


@requires_pdf
def test_pdf_metadata_token_resource_bound():
    with pytest.raises(MEDIA.MediaError, match="text_limit"):
        MEDIA._pdf_metadata(b"(" + b"a" * 300000 + b")", MEDIA._Extracted())


def test_pdf_dependency_drift_and_python39_fail_closed(monkeypatch):
    import pypdf

    monkeypatch.setattr(pypdf, "__version__", "unreviewed-version")
    with pytest.raises(MEDIA.MediaError, match="dependency_unavailable"):
        MEDIA._pdf_dependencies(MEDIA._Extracted())
    monkeypatch.setattr(MEDIA.sys, "version_info", (3, 9))
    with pytest.raises(MEDIA.MediaError, match="dependency_unavailable"):
        MEDIA._pdf_dependencies(MEDIA._Extracted())


@requires_pdf
@pytest.mark.parametrize("forgery", ["missing", "duplicate_interval", "changed_data"])
def test_pdf_forged_stream_binding_is_rejected(forgery):
    from pypdf import PdfReader

    with MEDIA._PDFSource(coverage_pdf()) as source:
        reader = PdfReader(source.stream, strict=True)
        objects = [part.get_object() for part in reader.pages[0]["/Contents"]]
        for obj in objects:
            obj.get_data()
        if forgery == "missing":
            source.bound.pop(id(objects[0]))
        elif forgery == "duplicate_interval":
            source.bound[id(objects[0])]["range"] = source.bound[id(objects[1])][
                "range"
            ]
        else:
            objects[0]._data = b"changed"
        with pytest.raises(MEDIA.MediaError, match="unsupported_region"):
            source.inspect_remaining(objects, MEDIA._Extracted())


def role_pdf(role, private=False):
    from pypdf.generic import ArrayObject, NumberObject, FloatObject

    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    if role == "goto":
        writer._root_object[NameObject("/OpenAction")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/GoTo"),
                NameObject("/D"): ArrayObject(
                    [page.indirect_reference, NameObject("/Fit")]
                ),
            }
        )
    elif role == "attribute":
        attribute = DictionaryObject(
            {
                NameObject("/O"): NameObject("/List"),
                NameObject("/ListNumbering"): NameObject("/Disc"),
            }
        )
        if private:
            attribute[NameObject("/Alt")] = TextStringObject(DENIED)
        element = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/StructElem"),
                NameObject("/S"): NameObject("/L"),
                NameObject("/A"): ArrayObject([attribute]),
            }
        )
        writer._root_object[NameObject("/StructTreeRoot")] = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/StructTreeRoot"),
                NameObject("/K"): writer._add_object(element),
            }
        )
    else:
        glyph = DecodedStreamObject()
        glyph.set_data(
            b"500 0 d0\n" + (b"% " + DENIED.encode() + b"\n" if private else b"")
        )
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type3"),
                NameObject("/FontBBox"): ArrayObject(
                    [
                        NumberObject(0),
                        NumberObject(0),
                        NumberObject(500),
                        NumberObject(500),
                    ]
                ),
                NameObject("/FontMatrix"): ArrayObject(
                    [
                        FloatObject(0.001),
                        NumberObject(0),
                        NumberObject(0),
                        FloatObject(0.001),
                        NumberObject(0),
                        NumberObject(0),
                    ]
                ),
                NameObject("/CharProcs"): DictionaryObject(
                    {NameObject("/A"): writer._add_object(glyph.flate_encode())}
                ),
                NameObject("/Encoding"): DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Encoding"),
                        NameObject("/Differences"): ArrayObject(
                            [NumberObject(65), NameObject("/A")]
                        ),
                    }
                ),
                NameObject("/FirstChar"): NumberObject(65),
                NameObject("/LastChar"): NumberObject(65),
                NameObject("/Widths"): ArrayObject([NumberObject(500)]),
                NameObject("/Resources"): DictionaryObject(),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf (A) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content.flate_encode())
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


@requires_pdf
@pytest.mark.parametrize("role", ["goto", "attribute", "glyph"])
def test_pdf_navigation_attributes_and_glyphs_keep_their_data_roles(role):
    assert scan(role_pdf(role), "pdf")["status"] == "pass"
    if role != "goto":
        assert scan(role_pdf(role, private=True), "pdf")["status"] == "fail"


@requires_pdf
def test_pdf_flate_decoder_unused_trailer_is_not_a_validated_symbol_region():
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    stream = DecodedStreamObject()
    stream.set_data(b"q\nQ\n")
    encoded = stream.flate_encode()
    encoded._data += DENIED.encode()
    page[NameObject("/Contents")] = writer._add_object(encoded)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="unsupported_region"):
        scan(output.getvalue(), "pdf")


@requires_pdf
@pytest.mark.parametrize("private", [False, True])
def test_pdf_known_free_reference_uses_null_and_keeps_free_source_inspection(private):
    from pypdf.generic import IndirectObject

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    ref = writer._add_object(
        TextStringObject(DENIED if private else "public removed string")
    )
    writer._root_object[NameObject("/OptionalEntry")] = IndirectObject(
        ref.idnum, 0, writer
    )
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    start = data.index(b"xref\n")
    lines = data[start:].splitlines(keepends=True)
    index = 2 + ref.idnum
    assert lines[index].endswith(b"n \n")
    lines[index] = lines[index][:-3] + b"f \n"
    data = data[:start] + b"".join(lines)
    result = scan(data, "pdf")
    assert result["status"] == ("fail" if private else "pass")
    assert result["pdf_known_free_references"] > 0


@requires_pdf
@pytest.mark.parametrize(
    "state", ["unknown", "generation", "live_conflict", "object_stream_conflict"]
)
def test_pdf_known_free_compatibility_cannot_hide_other_reference_failures(state):
    from pypdf.generic import IndirectObject

    reader = MEDIA._pdf_reader(io.BytesIO(pdf()))
    number = 99999
    if state != "unknown":
        reader.xref_free_entry.setdefault(0, {})[number] = True
    if state == "live_conflict":
        reader.xref.setdefault(0, {})[number] = 1
    if state == "object_stream_conflict":
        reader.xref_objStm[number] = (1, 0)
    generation = 1 if state == "generation" else 0
    with pytest.raises(Exception):
        reader.get_object(IndirectObject(number, generation, reader))
    assert reader.strict is True


@requires_pdf
@pytest.mark.parametrize(
    "target,expected",
    [
        ("mailto:public@example.invalid", "pass"),
        ("mailto:" + DENIED, "fail"),
        ("mailto:" + DENIED.replace("@", "%40"), "fail"),
        ("mailto:", "reject"),
        ("mailto:?subject=public", "reject"),
        ("mailto:pub\nlic@example.invalid", "reject"),
        ("mailto:pub\tlic@example.invalid", "reject"),
        ("mailto:invalid-address", "reject"),
        ("mailto:user@", "reject"),
        ("mailto:user%0a@example.invalid", "reject"),
        ("mailto:user%GG@example.invalid", "reject"),
        ("javascript:alert(1)", "reject"),
        ("file:///tmp/synthetic", "reject"),
    ],
)
def test_pdf_mailto_is_validated_inert_and_percent_decoded_metadata(target, expected):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer._root_object[NameObject("/A")] = DictionaryObject(
        {
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject(target),
        }
    )
    output = io.BytesIO()
    writer.write(output)
    if expected == "reject":
        with pytest.raises(ValueError, match="unsupported_active_or_attachment"):
            scan(output.getvalue(), "pdf")
    else:
        assert scan(output.getvalue(), "pdf")["status"] == expected


def test_metadata_string_item_limit_and_byte_limit_remain_separate(monkeypatch):
    monkeypatch.setattr(MEDIA, "OBJECT_LIMIT", 2)
    monkeypatch.setattr(MEDIA, "TEXT_LIMIT", 100)
    output = MEDIA._Extracted()
    output.text("a")
    output.text("b")
    with pytest.raises(MEDIA.MediaError, match="text_limit"):
        output.text("c")
    monkeypatch.setattr(MEDIA, "OBJECT_LIMIT", 100)
    monkeypatch.setattr(MEDIA, "TEXT_LIMIT", 4)
    output = MEDIA._Extracted()
    output.text("abc")
    with pytest.raises(MEDIA.MediaError, match="text_limit"):
        output.text("de")


@requires_pdf
def test_pdf_null_cannot_substitute_a_mandatory_pages_dictionary():
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    pages = writer._root_object["/Pages"].indirect_reference
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    start = data.index(b"xref\n")
    lines = data[start:].splitlines(keepends=True)
    index = 2 + pages.idnum
    assert lines[index].endswith(b"n \n")
    lines[index] = lines[index][:-3] + b"f \n"
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(data[:start] + b"".join(lines), "pdf")


def graphics_pdf(role, payload):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    stream = DecodedStreamObject()
    stream.set_data(payload)
    from pypdf.generic import NumberObject

    if role == "pattern":
        stream[NameObject("/Type")] = NameObject("/Pattern")
        stream[NameObject("/PatternType")] = NumberObject(1)
    elif role == "function":
        stream[NameObject("/FunctionType")] = NumberObject(4)
    else:
        stream[NameObject("/FunctionType")] = NumberObject(99)
    writer._root_object[NameObject("/SyntheticGraphics")] = writer._add_object(
        stream.flate_encode()
    )
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


@requires_pdf
@pytest.mark.parametrize("role", ["pattern", "function"])
def test_pdf_standard_graphics_are_inert_fully_scanned_content(role):
    public = b"q\nQ\n" if role == "pattern" else b"{ 1 0 mul }"
    assert scan(graphics_pdf(role, public), "pdf")["status"] == "pass"
    assert (
        scan(graphics_pdf(role, b"% " + DENIED.encode() + b"\n" + public), "pdf")[
            "status"
        ]
        == "fail"
    )
    escaped = (
        b"(" + b"".join(("\\%03o" % value).encode() for value in DENIED.encode()) + b")"
    )
    assert scan(graphics_pdf(role, escaped), "pdf")["status"] == "fail"


@requires_pdf
def test_pdf_unknown_graphics_and_binary_are_not_reclassified_as_source_text():
    with pytest.raises(ValueError, match="unsupported_stream"):
        scan(graphics_pdf("unknown", b"{ 1 0 mul }"), "pdf")
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(graphics_pdf("pattern", b"\xff\x00\x80"), "pdf")


def mutated_font_pdf(private=False, malformed=False):
    from pypdf import PdfReader

    reader = PdfReader(
        io.BytesIO(font_pdf(DENIED if private else "Public font")), strict=True
    )
    writer = PdfWriter(clone_from=reader)
    for obj in writer._objects:
        if isinstance(obj, DecodedStreamObject) and obj._data.startswith(
            b"\x00\x01\x00\x00"
        ):
            data = bytearray(obj._data)
            if malformed:
                data[20:24] = (len(data) + 100).to_bytes(4, "big")
            else:
                data[16] ^= 1
            obj._data = bytes(data)
            break
    else:
        raise AssertionError("synthetic font stream not found")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@requires_font
@pytest.mark.parametrize("private", [False, True])
def test_font_checksum_is_diagnostic_only_after_full_decoder_validation(private):
    data = mutated_font_pdf(private=private)
    before = hashlib.sha256(data).hexdigest()
    result = scan(data, "pdf")
    assert result["status"] == ("fail" if private else "pass")
    assert result["font_checksum_mismatches"] > 0
    assert hashlib.sha256(data).hexdigest() == before


@requires_font
def test_checksum_diagnostics_never_hide_malformed_font_offsets():
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(mutated_font_pdf(malformed=True), "pdf")


@requires_font
@pytest.mark.parametrize("private", [False, True])
def test_type1_full_source_inspection_is_used_by_pdf_entry(private):
    from test_type1_identity import program, alter_plain
    from pypdf.generic import NumberObject

    data, lengths = program()
    if private:
        data, lengths = alter_plain(
            data, lengths, lambda p: p[:4] + b"% " + DENIED.encode() + b"\n" + p[4:]
        )
    stream = DecodedStreamObject()
    stream.set_data(data)
    for key, value in zip(("/Length1", "/Length2", "/Length3"), lengths):
        stream[NameObject(key)] = NumberObject(value)
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer._root_object[NameObject("/SyntheticFont")] = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontFile"): writer._add_object(stream.flate_encode()),
        }
    )
    output = io.BytesIO()
    writer.write(output)
    raw = output.getvalue()
    before = hashlib.sha256(raw).hexdigest()
    result = scan(raw, "pdf")
    assert result["status"] == ("fail" if private else "pass")
    assert result["type1_metrics"][0]["binary_ranges"] == 3
    assert result["type1_metrics"][0]["operations"] <= MEDIA.OBJECT_LIMIT
    assert "type1_adapter_sha256" in result["decoder_versions"]
    assert hashlib.sha256(raw).hexdigest() == before


@pytest.fixture
def supported_mp4(tmp_path):
    path = tmp_path / "h264.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=16x16:d=0.2",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return path


def inject_sei(path, uuid, payload, tmp_path):
    target = tmp_path / "injected.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_metadata=sei_user_data=" + uuid.hex() + "+" + payload,
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return target.read_bytes()


@requires_decoder
def test_supported_h264_main_pass_preserves_source(supported_mp4):
    original = supported_mp4.read_bytes()
    result = scan(original, "mp4")
    assert result["status"] == "pass"
    assert result["decoder_versions"]["hachoir"] == "3.4.0"
    assert len(result["decoder_versions"]["mp4_adapter_sha256"]) == 64
    assert supported_mp4.read_bytes() == original


@requires_decoder
@pytest.mark.parametrize("split", [False, True])
def test_main_checks_sei_payload_and_uuid_boundary(supported_mp4, tmp_path, split):
    uuid = b"public--" + DENIED[:8].encode() if split else b"public-uuid-1234"
    assert len(uuid) == 16
    payload = DENIED[8:] if split else DENIED
    candidate = inject_sei(supported_mp4, uuid, payload, tmp_path)
    assert scan(candidate, "mp4")["status"] == "fail"


@requires_decoder
def test_main_checks_full_sei_uuid(supported_mp4, tmp_path):
    marker = "priv-uuid-123456"
    candidate = inject_sei(supported_mp4, marker.encode(), "public", tmp_path)
    assert scan(candidate, "mp4", [marker])["status"] == "fail"


@requires_decoder
def test_mp4_offline_mov_options_before_each_open(supported_mp4, monkeypatch):
    original = MEDIA._run
    calls = []

    def traced(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(MEDIA, "_run", traced)
    MEDIA._mp4(supported_mp4, MEDIA._Extracted())
    opens = [
        args
        for args in calls
        if args[0] in {FFMPEG, FFPROBE} and "-version" not in args
    ]
    assert len(opens) == 4
    for args in opens:
        assert args[args.index("-f") + 1] == "mov"
        assert args[args.index("-enable_drefs") + 1] == "0"
        assert args[args.index("-use_absolute_path") + 1] == "0"
        assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
        assert args.index("-use_absolute_path") < args.index(str(supported_mp4))


@requires_decoder
@pytest.mark.parametrize(
    "fault", ["hash", "chunks", "oversize", "process", "dependency", "diagnostic"]
)
def test_mp4_helper_ipc_fails_closed(supported_mp4, monkeypatch, fault):
    original = MEDIA._run

    def broken(args, **kwargs):
        code, raw, error = original(args, **kwargs)
        if "--packet-ranges" in args:
            if fault == "dependency":
                return 1, b'{"status":"error","code":"mp4_hachoir_unavailable"}', b""
            if fault == "process":
                return 1, b"", b"private stderr must not escape"
            value = json.loads(raw)
            if fault == "hash":
                value["source_sha256"] = "0" * 64
            elif fault == "chunks":
                value["metadata_chunks_b64"] = []
            elif fault == "diagnostic":
                value["diagnostics"] = [{"code": "private diagnostic"}]
            else:
                value["codec_metadata_chunks_b64"] = ["QQ=="] * (MEDIA.OBJECT_LIMIT + 2)
            return 0, json.dumps(value).encode(), b""
        return code, raw, error

    monkeypatch.setattr(MEDIA, "_run", broken)
    with pytest.raises(
        MEDIA.MediaError, match="unverified|unsupported_metadata|dependency_unavailable"
    ):
        MEDIA._mp4(supported_mp4, MEDIA._Extracted())


@requires_decoder
@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
def test_main_checks_sei_utf16(supported_mp4, tmp_path, codec):
    encoded = DENIED.encode(codec)
    placeholder = "Q" * len(encoded)
    candidate = inject_sei(supported_mp4, b"public-uuid-1234", placeholder, tmp_path)
    assert candidate.count(placeholder.encode()) == 1
    candidate = candidate.replace(placeholder.encode(), encoded)
    assert scan(candidate, "mp4")["status"] == "fail"


@requires_pdf
@pytest.mark.parametrize("marker", [DENIED, "Public previous metadata"])
def test_overwritten_compressed_metadata_inspects_all_revisions(marker):
    from pypdf.generic import IndirectObject

    old = PdfWriter()
    old.add_blank_page(72, 72)
    stream = DecodedStreamObject()
    stream.set_data(("<x>" + marker + "</x>").encode())
    stream[NameObject("/Type")] = NameObject("/Metadata")
    ref = old._add_object(stream.flate_encode())
    old.root_object[NameObject("/Probe")] = ref
    original = io.BytesIO()
    old.write(original)
    new = PdfWriter(io.BytesIO(original.getvalue()), incremental=True)
    replacement = DecodedStreamObject()
    replacement.set_data(b"<x>Public replacement</x>")
    replacement[NameObject("/Type")] = NameObject("/Metadata")
    replacement = replacement.flate_encode()
    replacement.indirect_reference = IndirectObject(ref.idnum, ref.generation, new)
    new._objects[ref.idnum - 1] = replacement
    output = io.BytesIO()
    new.write(output)
    data = output.getvalue()
    assert marker.encode() not in data
    assert scan(data, "pdf")["status"] == ("fail" if marker == DENIED else "pass")


def incremental_pdf():
    writer = PdfWriter(io.BytesIO(pdf()), incremental=True)
    writer.add_metadata({"/Title": "Public new revision"})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def mutate_final_xref(data, mutation):
    from pypdf import PdfReader
    from pypdf.generic import StreamObject

    reader = PdfReader(io.BytesIO(data), strict=True)
    obj = next(
        obj
        for obj in reader.resolved_objects.values()
        if isinstance(obj, StreamObject) and obj.get("/Type") == "/XRef"
    )
    stream = DecodedStreamObject()
    for key, value in obj.items():
        if key not in {"/Filter", "/Length", "/DecodeParms"}:
            stream[key] = value
    stream.set_data(obj.get_data())
    mutation(stream, reader)
    output = io.BytesIO()
    output.write(data[: reader._startxref])
    output.write(f"{obj.indirect_reference.idnum} 0 obj\n".encode())
    stream.write_to_stream(output)
    output.write(f"\nendobj\nstartxref\n{reader._startxref}\n%%EOF\n".encode())
    return output.getvalue()


@requires_pdf
@pytest.mark.parametrize(
    "fault", ["tail", "truncated", "width", "index", "cycle", "outside"]
)
def test_xref_stream_declared_shape_and_chain_are_strict(fault):
    from pypdf.generic import NumberObject, ArrayObject

    def mutation(stream, reader):
        if fault == "tail":
            stream.set_data(stream.get_data() + b"hidden bytes")
        elif fault == "truncated":
            stream.set_data(stream.get_data()[:-1])
        elif fault == "width":
            stream[NameObject("/W")] = ArrayObject([NumberObject(1)] * 4)
        elif fault == "index":
            stream[NameObject("/Index")] = ArrayObject(
                [NumberObject(0), NumberObject(999999)]
            )
        elif fault == "cycle":
            stream[NameObject("/Prev")] = NumberObject(reader._startxref)
        else:
            stream[NameObject("/Prev")] = NumberObject(999999999)

    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(mutate_final_xref(incremental_pdf(), mutation), "pdf")


@requires_pdf
def test_linked_rootless_partial_xref_is_not_standalone_document():
    data = incremental_pdf()
    first, rest = data.split(b"startxref", 1)
    # Existing writer created a complete final Root; only the linked previous
    # table is partial. Equal-length spaces preserve every original offset.
    import re

    first = re.sub(rb"/Root [0-9]+ [0-9]+ R", lambda match: b" " * len(match[0]), first)
    candidate = first + b"startxref" + rest
    assert scan(candidate, "pdf")["status"] == "pass"


@requires_pdf
def test_revision_budget_is_global(monkeypatch):
    monkeypatch.setattr(MEDIA, "OBJECT_LIMIT", 12)
    output = MEDIA._Extracted()
    with MEDIA._PDFSource(incremental_pdf()) as source:
        with pytest.raises(MEDIA.MediaError, match="object_limit|unsupported_region"):
            MEDIA._pdf_contents(source, output)


@requires_pdf
def test_hybrid_table_companion_and_exact_duplicate_source_spans():
    from pypdf import PdfReader

    data = incremental_pdf()
    reader = PdfReader(io.BytesIO(data), strict=True)
    root = reader.trailer.raw_get("/Root")
    previous = next(
        obj["/Prev"]
        for obj in reader.resolved_objects.values()
        if obj.get("/Type") == "/XRef"
    )
    offset = len(data)
    suffix = (
        f"xref\n0 1\n0000000000 65535 f \ntrailer\n"
        f"<< /Size {reader.trailer['/Size']} /Root {root.idnum} {root.generation} R "
        f"/XRefStm {reader._startxref} /Prev {previous} >>\n"
        f"startxref\n{offset}\n%%EOF\n"
    ).encode()
    assert scan(data + suffix, "pdf")["status"] == "pass"


@requires_pdf
def test_older_forbidden_action_is_not_hidden_by_new_dictionary():
    from pypdf.generic import IndirectObject

    writer = PdfWriter()
    writer.add_blank_page(72, 72)
    old = DictionaryObject(
        {NameObject("/JS"): TextStringObject("inert forbidden source")}
    )
    ref = writer._add_object(old)
    writer.root_object[NameObject("/Probe")] = ref
    base = io.BytesIO()
    writer.write(base)
    new = PdfWriter(io.BytesIO(base.getvalue()), incremental=True)
    replacement = DictionaryObject(
        {NameObject("/Public"): TextStringObject("replacement")}
    )
    replacement.indirect_reference = IndirectObject(ref.idnum, ref.generation, new)
    new._objects[ref.idnum - 1] = replacement
    result = io.BytesIO()
    new.write(result)
    with pytest.raises(ValueError, match="unsupported_active_or_attachment"):
        scan(result.getvalue(), "pdf")


@requires_pdf
def test_partial_source_overlap_remains_refused():
    from pypdf import PdfReader

    with MEDIA._PDFSource(coverage_pdf()) as source:
        reader = PdfReader(source.stream, strict=True)
        objects = [part.get_object() for part in reader.pages[0]["/Contents"]]
        first = source.bound[id(objects[0])]
        second = source.bound[id(objects[1])]
        start, end = first["range"]
        region = source.data[start + 1 : end]
        objects[1]._data = region
        second.update(raw=region, range=(start + 1, end), reads=[(start + 1, region)])
        with pytest.raises(MEDIA.MediaError, match="unsupported_region"):
            source.inspect_remaining(objects, MEDIA._Extracted())
