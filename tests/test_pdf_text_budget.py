"""Original PDF content syntax must not masquerade as extracted prose."""

import io
import json

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

from test_media_identity import DENIED, ENGINE, MEDIA, requires_pdf, scan


def drawing_pdf(marker=b"", *, compressed=True):
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    stream = DecodedStreamObject()
    # Distinct coordinates prevent whole-value deduplication from hiding cost.
    commands = b"".join(
        (f"{n}.125 {n + 1}.375 m {n + 2}.625 {n + 3}.875 l S\n").encode()
        for n in range(7000)
    )
    stream.set_data(commands + marker + b"\n")
    page[NameObject("/Contents")] = writer._add_object(
        stream.flate_encode() if compressed else stream
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@requires_pdf
@pytest.mark.parametrize("compressed", [False, True])
def test_drawing_coordinates_do_not_exhaust_prose_budget(compressed):
    data = drawing_pdf(compressed=compressed)
    assert scan(data, "pdf")["status"] == "pass"


@requires_pdf
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("location", ["comment", "literal", "hex", "name", "unknown"])
def test_drawing_projection_keeps_textual_metadata(compressed, location):
    marker = DENIED.encode()
    if location == "comment":
        marker = b"% " + marker
    elif location == "literal":
        marker = b"(" + marker + b") Tj"
    elif location == "hex":
        marker = b"<" + marker.hex().encode() + b"> Tj"
    elif location == "name":
        marker = b"/" + marker.replace(b"@", b"#40") + b" MP"
    assert scan(drawing_pdf(marker, compressed=compressed), "pdf")["status"] == "fail"


@requires_pdf
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("comment", [False, True])
def test_drawing_projection_keeps_utf16_unknown_bytes(codec, compressed, comment):
    marker = (b"% " if comment else b"") + DENIED.encode(codec)
    assert scan(drawing_pdf(marker, compressed=compressed), "pdf")["status"] == "fail"


def test_real_text_limit_remains_enforced():
    output = MEDIA._Extracted()
    with pytest.raises(MEDIA.MediaError, match="text_limit"):
        output.text("a" * (MEDIA.TEXT_LIMIT + 1))


@requires_pdf
def test_multiple_views_of_the_same_source_do_not_become_retained_documents():
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    contents = ArrayObject()
    for index in range(3):
        stream = DecodedStreamObject()
        stream.set_data(b"% " + bytes([65 + index]) * 150000 + b"\n")
        contents.append(writer._add_object(stream.flate_encode()))
    page[NameObject("/Contents")] = contents
    output = io.BytesIO()
    writer.write(output)
    assert scan(output.getvalue(), "pdf")["status"] == "pass"


@requires_pdf
@pytest.mark.parametrize(
    "data,expected", [(b"\x80", "pass"), (DENIED.encode(), "fail")]
)
def test_font_cid_membership_stream_keeps_byte_observations(data, expected):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    bitmap = DecodedStreamObject()
    bitmap.set_data(data)
    descriptor = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/CIDSet"): writer._add_object(bitmap.flate_encode()),
        }
    )
    writer.root_object[NameObject("/Probe")] = writer._add_object(descriptor)
    output = io.BytesIO()
    writer.write(output)
    assert scan(output.getvalue(), "pdf")["status"] == expected


@requires_pdf
def test_cid_membership_cannot_override_content_role():
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    stream = DecodedStreamObject()
    stream.set_data(
        b"/Span << /ActualText <" + DENIED.encode().hex().encode() + b"> >> BDC EMC\n"
    )
    ref = writer._add_object(stream.flate_encode())
    page[NameObject("/Contents")] = ref
    writer.root_object[NameObject("/Probe")] = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/FontDescriptor"),
                NameObject("/CIDSet"): ref,
            }
        )
    )
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="unsupported_font"):
        scan(output.getvalue(), "pdf")


@pytest.mark.parametrize(
    "sources", [None, [1], ["!not-base64!"], [""], ["YQ=="] * (MEDIA.OBJECT_LIMIT + 1)]
)
def test_invalid_compact_sources_cannot_gain_a_clean_verdict(monkeypatch, sources):
    packet = {"texts": [], "images": [], "versions": {}, "pdf_byte_texts": sources}
    monkeypatch.setattr(
        MEDIA, "_run", lambda *a, **kw: (0, json.dumps(packet).encode(), b"")
    )
    with pytest.raises(ValueError, match="private_identity_media_"):
        MEDIA.scan_private_media(
            b"synthetic input",
            [DENIED],
            kind="pdf",
            check_text=ENGINE.scan_private_text,
            check_image=lambda *a: {},
        )


def test_compact_sources_remain_bounded():
    output = MEDIA._Extracted()
    with pytest.raises(MEDIA.MediaError, match="text_limit"):
        output.pdf_bytes(b"a" * (MEDIA.TEXT_LIMIT + 1))
