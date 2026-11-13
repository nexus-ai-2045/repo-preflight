"""repo-preflight を repo-preflight 自身へ全 intent で実行する保証テスト。

codex review (PR #16) 指摘の再発防止:
merge / publish / release などの文書化済みコマンドが、対象 repo としての
repo-preflight 自身に使えなくなる退行 (change_sensitive_scope_unavailable や
空 diff での history inventory 失敗) を検知する。

検査するのは scan 層が成立するかどうかだけ。blocked (人間確認待ち) は
設計どおりの正常系として扱い、tool_error だけを退行とみなす。
"""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "readiness_scan.py"

DOCUMENTED_INTENTS = [
    ("create_repo", []),
    ("push", ["--base-ref", "origin/main"]),
    ("open_pr", ["--base-ref", "origin/main"]),
    ("merge", ["--base-ref", "origin/main"]),
    ("publish", ["--audience", "public", "--consistency-base-ref", "origin/main"]),
    ("release", ["--consistency-base-ref", "origin/main"]),
]


def _origin_main_is_usable_base() -> bool:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
        cwd=REPO,
        capture_output=True,
    )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "refs/remotes/origin/main", "HEAD"],
        cwd=REPO,
        capture_output=True,
    )
    return probe.returncode == 0 and ancestor.returncode == 0


def _require_real_base():
    if _origin_main_is_usable_base():
        return
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail(
            "refs/remotes/origin/main が CI checkout に無い、または HEAD の祖先ではない。"
            "fetch-depth: 0 と比較元を確認する"
        )
    pytest.skip("origin/main が無い、または HEAD の祖先ではない環境")


def _git(repo, *args, input=None, env=None):
    return subprocess.run(
        ["git", *args], cwd=repo, input=input, env=env, capture_output=True, check=True
    ).stdout


def _build_bounded_self_repository(repo):
    """現在HEADの全追跡treeを保ち、履歴だけを独立した2 commitにする。"""
    _require_real_base()
    archive = _git(REPO, "-c", "core.autocrlf=false", "archive", "--format=tar", "HEAD")
    executable = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        for member in files:
            path = PurePosixPath(member.name)
            assert not path.is_absolute() and ".." not in path.parts
            assert all(":" not in part and "\\" not in part for part in path.parts)
            target = repo.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            assert member.isfile(), "fixture only accepts regular tracked files"
            source = files.extractfile(member)
            assert source is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            if member.mode & 0o111:
                executable.append(path.as_posix())
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "core.autocrlf", "false")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://github.com/nexus-ai-2045/repo-preflight.git",
    )
    _git(repo, "-c", "core.autocrlf=false", "add", "--all")
    if executable:
        _git(repo, "update-index", "--chmod=+x", "--", *executable)
    tree = _git(repo, "write-tree").strip()
    assert tree == _git(REPO, "rev-parse", "HEAD^{tree}").strip()
    identity = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        identity[f"GIT_{role}_NAME"] = "nexus-ai-2045"
        identity[f"GIT_{role}_EMAIL"] = "nexus-ai-2045@users.noreply.github.com"
    first = _git(
        repo, "commit-tree", tree.decode(), input=b"Fixture base\n", env=identity
    ).strip()
    second = _git(
        repo,
        "commit-tree",
        tree.decode(),
        "-p",
        first.decode(),
        input=b"Fixture head\n",
        env=identity,
    ).strip()
    _git(repo, "update-ref", "refs/heads/main", second.decode())
    _git(repo, "update-ref", "refs/remotes/origin/main", second.decode())
    return repo


@pytest.fixture(scope="module")
def bounded_self_repository(tmp_path_factory):
    return _build_bounded_self_repository(tmp_path_factory.mktemp("bounded-self"))


def test_bounded_self_preserves_tree_with_caller_autocrlf(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    assert _git(REPO, "config", "--bool", "core.autocrlf").strip() == b"true"
    repo = tmp_path / "repository"
    repo.mkdir()
    _build_bounded_self_repository(repo)
    assert _git(repo, "rev-parse", "HEAD^{tree}") == _git(
        REPO, "rev-parse", "HEAD^{tree}"
    )
    assert not _git(repo, "status", "--porcelain").strip()


def test_bounded_self_preserves_full_tree_noninitial_history_and_empty_diff(
    bounded_self_repository,
):
    repo = bounded_self_repository
    assert _git(repo, "rev-parse", "HEAD^{tree}") == _git(
        REPO, "rev-parse", "HEAD^{tree}"
    )
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == b"2"
    assert _git(repo, "rev-parse", "HEAD^{tree}") == _git(
        repo, "rev-parse", "HEAD~1^{tree}"
    )
    assert _git(repo, "rev-parse", "origin/main") == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "remote", "get-url", "origin").strip() == (
        b"https://github.com/nexus-ai-2045/repo-preflight.git"
    )
    assert not (repo / ".git" / "objects" / "info" / "alternates").exists()
    assert _git(repo, "rev-parse", "--is-shallow-repository").strip() == b"false"
    assert not _git(repo, "diff", "--name-only", "origin/main...HEAD").strip()
    assert not _git(repo, "status", "--porcelain").strip()


def _run_scan(repo: Path, intent=None, extra=()):
    command = [sys.executable, str(SCRIPT), "--repo", str(repo)]
    if intent:
        command.extend(["--intent", intent])
    result = subprocess.run(
        [*command, *extra], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.stdout, "self scan did not produce a report"
    report = json.loads(result.stdout)
    assert result.returncode != 2, report
    return report


def _assert_scan(scan, *, mode, consistency_base=False):
    assert scan["status"] != "tool_error", scan.get("issues", scan)
    consistency = scan["checks"]["repository_consistency"]
    assert consistency["status"] == "pass", consistency
    assert scan["scan_scope"]["mode"] == mode
    if consistency_base:
        assert scan["consistency_scope"]["mode"] == "target_diff"
        assert scan["consistency_scope"]["base_ref"] == "origin/main"
        assert scan["consistency_scope"]["base_oid"]


@pytest.mark.parametrize(("intent", "extra"), DOCUMENTED_INTENTS)
def test_documented_intents_stay_usable_against_this_repository(
    bounded_self_repository, intent, extra
):
    report = _run_scan(bounded_self_repository, intent, extra)
    assert report.get("schema") == "repo-preflight.dialogue/v3"
    assert report.get("intent") == intent
    if intent == "create_repo":
        assert report["status"] in {
            "needs_human_input",
            "blocked",
            "ready_after_confirmation",
        }
        assert report.get("proposals")
    _assert_scan(
        report["scan"],
        mode="target_diff" if intent in {"push", "open_pr", "merge"} else "repository",
        consistency_base=intent in {"publish", "release"},
    )


def test_shortest_plain_scan_does_not_tool_error_on_self(bounded_self_repository):
    report = _run_scan(bounded_self_repository)
    _assert_scan(report, mode="repository", consistency_base=True)
    assert "change_sensitive_scope_unavailable" not in report["checks"][
        "repository_consistency"
    ].get("findings", [])


def test_real_checkout_release_preserves_full_history_scan():
    if os.environ.get("REPO_PREFLIGHT_DEEP_SELF_TEST") != "1":
        pytest.skip("実checkout全履歴: REPO_PREFLIGHT_DEEP_SELF_TEST=1 で明示実行")
    _require_real_base()
    assert _git(REPO, "rev-parse", "--is-shallow-repository").strip() == b"false"
    report = _run_scan(REPO, "release", ["--consistency-base-ref", "origin/main"])
    assert report["schema"] == "repo-preflight.dialogue/v3"
    assert report["intent"] == "release"
    _assert_scan(report["scan"], mode="repository", consistency_base=True)


def test_ci_missing_real_base_fails_instead_of_skipping(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _git(tmp_path, "init", "--initial-branch=main")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    assert not _origin_main_is_usable_base()
    with pytest.raises(pytest.fail.Exception, match="CI checkout"):
        _require_real_base()


def test_deep_self_scan_is_enabled_only_for_representative_linux_job():
    import yaml

    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text("utf-8"))
    enabled = []
    for job_id, job in workflow["jobs"].items():
        for step in job["steps"]:
            flag = step.get("env", {}).get("REPO_PREFLIGHT_DEEP_SELF_TEST")
            if flag is not None:
                enabled.append((job_id, step, flag))
    assert len(enabled) == 1
    job_id, step, flag = enabled[0]
    assert job_id == "test"
    assert workflow["jobs"][job_id]["runs-on"] == "ubuntu-latest"
    assert {
        entry["python-version"]
        for entry in workflow["jobs"][job_id]["strategy"]["matrix"]["include"]
    } == {"3.11", "3.13"}
    assert flag == "${{ matrix.python-version == '3.11' && '1' || '0' }}"
    assert "scripts/local_check.py --ci" in step["run"]
