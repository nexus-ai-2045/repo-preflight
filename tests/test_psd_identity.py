"""PSDの画素とメタデータの区別、および検査失敗の回帰。"""

import importlib.util
import io
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip(
    "psd_tools", minversion="1.26", reason="PSD検査の追加依存はPython 3.11以降で提供"
)
from psd_tools import PSDImage
from psd_tools.psd.image_resources import ImageResource

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "psd_engine", ROOT / "scripts/identity_normalization.py"
)
ENGINE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ENGINE)
DENIED = "psd-fixture-private@example.invalid"


def psd(text=None):
    image = PSDImage.new("RGB", (2, 2), color=(1, 2, 3))
    if text is not None:
        image._record.image_resources[1060] = ImageResource(key=1060, data=text)
    stream = io.BytesIO()
    image.save(stream)
    return stream.getvalue()


def test_clean_psd_is_not_parsed_as_source():
    assert ENGINE.scan_private_psd(psd(), [DENIED])["status"] == "pass"


@pytest.mark.parametrize("text", [DENIED, '"psd-fixture-" + "private@example.invalid"'])
def test_metadata_literal_and_expression_are_detected(text):
    result = ENGINE.scan_private_psd(psd(text.encode()), [DENIED])
    assert result["status"] == "fail"
    assert result["literal_match_count"] > 0


def test_utf16_layer_name_is_detected():
    image = PSDImage.new("RGB", (2, 2))
    from PIL import Image
    from psd_tools.api.layers import PixelLayer

    PixelLayer.frompil(Image.new("RGB", (2, 2)), image, name=DENIED)
    stream = io.BytesIO()
    image.save(stream)
    assert ENGINE.scan_private_psd(stream.getvalue(), [DENIED])["status"] == "fail"


@pytest.mark.parametrize("encoded", [False, True])
def test_metadata_path_is_detected(encoded):
    from path_input_fixture import fixture_path
    from urllib.parse import quote

    text = fixture_path("macos", "psd-fixture", "work")
    if encoded:
        text = quote(text, safe="")
    result = ENGINE.scan_private_psd(psd(text.encode()), [DENIED])
    assert result["status"] == "fail" and result["metadata_path_match"]


@pytest.mark.parametrize("data", [b"", b"8BPS", b"8BPS\0\1" + bytes(40), psd()[:30]])
def test_malformed_psd_is_not_clean(data):
    with pytest.raises(ValueError, match="psd_unverified"):
        ENGINE.scan_private_psd(data, [DENIED])


def test_missing_decoder_is_not_clean():
    import sys

    with patch.dict(sys.modules, {"psd_tools.psd": None}):
        with pytest.raises(ValueError, match="psd_decoder_unavailable"):
            ENGINE.scan_private_psd(psd(), [DENIED])


@pytest.mark.parametrize(
    "case,expected", [("clean", "pass"), ("private", "fail"), ("broken", "tool_error")]
)
def test_readiness_routes_psd_through_container_parser(
    tmp_path, monkeypatch, case, expected
):
    spec = importlib.util.spec_from_file_location(
        "psd_scanner", ROOT / "scripts/readiness_scan.py"
    )
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = tmp_path / "policy.txt"
    policy.write_text(DENIED + "\n")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    raw = psd(DENIED.encode()) if case == "private" else psd()
    if case == "broken":
        raw = raw[:30]
    check = scanner.PrivateIdentityScan(repo, True)
    check.check(raw)
    assert check.result()["status"] == expected


def test_compressed_merged_pixels_keep_literal_detection():
    from PIL import Image

    image = PSDImage.frompil(Image.frombytes("L", (len(DENIED), 1), DENIED.encode()))
    stream = io.BytesIO()
    image.save(stream)
    assert ENGINE.scan_private_psd(stream.getvalue(), [DENIED])["status"] == "fail"


@pytest.mark.parametrize("compression", [1, 2])
def test_invalid_compressed_merged_pixels_are_rejected(compression):
    from psd_tools.constants import Compression

    image = PSDImage.new("RGB", (2, 2))
    image._record.image_data.compression = Compression(compression)
    image._record.image_data.data = b"not-a-valid-compressed-stream"
    stream = io.BytesIO()
    image._record.write(stream)
    with pytest.raises(ValueError, match="psd_unverified"):
        ENGINE.scan_private_psd(stream.getvalue(), [DENIED])


def test_hidden_zip_layer_pixels_are_scanned():
    from psd_tools.constants import Compression
    from psd_tools.api.layers import PixelLayer
    from PIL import Image

    image = PSDImage.new("RGB", (len(DENIED), 1))
    layer = PixelLayer.frompil(Image.new("RGB", (len(DENIED), 1)), image, name="hidden")
    layer.visible = False
    image._record.layer_and_mask_information.layer_info.channel_image_data[0][
        1
    ].set_data(DENIED.encode(), len(DENIED), 1, 8)
    channel = image._record.layer_and_mask_information.layer_info.channel_image_data[0][
        1
    ]
    channel.compression = Compression.ZIP
    channel.set_data(DENIED.encode(), len(DENIED), 1, 8)
    stream = io.BytesIO()
    image.save(stream)
    raw = stream.getvalue()
    assert DENIED.encode() not in raw
    assert ENGINE.scan_private_psd(raw, [DENIED])["status"] == "fail"


def test_hidden_mask_pixels_are_scanned():
    from psd_tools.constants import Compression, ChannelID
    from psd_tools.api.layers import PixelLayer
    from psd_tools.psd.layer_and_mask import MaskData
    from PIL import Image

    image = PSDImage.new("RGB", (len(DENIED), 1))
    PixelLayer.frompil(Image.new("RGB", (len(DENIED), 1)), image, name="masked")
    info = image._record.layer_and_mask_information.layer_info
    record = info.layer_records[0]
    record.channel_info[1].id = ChannelID.USER_LAYER_MASK
    record.mask_data = MaskData(top=0, left=0, bottom=1, right=len(DENIED))
    channel = info.channel_image_data[0][1]
    channel.compression = Compression.ZIP
    channel.set_data(DENIED.encode(), len(DENIED), 1, 8)
    stream = io.BytesIO()
    image._record.write(stream)
    assert ENGINE.scan_private_psd(stream.getvalue(), [DENIED])["status"] == "fail"


@pytest.mark.parametrize("extended", [False, True])
def test_binary_framed_iptc_caption_is_decoded_before_text_scan(extended):
    import struct

    caption = b'"psd-fixture-" + "private@example.invalid"'.ljust(260, b" ")
    length = (
        (b"\x80\x04" + struct.pack(">I", len(caption)))
        if extended
        else struct.pack(">H", len(caption))
    )
    image = PSDImage.new("RGB", (2, 2))
    image._record.image_resources[1028] = ImageResource(
        key=1028, data=b"\x1c\x02\x78" + length + caption
    )
    stream = io.BytesIO()
    image._record.write(stream)
    assert ENGINE.scan_private_psd(stream.getvalue(), [DENIED])["status"] == "fail"


def test_unrecognized_binary_metadata_does_not_drop_printable_caption():
    image = PSDImage.new("RGB", (2, 2))
    image._record.image_resources[4000] = ImageResource(
        key=4000, data=b"\x01\x04" + b'"psd-fixture-" + "private@example.invalid"'
    )
    stream = io.BytesIO()
    image._record.write(stream)
    assert ENGINE.scan_private_psd(stream.getvalue(), [DENIED])["status"] == "fail"


def test_truncated_iptc_record_is_unverified():
    image = PSDImage.new("RGB", (2, 2))
    image._record.image_resources[1028] = ImageResource(
        key=1028, data=b"\x1c\x02\x78\x01\x04short"
    )
    stream = io.BytesIO()
    image._record.write(stream)
    with pytest.raises(ValueError, match="psd_unverified"):
        ENGINE.scan_private_psd(stream.getvalue(), [DENIED])


def test_unknown_metadata_control_whitespace_does_not_split_expression():
    image = PSDImage.new("RGB", (2, 2))
    value = b'"psd-fixture-"\x1f+ "private@example.invalid"'
    image._record.image_resources[4000] = ImageResource(key=4000, data=value)
    stream = io.BytesIO()
    image._record.write(stream)
    assert ENGINE.scan_private_psd(stream.getvalue(), [DENIED])["status"] == "fail"
