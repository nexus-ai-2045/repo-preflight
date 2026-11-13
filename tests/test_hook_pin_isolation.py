"""Explicit hook pins are checked independently of the caller's PATH."""

import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import identity_gate


def test_explicit_pin_is_revalidated_without_path_fallback(tmp_path, monkeypatch):
    discovered = tmp_path / "gitleaks"
    explicit = tmp_path / "configured" / "gitleaks"
    explicit.parent.mkdir()
    for binary in (discovered, explicit):
        binary.write_bytes(b"fixture")
        binary.chmod(0o755)
    pins = tmp_path / "pins.json"
    pins.write_text(
        json.dumps({"binary_sha256": {"test": hashlib.sha256(b"fixture").hexdigest()}})
    )
    monkeypatch.setattr(identity_gate, "GITLEAKS_PINS", pins)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("REPO_PREFLIGHT_GITLEAKS", raising=False)
    assert identity_gate.pinned_gitleaks() == str(discovered.resolve())
    monkeypatch.setenv("REPO_PREFLIGHT_GITLEAKS", str(explicit))
    assert identity_gate.pinned_gitleaks() == str(explicit.resolve())
    explicit.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="^gitleaks_unpinned$"):
        identity_gate.pinned_gitleaks()
    explicit.unlink()
    with pytest.raises(RuntimeError, match="^gitleaks_missing$"):
        identity_gate.pinned_gitleaks()
