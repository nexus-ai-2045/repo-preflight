"""個人識別子の本文検査を、内容hashごとの合格控えで省く仕組みの試験。

控えは検査の鍵 (検査器・方針・正規化器・例外・実行環境) と本文のSHA-256で引く。
鍵が1つでも違う・控えが壊れている・権限が緩いときは全体を読み直す (fail-closed)。
"""

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from identity_provider_fixture import configure_private_provider

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readiness_scan.py"
SPEC = importlib.util.spec_from_file_location("readiness_scan_cache_tests", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

MARKER = "synthetic-private-identity-marker"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="控えは POSIX の所有者・権限確認が前提で Windows では無効"
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    empty_excludes = tmp_path / "fixture-empty-excludes"
    empty_excludes.write_text("", encoding="utf-8")
    git(repo, "config", "core.excludesFile", str(empty_excludes))
    git(repo, "config", "user.name", "Test Author")
    git(repo, "config", "user.email", "test-author@example.invalid")
    for name in MODULE.REQUIRED:
        body = f"# {name}\n"
        if name == MODULE.REVIEW_RECORD:
            body += f"{MODULE.REVIEW_RECORD_MARKER}\n"
        (repo / name).write_text(body, encoding="utf-8")
    (repo / "notes.txt").write_text("public cache probe\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", "https://github.com/example/repo.git")
    return repo


@pytest.fixture
def body_checks(monkeypatch):
    """作業treeの本文検査 (defer_unverified_jpeg付きの呼出し) だけを数える。"""
    calls: list[bytes] = []
    original = MODULE.PrivateIdentityScan.check

    def spy(self, data, **kwargs):
        if "defer_unverified_jpeg" in kwargs:
            calls.append(bytes(data))
        return original(self, data, **kwargs)

    monkeypatch.setattr(MODULE.PrivateIdentityScan, "check", spy)
    return calls


def cache_dir() -> Path:
    return Path(os.environ["XDG_CACHE_HOME"]) / "repo-preflight" / "identity-scan"


def private_check(report: dict) -> dict:
    return report["checks"]["private_identity_scan"]


def test_conftest_isolates_cache_home(tmp_path):
    assert Path(os.environ["XDG_CACHE_HOME"]).is_relative_to(tmp_path)


def test_second_scan_skips_body_check_for_same_content(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    first = MODULE.scan(repo, require_private_identity=True)
    assert private_check(first)["status"] == "pass"
    first_calls = len(body_checks)
    assert first_calls == private_check(first)["worktree_inventory_files"] > 0
    body_checks.clear()
    second = MODULE.scan(repo, require_private_identity=True)
    assert body_checks == []
    check = private_check(second)
    assert check["status"] == "pass"
    assert check["worktree_files_cache_reused"] == first_calls
    assert check["worktree_scan_status"] == "complete"
    # 控えを使っても数え方・結論は同じ。
    for name in ("objects_scanned", "finding_count", "worktree_files_scanned"):
        assert check[name] == private_check(first)[name]
    assert private_check(first)["worktree_files_cache_reused"] == 0


def test_one_byte_change_rechecks_only_that_file(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    (repo / "notes.txt").write_text("public cache probf\n", encoding="utf-8")
    body_checks.clear()
    report = MODULE.scan(repo, require_private_identity=True)
    assert body_checks == [b"public cache probf\n"]
    assert private_check(report)["status"] == "pass"


def test_untracked_file_is_checked_then_reused_by_content(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    (repo / "draft.txt").write_text("new untracked body\n", encoding="utf-8")
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert body_checks == [b"new untracked body\n"]


def test_policy_change_invalidates_all_entries(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    first = MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    policy = Path(os.environ["PUBLIC_IDENTITY_DENYLIST"])
    policy.write_text(f"{MARKER}\nanother-synthetic-marker\n", encoding="utf-8")
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total
    assert private_check(first)["status"] == "pass"


def test_scanner_source_change_invalidates_all_entries(
    tmp_path, body_checks, monkeypatch
):
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    monkeypatch.setattr(MODULE, "_scanner_source_sha256", lambda: "0" * 64)
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total


def test_reviewed_exceptions_change_invalidates_all_entries(
    tmp_path, body_checks, monkeypatch
):
    from types import SimpleNamespace

    def private_api(endpoint):
        assert endpoint == "https://api.github.com/repos/example/repo"
        return {"full_name": "example/repo", "private": True, "visibility": "private"}

    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(gh_api_get=private_api),
    )
    repo = make_repo(tmp_path)
    config = tmp_path / "reviewed.json"
    config.write_text(
        json.dumps({"version": 1, "entries": []}),
        encoding="utf-8",
    )
    first = MODULE.scan(
        repo, require_private_identity=True, reviewed_secret_exceptions=config
    )
    assert private_check(first)["status"] == "pass"
    total = len(body_checks)
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total


def test_failing_content_is_never_recorded(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    leaked = f"contact {MARKER}\n".encode()
    (repo / "notes.txt").write_bytes(leaked)
    first = MODULE.scan(repo, require_private_identity=True)
    assert private_check(first)["status"] == "fail"
    body_checks.clear()
    second = MODULE.scan(repo, require_private_identity=True)
    assert leaked in body_checks
    assert private_check(second)["status"] == "fail"
    for path in cache_dir().glob("*.json"):
        assert MARKER.encode() not in path.read_bytes()
        assert hashlib.sha256(leaked).hexdigest() not in path.read_text()


def test_cache_stores_only_hashes_with_private_permissions(tmp_path):
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    directory = cache_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    files = list(directory.glob("*.json"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    text = files[0].read_text(encoding="utf-8")
    assert "public cache probe" not in text
    assert "notes.txt" not in text
    assert str(tmp_path) not in text


@pytest.mark.parametrize(
    "corruption",
    [b"", b"{not json", b'{"version": "other"}', b'{"entries": []}'],
)
def test_broken_cache_falls_back_to_full_scan(tmp_path, body_checks, corruption):
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    [path] = list(cache_dir().glob("*.json"))
    path.write_bytes(corruption)
    body_checks.clear()
    report = MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total
    assert private_check(report)["status"] == "pass"


def test_injected_entry_with_bad_format_rejects_whole_file(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    [path] = list(cache_dir().glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"]["content:not-a-hash"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total


def test_group_writable_cache_file_is_ignored(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    [path] = list(cache_dir().glob("*.json"))
    os.chmod(path, 0o620)
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total


def test_symlinked_cache_file_is_ignored(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    [path] = list(cache_dir().glob("*.json"))
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(path.read_bytes())
    os.chmod(elsewhere, 0o600)
    path.unlink()
    path.symlink_to(elsewhere)
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total


@pytest.mark.parametrize("value", ["0", "off", "false", "no"])
def test_env_disables_cache(tmp_path, body_checks, monkeypatch, value):
    monkeypatch.setenv("REPO_PREFLIGHT_IDENTITY_SCAN_CACHE", value)
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    body_checks.clear()
    report = MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total
    assert private_check(report)["worktree_files_cache_reused"] == 0
    assert not cache_dir().exists() or not list(cache_dir().glob("*.json"))


def test_cache_inside_repository_is_not_used(tmp_path, body_checks, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(repo / ".cache-inside"))
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total
    assert not (repo / ".cache-inside").exists()


def test_relative_cache_home_is_not_used(tmp_path, body_checks, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", "relative-cache")
    monkeypatch.chdir(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    body_checks.clear()
    MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total


def test_external_provider_does_not_use_cache(tmp_path, body_checks, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    assert total > 0
    body_checks.clear()
    report = MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total
    assert private_check(report)["status"] == "pass"


def test_unwritable_cache_home_keeps_scan_passing(tmp_path, body_checks, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocked))
    repo = make_repo(tmp_path)
    first = MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    body_checks.clear()
    second = MODULE.scan(repo, require_private_identity=True)
    assert len(body_checks) == total
    assert private_check(first)["status"] == private_check(second)["status"] == "pass"


def test_base_ref_scan_reuses_entries(tmp_path, body_checks):
    repo = make_repo(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git(repo, "update-ref", "refs/remotes/origin/main", head)
    (repo / "change.txt").write_text("two line\nchange\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")
    MODULE.scan(repo, base_ref="origin/main", require_private_identity=True)
    body_checks.clear()
    report = MODULE.scan(repo, base_ref="origin/main", require_private_identity=True)
    assert body_checks == []
    assert private_check(report)["status"] == "pass"


def test_pass_that_relied_on_reviewed_exception_is_not_recorded(
    tmp_path, body_checks, monkeypatch
):
    from types import SimpleNamespace

    from synthetic_paths import synthetic_home_path
    from test_reviewed_path_exceptions import policy as path_policy

    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(
            gh_api_get=lambda endpoint: {
                "full_name": "example/repo",
                "private": True,
                "visibility": "private",
            }
        ),
    )
    repo = make_repo(tmp_path)
    data = synthetic_home_path("macos", "synthetic-owner").encode()
    (repo / "reviewed.txt").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "reviewed material")
    config = path_policy(tmp_path, data)
    first = MODULE.scan(
        repo, require_private_identity=True, reviewed_secret_exceptions=config
    )
    assert private_check(first)["status"] == "pass"
    assert data in body_checks
    body_checks.clear()
    second = MODULE.scan(
        repo, require_private_identity=True, reviewed_secret_exceptions=config
    )
    assert body_checks == [data]
    assert private_check(second)["status"] == "pass"


def test_loose_cache_directory_disables_the_cache_without_repair(tmp_path, body_checks):
    # 権限が緩いフォルダは自動で直さず、控えを読みも書きもしない
    # (PR #19 のレビュー指摘 3 回: その場で直す処理が古い控えを残す抜け道になった)。
    repo = make_repo(tmp_path)
    MODULE.scan(repo, require_private_identity=True)
    total = len(body_checks)
    nested = cache_dir() / "nested"
    nested.mkdir()
    os.chmod(cache_dir(), 0o770)
    before = sorted(path.name for path in cache_dir().iterdir())
    for _ in range(2):
        body_checks.clear()
        MODULE.scan(repo, require_private_identity=True)
        assert len(body_checks) == total
    assert stat.S_IMODE(cache_dir().stat().st_mode) == 0o770
    assert sorted(path.name for path in cache_dir().iterdir()) == before
