"""合成fixtureの自己承認と旧形式への切替を実Gitで拒否する。"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from synthetic_paths import synthetic_home_path
from test_readiness_scan import MODULE, git, make_repo
from test_reviewed_path_exceptions import policy as path_policy
from test_reviewed_secret_exceptions import policy as secret_policy
from test_preflight_ratchet import r as ratchet
import base_guard
from test_base_guard import caller


@pytest.fixture
def visibility(monkeypatch):
    def set_visibility(private=False):
        def get(_):
            return {
                "full_name": "example/repo",
                "private": private,
                "visibility": "private" if private else "public",
            }

        monkeypatch.setattr(
            MODULE,
            "load_github_settings_module",
            lambda: SimpleNamespace(gh_api_get=get),
        )

    set_visibility()
    return set_visibility


def legacy_policy(tmp_path, version):
    data = ("sk-" + "a" * 24).encode()
    config = secret_policy(tmp_path, data)
    doc = json.loads(config.read_text())
    doc["version"] = version
    if version == 2:
        doc["entries"][0].update(
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            review_reference="review:private-synthetic-case",
        )
    config.write_text(json.dumps(doc))
    return config


@pytest.mark.parametrize("version", [1, 2])
def test_public_cannot_downgrade_to_secret_exception(tmp_path, visibility, version):
    config = legacy_policy(tmp_path, version)
    with pytest.raises(RuntimeError, match="reviewed_secret_exceptions_invalid"):
        MODULE.ReviewedSecretExceptions(config, "https://github.com/example/repo.git")


@pytest.mark.parametrize("version", [1, 2])
def test_private_legacy_exception_remains_explicit(tmp_path, visibility, version):
    visibility(True)
    config = legacy_policy(tmp_path, version)
    reviewed = MODULE.ReviewedSecretExceptions(
        config, "https://github.com/example/repo.git"
    )
    assert not reviewed.has_unreviewed("reviewed.txt", ("sk-" + "a" * 24).encode())


@pytest.mark.parametrize("live", [None, {}, "offline", {"full_name": "other/repo"}])
def test_legacy_exception_requires_live_proof(tmp_path, monkeypatch, live):
    def get(_):
        if live == "offline":
            raise RuntimeError("offline")
        return live

    monkeypatch.setattr(
        MODULE, "load_github_settings_module", lambda: SimpleNamespace(gh_api_get=get)
    )
    with pytest.raises(RuntimeError, match="reviewed_secret_exceptions_invalid"):
        MODULE.ReviewedSecretExceptions(
            legacy_policy(tmp_path, 1), "https://github.com/example/repo.git"
        )


@pytest.fixture
def approved_public(tmp_path, monkeypatch, visibility):
    data = synthetic_home_path("windows", "My Name/example").encode() + b"\n"
    config = path_policy(tmp_path, data)
    doc = json.loads(config.read_text())
    doc["version"] = 3
    config.write_text(json.dumps(doc))
    tool_parent = tmp_path / "trusted"
    tool_parent.mkdir()
    tool = make_repo(tool_parent)
    bundled = tool / ".repo-preflight/reviewed-synthetic-fixtures.json"
    bundled.parent.mkdir()
    bundled.write_text(json.dumps(doc))
    git(tool, "add", ".")
    git(tool, "commit", "-m", "review synthetic fixture")
    original_loader = MODULE.trusted_public_fixture_policy

    def load_committed_policy():
        with monkeypatch.context() as context:
            context.setattr(MODULE, "__file__", str(tool / "scripts/readiness_scan.py"))
            return original_loader()

    monkeypatch.setattr(MODULE, "trusted_public_fixture_policy", load_committed_policy)
    return data, config, tool, bundled


def test_public_fixture_uses_committed_tool_policy(approved_public):
    data, config, _, bundled = approved_public
    bundled.write_text('{"version": 1, "entries": []}')
    reviewed = MODULE.ReviewedSecretExceptions(
        config, "https://github.com/example/repo.git"
    )
    assert not reviewed.has_unreviewed_paths("reviewed.txt", data)
    assert not reviewed.has_unreviewed_paths(
        "reviewed.txt", data.replace(b"\n", b"\r\n")
    )


@pytest.mark.parametrize("change", ["hash", "entry", "path"])
def test_data_and_policy_cannot_self_approve(approved_public, change):
    data, config, _, _ = approved_public
    doc = json.loads(config.read_text())
    if change == "hash":
        doc["entries"][0]["content_sha256"] = hashlib.sha256(
            data + b"extra"
        ).hexdigest()
    elif change == "entry":
        extra = dict(doc["entries"][0], id="self-approved", path="extra.txt")
        doc["entries"].append(extra)
    else:
        doc["entries"][0]["path"] = "elsewhere.txt"
    config.write_text(json.dumps(doc))
    with pytest.raises(RuntimeError, match="reviewed_secret_exceptions_invalid"):
        MODULE.ReviewedSecretExceptions(config, "https://github.com/example/repo.git")


def test_missing_committed_policy_never_uses_target_copy(approved_public):
    _, config, tool, bundled = approved_public
    git(tool, "rm", str(bundled.relative_to(tool)))
    git(tool, "commit", "-m", "remove policy")
    with pytest.raises(RuntimeError, match="reviewed_secret_exceptions_invalid"):
        MODULE.ReviewedSecretExceptions(config, "https://github.com/example/repo.git")


@pytest.mark.parametrize("change", ["duplicate_key", "version_type", "count_type"])
def test_malformed_bundled_policy_is_not_equal_to_valid_target(approved_public, change):
    _, config, tool, bundled = approved_public
    doc = json.loads(config.read_text())
    if change == "version_type":
        doc["version"] = 3.0
    elif change == "count_type":
        doc["entries"][0]["occurrence_count"] = True
    text = json.dumps(doc)
    if change == "duplicate_key":
        text = text.replace('"version": 3', '"version": 1, "version": 3', 1)
    bundled.write_text(text)
    git(tool, "add", ".")
    git(tool, "commit", "-m", "malformed bundled policy")
    with pytest.raises(RuntimeError, match="reviewed_secret_exceptions_invalid"):
        MODULE.ReviewedSecretExceptions(config, "https://github.com/example/repo.git")


@pytest.mark.parametrize("change", ["data", "path", "count", "secret"])
def test_changed_payload_never_inherits_fixture_exception(approved_public, change):
    data, config, _, _ = approved_public
    reviewed = MODULE.ReviewedSecretExceptions(
        config, "https://github.com/example/repo.git"
    )
    path = "elsewhere.txt" if change == "path" else "reviewed.txt"
    changed = data
    if change == "data":
        changed += b" extra"
    elif change == "count":
        changed += b" " + data
    elif change == "secret":
        changed += (" sk-" + "a" * 24).encode()
    assert reviewed.has_unreviewed_paths(path, changed)
    if change == "secret":
        assert reviewed.has_unreviewed(path, changed)


def test_target_policy_is_not_automatically_used_by_ci_scan(tmp_path):
    repo = make_repo(tmp_path)
    data = ("sk-" + "a" * 24).encode()
    (repo / "reviewed.txt").write_bytes(data)
    source = legacy_policy(tmp_path, 1)
    target = repo / ".repo-preflight/reviewed-synthetic-fixtures.json"
    target.parent.mkdir()
    target.write_bytes(source.read_bytes())
    git(repo, "add", ".")
    git(repo, "commit", "-m", "untrusted self approval")
    result = ratchet.run_scan(repo, [])
    assert result["checks"]["secret_scan"]["status"] == "fail"


@pytest.mark.parametrize("change", ["policy", "workflow_pin"])
def test_base_guard_blocks_policy_or_workflow_self_approval(tmp_path, change):
    repo = make_repo(tmp_path)
    workflow = repo / ".github/workflows/ci.yml"
    workflow.write_text(caller())
    git(repo, "add", ".")
    git(repo, "commit", "-m", "trusted caller")
    git(repo, "tag", "approved-base")
    if change == "policy":
        path = repo / ".repo-preflight/reviewed-synthetic-fixtures.json"
        path.parent.mkdir()
        path.write_text('{"version": 1, "entries": []}')
    else:
        workflow.write_text(caller("b" * 40, "b" * 40))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "try self approval")
    result = base_guard.evaluate(repo, "approved-base", head_ref="HEAD")
    assert any(item["kind"] == "protected_path_changed" for item in result["findings"])
