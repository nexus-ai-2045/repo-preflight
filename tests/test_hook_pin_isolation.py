"""Explicit hook pins are checked independently of the caller's PATH."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import identity_gate


def test_explicit_pin_is_revalidated_without_path_fallback(tmp_path, monkeypatch):
    filename = "gitleaks.exe" if os.name == "nt" else "gitleaks"
    discovered = tmp_path / filename
    explicit = tmp_path / "configured" / filename
    explicit.parent.mkdir()
    executable = Path(sys.executable).resolve()
    original = executable.read_bytes()
    for binary in (discovered, explicit):
        shutil.copyfile(executable, binary)
        binary.chmod(0o755)
    pins = tmp_path / "pins.json"
    pins.write_text(
        json.dumps({"binary_sha256": {"test": hashlib.sha256(original).hexdigest()}})
    )
    monkeypatch.setattr(identity_gate, "GITLEAKS_PINS", pins)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("REPO_PREFLIGHT_GITLEAKS", raising=False)
    assert identity_gate.pinned_gitleaks() == str(discovered.resolve())
    monkeypatch.setenv("REPO_PREFLIGHT_GITLEAKS", str(explicit))
    assert identity_gate.pinned_gitleaks() == str(explicit.resolve())
    explicit.write_bytes(original + b"\nchanged")
    with pytest.raises(RuntimeError, match="^gitleaks_unpinned$"):
        identity_gate.pinned_gitleaks()
    explicit.unlink()
    with pytest.raises(RuntimeError, match="^gitleaks_missing$"):
        identity_gate.pinned_gitleaks()
