"""Compressed PDF metadata must reach callbacks after semantic decoding."""

import io
import sys

import pytest

from test_media_identity import MEDIA, ENGINE, scan, DENIED
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 10), reason="PDF inspection requires Python3.10+"
)


def compressed_pdf(payload, *, cff=False):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    stream = DecodedStreamObject()
    stream.set_data(payload)
    if cff:
        stream[NameObject("/Subtype")] = NameObject(
            "/OpenType" if payload.startswith(b"OTTO") else "/Type1C"
        )
        descriptor = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/FontDescriptor"),
                NameObject("/FontFile3"): writer._add_object(stream.flate_encode()),
            }
        )
        writer._root_object[NameObject("/FixtureFont")] = writer._add_object(descriptor)
    else:
        stream[NameObject("/Type")] = NameObject("/Metadata")
        stream[NameObject("/Subtype")] = NameObject("/XML")
        writer._root_object[NameObject("/Metadata")] = writer._add_object(
            stream.flate_encode()
        )
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def cff_bytes(
    glyph="UnusedPublic", *, notice="Public notice", extra=None, opentype=False
):
    if sys.version_info < (3, 11):
        pytest.skip("Pinned fontTools requires Python3.11+")
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen

    names = [".notdef", "A", glyph]
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({65: "A"})
    charstrings = {name: T2CharStringPen(500, None).getCharString() for name in names}
    builder.setupCFF(
        "PublicFixture",
        {
            "FullName": "PublicFixture",
            "FamilyName": "PublicFixture",
            "Weight": "Regular",
            "Notice": notice,
            "Copyright": extra or "Public copyright",
        },
        charstrings,
        {},
    )
    if opentype:
        builder.setupHorizontalMetrics({name: (500, 0) for name in names})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable(
            {
                "familyName": "PublicFixture",
                "styleName": "Regular",
                "fullName": "PublicFixture",
                "psName": "PublicFixture",
            }
        )
        builder.setupOS2(
            sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200
        )
        builder.setupPost()
        output = io.BytesIO()
        builder.save(output)
        return output.getvalue()
    cff = builder.font["CFF "].cff
    output = io.BytesIO()
    cff.compile(output, builder.font)
    return output.getvalue()


def refs(value):
    return "".join("&#%d;" % ord(c) for c in value)


def test_cff_unused_glyph_name_reaches_identity_callback():
    data = compressed_pdf(cff_bytes(DENIED), cff=True)
    assert DENIED.encode() not in data
    assert scan(data, "pdf")["status"] == "fail"


@pytest.mark.parametrize("role", ["text", "attribute"])
def test_xml_character_references_reach_identity_callback(role):
    encoded = refs(DENIED)
    xml = f'<x creator="{encoded}"/>' if role == "attribute" else f"<x>{encoded}</x>"
    data = compressed_pdf(xml.encode())
    assert DENIED.encode() not in data
    assert scan(data, "pdf")["status"] == "fail"


@pytest.mark.parametrize("role", ["glyph", "notice", "copyright"])
def test_all_cff_string_metadata_roles_reach_callbacks(role):
    options = {
        "glyph": {"glyph": DENIED},
        "notice": {"notice": DENIED},
        "copyright": {"extra": DENIED},
    }[role]
    assert (
        scan(compressed_pdf(cff_bytes(**options), cff=True), "pdf")["status"] == "fail"
    )


@pytest.mark.parametrize("cff", [False, True])
def test_clean_compressed_metadata_passes(cff):
    payload = (
        cff_bytes() if cff else b'<x creator="Public &amp; safe">Public &#65; text</x>'
    )
    assert scan(compressed_pdf(payload, cff=cff), "pdf")["status"] == "pass"


@pytest.mark.parametrize(
    "xml",
    [
        b"<!DOCTYPE x><x/>",
        b'<!DOCTYPE x [<!ENTITY e "value">]><x>&e;</x>',
        b'<!DOCTYPE x SYSTEM "https://example.invalid/private"><x/>',
        b"<x>&unknown;</x>",
        b'<x a="&#0;"/>',
        b"<x>",
        b"<x/><y/>",
        b"<p:x/>",
    ],
)
def test_unverified_xml_refuses(xml):
    with pytest.raises(ValueError, match="private_identity_media_"):
        scan(compressed_pdf(xml), "pdf")


@pytest.mark.parametrize("role", ["text", "attribute", "cff_glyph"])
def test_decoded_values_reach_secret_callback(role):
    secret = "api_key=synthetic_credential_1234567890"
    if role == "cff_glyph":
        data = compressed_pdf(cff_bytes(secret), cff=True)
    else:
        encoded = refs(secret)
        xml = f'<x a="{encoded}"/>' if role == "attribute" else f"<x>{encoded}</x>"
        data = compressed_pdf(xml.encode())
    seen = []

    def text(value, patterns):
        seen.append(value)
        result = ENGINE.scan_private_text(value, patterns)
        if secret in value:
            result.update(secret_match_count=1)
        return result

    result = MEDIA.scan_private_media(
        data,
        [DENIED],
        kind="pdf",
        check_text=text,
        check_image=lambda *args: pytest.fail("no image expected"),
    )
    assert any(secret in value for value in seen)
    assert result["secret_match_count"] > 0


@pytest.mark.parametrize("role", ["text", "attribute", "cff_glyph"])
def test_real_readiness_decoded_secret_metadata_fails(tmp_path, monkeypatch, role):
    from test_readiness_media import setup_scan, MODULE

    secret = "ghp_" + "A" * 24
    if role == "cff_glyph":
        payload = cff_bytes(secret)
    else:
        encoded = refs(secret)
        xml = f'<x a="{encoded}"/>' if role == "attribute" else f"<x>{encoded}</x>"
        payload = xml.encode()
    repo = setup_scan(tmp_path, monkeypatch)
    (repo / "metadata.pdf").write_bytes(
        compressed_pdf(payload, cff=role == "cff_glyph")
    )
    report = MODULE.scan(repo, require_private_identity=True)
    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert report["checks"]["secret_scan"]["finding_count"] >= 1


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_xml_hex_cdata_fragments_and_namespace_values(encoding):
    encoded = "".join("&#x%x;" % ord(c) for c in DENIED)
    for xml in (
        f"<x>{encoded}</x>",
        f"<x>{DENIED[:8]}<![CDATA[{DENIED[8:]}]]></x>",
        f'<x xmlns:p="{encoded}"/>',
    ):
        assert scan(compressed_pdf(xml.encode(encoding)), "pdf")["status"] == "fail"


def test_xml_attribute_and_depth_limits_remain_bounded(monkeypatch):
    monkeypatch.setattr(MEDIA, "TEXT_LIMIT", 80)
    with pytest.raises(MEDIA.MediaError, match="text_limit"):
        MEDIA._pdf_xml_metadata(b'<x a="' + b"A" * 81 + b'"/>', MEDIA._Extracted())
    monkeypatch.setattr(MEDIA, "TEXT_LIMIT", 2000000)
    with pytest.raises(MEDIA.MediaError, match="object_limit"):
        MEDIA._pdf_xml_metadata(b"<x>" * 65 + b"</x>" * 65, MEDIA._Extracted())
    monkeypatch.setattr(MEDIA, "OBJECT_LIMIT", 3)
    with pytest.raises(MEDIA.MediaError, match="object_limit|text_limit"):
        MEDIA._pdf_xml_metadata(b'<x a="a" b="b" c="c"/>', MEDIA._Extracted())


@pytest.mark.parametrize("private", [False, True])
def test_opentype_cff_string_index_metadata(private):
    data = compressed_pdf(
        cff_bytes(extra=DENIED if private else None, opentype=True), cff=True
    )
    assert scan(data, "pdf")["status"] == ("fail" if private else "pass")
