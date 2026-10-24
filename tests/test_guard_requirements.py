"""手元の送信前検査の依存（requirements-guard.txt）と、復号器が無いときに黙って通らないことの回帰。"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "requirements-guard.txt"
DENIED = "guard-fixture-private@example.invalid"

# GIF・PSD の検査に要る固定版。--no-deps で入れるので psd-tools の依存も固定する。
IMAGE_PINS = {
    "pillow": {'python_version < "3.11"', 'python_version >= "3.11"'},
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
    # pyproject の .[images]（Pillow>=11.3,<14 / psd-tools>=1.26,<2）の範囲から外れない。
    for name, version, _, _ in _requirements():
        if name == "pillow":
            assert (11, 3) <= _major_minor(version) < (14, 0)
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
