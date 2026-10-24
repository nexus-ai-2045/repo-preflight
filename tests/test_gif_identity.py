"""GIFの拡張本文・非表示を含むフレームの回帰。実素材は保存しない。"""

import importlib.util
import io
import struct
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from PIL import Image, ImageFile
from path_input_fixture import fixture_path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "gif_engine", ROOT / "scripts/identity_normalization.py"
)
ENGINE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ENGINE)
DENIED = "gif-fixture-private@example.invalid"


def gif(comment=None, values=b"ordinary pixels"):
    image = Image.frombytes("P", (len(values), 1), values)
    image.putpalette([v for x in range(256) for v in (x, x, x)])
    stream = io.BytesIO()
    args = {"format": "GIF", "optimize": False}
    if comment is not None:
        args["comment"] = comment
    image.save(stream, **args)
    return stream.getvalue()


def test_clean_gif_pixels_are_not_source():
    assert (
        ENGINE.scan_private_gif(gif(values=b'("alpha"+"beta";'), [DENIED])["status"]
        == "pass"
    )


def test_decoded_palette_indices_preserve_literal_detection():
    assert (
        ENGINE.scan_private_gif(gif(values=DENIED.encode()), [DENIED])["status"]
        == "fail"
    )


@pytest.mark.parametrize(
    "value", [DENIED, '"gif-fixture-" + "private@example.invalid"']
)
def test_comment_fields_are_scanned(value):
    assert (
        ENGINE.scan_private_gif(gif(comment=value.encode()), [DENIED])["status"]
        == "fail"
    )


def test_comment_is_joined_across_subblocks():
    comment = b" " * 250 + b'"gif-fixture-" + "private@example.invalid"'
    assert ENGINE.scan_private_gif(gif(comment=comment), [DENIED])["status"] == "fail"


def test_metadata_paths_and_url_encoding_are_scanned():
    value = quote(fixture_path("macos", "gif-fixture", "work"), safe="").encode()
    result = ENGINE.scan_private_gif(gif(comment=value), [DENIED])
    assert result["status"] == "fail" and result["metadata_path_match"]


def test_every_animation_frame_is_scanned():
    first = Image.new("P", (len(DENIED), 1), 0)
    first.putpalette([v for x in range(256) for v in (x, x, x)])
    second = Image.frombytes("P", (len(DENIED), 1), DENIED.encode())
    second.putpalette(first.getpalette())
    stream = io.BytesIO()
    first.save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[second],
        duration=[100, 0],
        optimize=False,
    )
    assert ENGINE.scan_private_gif(stream.getvalue(), [DENIED])["status"] == "fail"


@pytest.mark.parametrize(
    "data", [b"", b"GIF89a", gif()[:-1], gif() + b"trailing", gif()[:25]]
)
def test_malformed_gif_is_not_clean(data):
    with pytest.raises(ValueError, match="gif_unverified"):
        ENGINE.scan_private_gif(data, [DENIED])


def test_untrusted_truncated_decoder_setting_is_rejected(monkeypatch):
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    with pytest.raises(ValueError, match="gif_unverified"):
        ENGINE.scan_private_gif(gif(), [DENIED])


def test_image_expansion_is_bounded_before_decode():
    data = bytearray(gif())
    offset = 13 + 3 * (2 ** ((data[10] & 7) + 1))
    data[6:10] = struct.pack("<HH", 65535, 65535)
    data[offset + 5 : offset + 9] = struct.pack("<HH", 65535, 65535)
    with pytest.raises(ValueError, match="gif_size_limit"):
        ENGINE.scan_private_gif(bytes(data), [DENIED])


def test_forced_rgb_decoder_setting_cannot_hide_palette_indices(monkeypatch):
    from PIL import GifImagePlugin

    monkeypatch.setattr(
        GifImagePlugin, "LOADING_STRATEGY", GifImagePlugin.LoadingStrategy.RGB_ALWAYS
    )
    with pytest.raises(ValueError, match="gif_unverified"):
        ENGINE.scan_private_gif(gif(values=DENIED.encode()), [DENIED])
