"""Synthetic ICC profiles; no original PDF or profile is copied here."""

import io
import struct

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from test_media_identity import DENIED, MEDIA, requires_pdf, scan


def mluc(*values):
    table = bytearray(b"mluc" + bytes(4) + struct.pack(">II", len(values), 12))
    body = bytearray()
    for text in values:
        value = text.encode("utf-16-be")
        table.extend(
            b"enUS" + struct.pack(">II", len(value), 16 + 12 * len(values) + len(body))
        )
        body.extend(value)
    return bytes(table + body)


def profile(tags=None):
    tags = tags or [
        (b"desc", mluc("Public profile")),
        (b"wtpt", b"XYZ " + bytes(16)),
        (b"chad", b"sf32" + bytes(40)),
        (b"rTRC", b"para" + bytes(4) + struct.pack(">HHI", 0, 0, 65536)),
    ]
    header = bytearray(128)
    header[8] = 4
    header[12:24] = b"mntrRGB XYZ "
    header[36:40] = b"acsp"
    table = bytearray(struct.pack(">I", len(tags)))
    body = bytearray()
    for signature, value in tags:
        body.extend(bytes(-len(body) % 4))
        table.extend(
            signature + struct.pack(">II", 132 + 12 * len(tags) + len(body), len(value))
        )
        body.extend(value)
    result = header + table + body
    struct.pack_into(">I", result, 0, len(result))
    return bytes(result)


def pdf_profile(data, *, referenced=True, compressed=True):
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    stream = DecodedStreamObject()
    stream.set_data(data)
    stream[NameObject("/N")] = NumberObject(3)
    if compressed:
        stream = stream.flate_encode()
    ref = writer._add_object(stream)
    if referenced:
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/ColorSpace"): DictionaryObject(
                    {NameObject("/CS1"): ArrayObject([NameObject("/ICCBased"), ref])}
                )
            }
        )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@requires_pdf
def test_pdf_icc_matrix_profile_is_inspected_without_mutating_input():
    original = pdf_profile(profile())
    assert scan(original, "pdf")["status"] == "pass"
    assert original == pdf_profile(profile())


@requires_pdf
def test_pdf_icc_all_locales_reach_identity_callback():
    data = profile([(b"desc", mluc("Public", DENIED, "Another"))])
    assert scan(pdf_profile(data), "pdf")["status"] == "fail"


@requires_pdf
def test_pdf_icc_locale_codes_reach_identity_callback_when_compressed():
    value = bytearray(mluc("Public"))
    value[16:20] = b"JANE"
    data = profile([(b"desc", bytes(value))])
    assert scan(pdf_profile(data), "pdf", ["JANE"])["status"] == "fail"


def test_shared_locale_strings_are_decoded_once_at_record_limit():
    count = MEDIA.OBJECT_LIMIT
    header = 16 + 12 * count
    value = b"mluc" + bytes(4) + struct.pack(">II", count, 12)
    value += (b"enUS" + struct.pack(">II", 12, header)) * count
    value += "Public".encode("utf-16-be")
    output = MEDIA._Extracted()
    MEDIA._icc_profile(profile([(b"desc", value)]), output, 3)
    assert "Public" in output.texts


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "trailing",
        "overlap",
        "out_of_range",
        "reserved",
        "numeric_tail",
        "locale_gap",
    ],
)
def test_icc_unverified_regions_remain_blocked(mutation):
    data = bytearray(profile())
    if mutation == "unknown":
        data[132:136] = b"priv"
    elif mutation == "trailing":
        data.extend(b"unparsed")
        struct.pack_into(">I", data, 0, len(data))
    elif mutation == "overlap":
        struct.pack_into(">I", data, 148, 184)
    elif mutation == "out_of_range":
        struct.pack_into(">I", data, 136, len(data) + 4)
    elif mutation == "reserved":
        data[100] = 1
    elif mutation == "numeric_tail":
        data = bytearray(profile([(b"wtpt", b"XYZ " + bytes(16) + b"tail")]))
    else:
        value = bytearray(mluc("Public"))
        value.extend(b"hidden")
        data = bytearray(profile([(b"desc", bytes(value))]))
    with pytest.raises(ValueError):
        MEDIA._icc_profile(bytes(data), MEDIA._Extracted(), 3)


@requires_pdf
def test_unreferenced_profile_does_not_gain_stream_exemption():
    with pytest.raises(ValueError, match="unsupported_stream"):
        scan(pdf_profile(profile(), referenced=False), "pdf")
