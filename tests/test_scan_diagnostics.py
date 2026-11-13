import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from identity_provider_fixture import configure_private_provider

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scan_diagnostics", ROOT / "scripts/readiness_scan.py"
)
scanner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scanner
SPEC.loader.exec_module(scanner)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    work = tmp_path / "repository"
    work.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.name", "nexus_ai"),
        ("config", "user.email", "nexus-ai-2045@users.noreply.github.com"),
        ("config", "commit.gpgsign", "false"),
    ):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps(
            {
                "allow_names": ["nexus_ai"],
                "allow_emails": ["nexus-ai-2045@users.noreply.github.com"],
                "deny_patterns": ["fixture-private-identity"],
            }
        ),
    )
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    return work


def commit(repo, message="fixture"):
    for args in (("add", "."), ("commit", "-qm", message)):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_private_content_location_survives_scan_and_human_report(
    repo, tmp_path, monkeypatch
):
    configure_private_provider(tmp_path, monkeypatch)
    marker = "fixture-private-identity@example.invalid"
    (repo / "notes.txt").write_text("ordinary text\n" + marker + "\n")
    commit(repo)
    report = scanner.scan(repo)
    private = report["checks"]["private_identity_scan"]
    assert private["status"] == "fail"
    assert any(
        item["source"] == "worktree"
        and item["path"] == "notes.txt"
        and item["line"] is None
        for item in private["findings"]
    )
    assert any(item["source"] == "history_object" for item in private["findings"])
    human = scanner.format_human_report(report)
    assert "notes.txt" in human
    assert "line=未確定" in human
    assert marker not in json.dumps(report) + human


def test_private_filename_is_not_repeated_in_diagnostics(repo, tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    marker = "fixture-private-identity@example.invalid"
    (repo / (marker + ".txt")).write_text("ordinary text\n")
    commit(repo)
    report = scanner.scan(repo)
    private = report["checks"]["private_identity_scan"]
    assert any(item["source"] == "filename" for item in private["findings"])
    assert marker not in json.dumps(report) + scanner.format_human_report(report)


def test_human_report_displays_safe_secret_and_path_locations():
    report = {
        "status": "blocked",
        "checks": {
            "secret_scan": {
                "status": "fail",
                "finding_count": 1,
                "locations": ["config.txt"],
            },
            "personal_path_scan": {"status": "fail", "files": ["notes.txt"]},
            "public_attribution": {
                "status": "fail",
                "findings": [
                    {
                        "kind": "個人メールの形",
                        "path": "src/app.py",
                        "line": 12,
                        "source": "staged_diff",
                    }
                ],
                "findings_truncated": 3,
            },
        },
    }
    human = scanner.format_human_report(report)
    assert "config.txt" in human and "notes.txt" in human
    assert 'path="src/app.py"; line=12' in human
    assert "diagnostics truncated=true" in human


def test_private_location_budget_preserves_finding_count():
    probe = object.__new__(scanner.PrivateIdentityScan)
    probe.report = {"status": "complete"}
    probe.objects = 70
    probe.findings = 70
    probe.skipped = 0
    probe.finding_locations = []
    probe.finding_locations_truncated = 0
    for index in range(70):
        probe.record_location("worktree", path=f"file-{index}.txt")
    result = probe.result()
    assert result["finding_count"] == 70
    assert len(result["findings"]) == 50
    assert result["findings_truncated"] == 20


def test_private_gate_preserves_pattern_index_and_message_line(repo):
    marker = "fixture-private-identity"
    (repo / "notes.txt").write_text("ordinary text\n")
    commit(repo, "fixture\n\n" + marker)
    result = scanner._identity_gate_result(repo, None)
    assert result["status"] == "fail"
    assert any(
        item["rule"] == "deny_patterns[0]" and item["line"] == 3
        for item in result["findings"]
    )
    assert marker not in json.dumps(result)


def test_secret_scan_reports_existing_safe_location_without_value(
    repo, tmp_path, monkeypatch
):
    import hashlib

    configure_private_provider(tmp_path, monkeypatch)
    secret = "gh" + "p_" + hashlib.sha256(b"diagnostic-fixture").hexdigest()[:36]
    (repo / "settings.txt").write_text("ordinary text\n" + secret + "\n")
    commit(repo)
    report = scanner.scan(repo)
    check = report["checks"]["secret_scan"]
    assert check["status"] == "fail"
    assert "settings.txt" in check["locations"]
    human = scanner.format_human_report(report)
    assert "settings.txt" in human
    assert secret not in json.dumps(report) + human


@pytest.mark.parametrize("suffix", ["@example.invalid", "\u202e.txt"])
def test_secret_filename_is_hidden_without_private_provider(repo, monkeypatch, suffix):
    import hashlib

    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.delenv("PUBLIC_IDENTITY_DENYLIST", raising=False)
    secret = "gh" + "p_" + hashlib.sha256(b"filename-diagnostic").hexdigest()[:36]
    name = "fixture" + suffix
    (repo / name).write_text(secret + "\n")
    commit(repo)
    report = scanner.scan(repo)
    output = json.dumps(report) + scanner.format_human_report(report)
    assert name not in output
    assert secret not in output
    assert report["checks"]["secret_scan"]["locations"]


def test_diagnostic_paths_use_both_gate_and_scan_credential_rules():
    import hashlib

    secret = "gh" + "p_" + hashlib.sha256(b"short-diagnostic").hexdigest()[:24]
    gate = scanner.load_identity_gate()
    assert scanner._evidence_label_is_sensitive(secret.encode())
    assert gate.Diagnostics().safe_path("config-" + secret + ".txt") is None


def test_history_alias_cannot_enter_diagnostic_label():
    alias = "fixture-person@example.invalid"
    label = "history:" + "a" * 12 + ":" + alias
    assert scanner.diagnostic_evidence_label(label) == "history:" + "a" * 12


def test_private_submission_records_filename_and_blob_locations(
    repo, tmp_path, monkeypatch
):
    configure_private_provider(tmp_path, monkeypatch)
    (repo / "base.txt").write_text("ordinary text\n")
    commit(repo)
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    marker = "fixture-private-identity@example.invalid"
    (repo / (marker + ".txt")).write_text(marker + "\n")
    commit(repo)
    probe = scanner.PrivateIdentityScan(repo, True)
    scanner.history_hits(
        repo, (base + "..HEAD",), private_identity=probe, check_repository_names=True
    )
    details = probe.result()["findings"]
    assert any(item["source"] == "filename" for item in details)
    assert any(
        item["source"] == "history_object" and item["field"].startswith("blob:")
        for item in details
    )
    assert marker not in json.dumps(details)
