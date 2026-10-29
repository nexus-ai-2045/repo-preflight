"""手元の送信前検査の依存（requirements-guard.txt）と、復号器が無いときに黙って通らないことの回帰。"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "requirements-guard.txt"
DENIED = "guard-fixture-private@example.invalid"

# GIF・PSD の検査に要る固定版。--no-deps で入れるので psd-tools の依存も固定する。
IMAGE_PINS = {
    "pillow": {'python_version >= "3.11"'},
    "psd-tools": {'python_version >= "3.11"'},
    "attrs": {'python_version >= "3.11"'},
    "numpy": {'python_version >= "3.11"'},
}


def _requirements() -> list[tuple[str, str, str, list[str]]]:
    text = GUARD.read_text(encoding="utf-8")
    text = re.sub(r"\\\n\s*", " ", text)
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        spec, _, rest = line.partition(" --hash")
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", line)
        req, _, marker = spec.partition(";")
        m = re.fullmatch(r"([A-Za-z0-9._-]+)==([0-9][0-9A-Za-z.]*)", req.strip())
        assert m, f"== で固定していない行: {req.strip()}"
        entries.append((m.group(1).lower(), m.group(2), marker.strip(), hashes))
    return entries


def test_image_decoders_are_pinned_with_hashes():
    entries = _requirements()
    for name, markers in IMAGE_PINS.items():
        found = {marker for n, _, marker, _ in entries if n == name}
        assert found == markers, (name, found)
    for name, version, _, hashes in entries:
        if name in IMAGE_PINS:
            assert hashes, f"{name} に hash が無い"


def _major_minor(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def test_image_pins_stay_inside_the_images_extra():
    # pyproject の .[images]（Pillow>=13,<14 / psd-tools>=1.26,<2）の範囲から外れない。
    for name, version, _, _ in _requirements():
        if name == "pillow":
            assert (13, 0) <= _major_minor(version) < (14, 0)
        if name == "psd-tools":
            assert (1, 26) <= _major_minor(version) < (2, 0)


def _scanner():
    spec = importlib.util.spec_from_file_location(
        "guard_requirements_scanner", ROOT / "scripts/readiness_scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gif() -> bytes:
    return (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
        b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    )


def _scan_without_decoders(tmp_path, monkeypatch, raw: bytes) -> dict:
    scanner = _scanner()
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = tmp_path / "policy.txt"
    policy.write_text(DENIED + "\n", encoding="utf-8")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    blocked = {"PIL": None, "psd_tools": None, "psd_tools.psd": None}
    with patch.dict(sys.modules, blocked):
        check = scanner.PrivateIdentityScan(repo, True)
        check.check(raw)
        return check.result()


def test_gif_without_pillow_is_not_silently_clean(tmp_path, monkeypatch):
    result = _scan_without_decoders(tmp_path, monkeypatch, _gif())
    assert result["status"] == "tool_error"
    assert result["reason"] == "private_identity_gif_decoder_unavailable"


def test_psd_without_psd_tools_is_not_silently_clean(tmp_path, monkeypatch):
    result = _scan_without_decoders(tmp_path, monkeypatch, b"8BPS\x00\x01" + bytes(40))
    assert result["status"] == "tool_error"
    assert result["reason"] == "private_identity_psd_decoder_unavailable"


def _normalizer():
    spec = importlib.util.spec_from_file_location(
        "pillow_floor_normalizer", ROOT / "scripts/identity_normalization.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "declared", ["11.3.0", "12.0.0", "unknown", None, "14.0.0", "13.0.0rc1"]
)
@pytest.mark.parametrize("site", ["gif", "psd", "metadata", "pdf"])
def test_unsafe_pillow_is_rejected_before_any_input_decode(
    monkeypatch, tmp_path, declared, site
):
    from types import SimpleNamespace
    from importlib import metadata
    from scripts import media_metadata, media_identity

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("input decoder must not be called")

    image = SimpleNamespace(open=forbidden, DecompressionBombWarning=Warning)
    pillow = SimpleNamespace(
        __version__=declared,
        Image=image,
        ImageFile=SimpleNamespace(LOAD_TRUNCATED_IMAGES=False),
        GifImagePlugin=SimpleNamespace(LOADING_STRATEGY=0),
    )
    monkeypatch.setitem(sys.modules, "PIL", pillow)
    monkeypatch.setitem(
        sys.modules,
        "psd_tools.psd",
        SimpleNamespace(PSD=SimpleNamespace(read=forbidden)),
    )
    monkeypatch.setattr(metadata, "version", lambda name: declared)
    engine = _normalizer()
    if site == "gif":
        with pytest.raises(
            ValueError, match="private_identity_gif_decoder_unavailable"
        ):
            engine.scan_private_gif(_gif(), [DENIED])
    elif site == "psd":
        with pytest.raises(
            ValueError, match="private_identity_psd_decoder_unavailable"
        ):
            engine.scan_private_psd(b"8BPS\x00\x01" + bytes(40), [DENIED])
    elif site == "metadata":
        with pytest.raises(
            media_metadata.MediaMetadataError, match="dependency_unavailable"
        ):
            media_metadata._image_metadata(tmp_path / "unused.png", ".png", None)
    else:
        monkeypatch.setattr(media_identity, "os", SimpleNamespace(name="test"))
        monkeypatch.setattr(media_identity, "_pdf", forbidden)
        with pytest.raises(media_identity.MediaError, match="dependency_unavailable"):
            media_identity._child("pdf", tmp_path / "unused.pdf")
    assert not calls


@pytest.mark.parametrize("runtime", [(3, 9), (3, 10)])
def test_pillow_runtime_floor_does_not_restrict_text_or_standard_png(
    monkeypatch, runtime
):
    from test_png_identity import png

    engine = _normalizer()
    with monkeypatch.context() as context:
        context.setattr(sys, "version_info", runtime)
        context.setitem(sys.modules, "PIL", None)
        with pytest.raises(ValueError, match="pillow_runtime_unavailable"):
            engine.require_safe_pillow()
        assert engine.scan_private_text("public text", [DENIED])["status"] == "pass"
        assert engine.scan_private_png(png(), [DENIED])["status"] == "pass"


def test_safe_pillow_floor_accepts_only_matching_verified_distribution(monkeypatch):
    from types import SimpleNamespace
    from importlib import metadata

    engine = _normalizer()
    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(__version__="13.0.0"))
    monkeypatch.setattr(sys, "version_info", (3, 11))
    monkeypatch.setattr(metadata, "version", lambda name: "13.0.0")
    assert engine.require_safe_pillow() is None
    monkeypatch.setattr(metadata, "version", lambda name: "12.0.0")
    with pytest.raises(ValueError, match="pillow_runtime_unavailable"):
        engine.require_safe_pillow()


def test_missing_pillow_distribution_is_unverified(monkeypatch):
    from types import SimpleNamespace
    from importlib import metadata

    engine = _normalizer()
    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(__version__="13.0.0"))
    monkeypatch.setattr(sys, "version_info", (3, 11))

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    with pytest.raises(ValueError, match="pillow_runtime_unavailable"):
        engine.require_safe_pillow()


def test_pillow_optional_declarations_keep_core_39_without_unsafe_fallback():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["requires-python"] == ">=3.9"
    assert project["dependencies"] == []
    for extra in ("test", "images", "media"):
        pillow = [
            entry
            for entry in project["optional-dependencies"][extra]
            if entry.startswith("Pillow")
        ]
        assert pillow == ["Pillow>=13,<14; python_version >= '3.11'"]


@pytest.mark.parametrize("mode", ["package", "script"])
@pytest.mark.parametrize("target", ["media_metadata", "media_identity"])
def test_pillow_guard_imports_in_package_and_script_modes(
    monkeypatch, tmp_path, mode, target
):
    from types import SimpleNamespace
    from importlib import metadata, import_module

    monkeypatch.setattr(sys, "version_info", (3, 11))
    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(__version__="12.0.0"))
    monkeypatch.setattr(metadata, "version", lambda name: "12.0.0")
    if mode == "package":
        module = import_module("scripts." + target)
    else:
        monkeypatch.syspath_prepend(str(ROOT / "scripts"))
        spec = importlib.util.spec_from_file_location(
            "standalone_" + target, ROOT / "scripts" / (target + ".py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    if target == "media_metadata":
        with pytest.raises(module.MediaMetadataError, match="dependency_unavailable"):
            module._image_metadata(tmp_path / "unused.png", ".png", None)
    else:
        monkeypatch.setattr(module, "os", SimpleNamespace(name="test"))
        with pytest.raises(module.MediaError, match="dependency_unavailable"):
            module._child("pdf", tmp_path / "unused.pdf")


@pytest.mark.parametrize("target", ["media_metadata", "media_identity"])
def test_isolated_script_loads_shared_guard_before_safe_decoder(
    monkeypatch, tmp_path, target
):
    from types import SimpleNamespace
    from importlib import metadata

    calls = []
    monkeypatch.setattr(sys, "version_info", (3, 11))
    monkeypatch.setattr(metadata, "version", lambda name: "13.0.0")
    monkeypatch.setitem(sys.modules, "identity_normalization", None)

    def opened(*args, **kwargs):
        calls.append(True)
        raise ValueError("synthetic decoder reached")

    monkeypatch.setitem(
        sys.modules,
        "PIL",
        SimpleNamespace(
            __version__="13.0.0",
            Image=SimpleNamespace(open=opened, DecompressionBombWarning=Warning),
        ),
    )
    spec = importlib.util.spec_from_file_location(
        "isolated_" + target, ROOT / "scripts" / (target + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if target == "media_metadata":
        with pytest.raises(module.MediaMetadataError):
            module._image_metadata(tmp_path / "unused.png", ".png", None)
    else:
        monkeypatch.setattr(module, "os", SimpleNamespace(name="test"))
        monkeypatch.setattr(module, "_pdf", lambda *args: calls.append(True))
        module._child("pdf", tmp_path / "unused.pdf")
    assert calls == [True]
