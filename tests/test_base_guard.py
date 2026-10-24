"""base-guard のカナリア。比較元（保護された側）の呼び出し定義で、uses の @ と preflight-ref がそろうかを見る。

値はすべて仮の SHA。GitHub には何も送らない。
BASE_GUARD_SCRIPT で差し替えると、何もしない版で落ちることを確かめられる。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = os.environ.get("BASE_GUARD_SCRIPT") or str(ROOT / "scripts/base_guard.py")
A = "a" * 40
B = "b" * 40
GATE = "nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate.yml"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def caller(uses_ref=A, preflight_ref=A, *, job="gate", with_ref=True, quote=""):
    lines = [
        "name: ci",
        "on: [pull_request]",
        "jobs:",
        "  test:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - run: echo ok",
        f"  {job}:",
        f"    uses: {quote}{GATE}@{uses_ref}{quote} # v1.0.0",
    ]
    if with_ref:
        lines += [
            "    with:",
            f"      preflight-ref: {quote}{preflight_ref}{quote}  # 同じ SHA",
        ]
    else:
        lines += ["    with:", "      baseline: .repo-preflight/ratchet-baseline.json"]
    return "\n".join(lines) + "\n"


def _commit(repo: Path, files: dict[str, str | None], msg: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg, "--allow-empty")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "caller"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "fixture")
    _git(path, "config", "user.email", "fixture@example.invalid")
    _commit(path, {"README.md": "x\n"}, "root")
    return path


def run(repo, base, *extra):
    return subprocess.run(
        [sys.executable, SCRIPT, "--repo", str(repo), "--base-ref", base, *extra],
        capture_output=True,
        text=True,
    )


# --- 対照（通る） ---


def test_control_matching_sha_passes(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller()}, "base")
    result = run(repo, base)
    assert result.returncode == 0, result.stderr
    assert "そろっています" in result.stdout


def test_control_quoted_values_and_yaml_extension_pass(repo):
    base = _commit(repo, {".github/workflows/gate.yaml": caller(quote="'")}, "base")
    assert run(repo, base).returncode == 0


def test_control_bad_head_does_not_matter_when_base_is_good(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller()}, "base")
    _commit(repo, {".github/workflows/ci.yml": caller(A, B)}, "head breaks it")
    assert run(repo, base).returncode == 0


def test_control_allow_absent_passes_before_adoption(repo):
    base = _git(repo, "rev-parse", "HEAD")
    result = run(repo, base, "--allow-absent")
    assert result.returncode == 0
    assert "導入前" in result.stdout


# --- 落ちるカナリア ---


def test_mismatch_is_blocked_without_printing_sha(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller(A, B)}, "base")
    result = run(repo, base)
    assert result.returncode == 1
    assert "違う SHA" in result.stderr
    assert (
        A not in result.stdout + result.stderr
        and B not in result.stdout + result.stderr
    )


def test_uses_branch_name_is_blocked(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller("main", A)}, "base")
    result = run(repo, base)
    assert result.returncode == 1
    assert "uses の @ が 40桁" in result.stderr


def test_preflight_ref_tag_is_blocked(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller(A, "v1.0.0")}, "base")
    result = run(repo, base)
    assert result.returncode == 1
    assert "preflight-ref が 40桁" in result.stderr


def test_missing_preflight_ref_is_blocked(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller(with_ref=False)}, "base")
    result = run(repo, base)
    assert result.returncode == 1
    assert "preflight-ref が無い" in result.stderr


def test_short_sha_is_blocked(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller(A[:7], A[:7])}, "base")
    assert run(repo, base).returncode == 1


def test_reads_base_not_head_when_head_fixes_it(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller(A, B)}, "base is bad")
    _commit(repo, {".github/workflows/ci.yml": caller()}, "head looks fine")
    assert run(repo, base).returncode == 1


def test_reads_base_not_worktree(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller(A, B)}, "base is bad")
    (repo / ".github/workflows/ci.yml").write_text(caller(), encoding="utf-8")
    assert run(repo, base).returncode == 1


def test_one_bad_job_among_several_is_blocked(repo):
    second = "\n".join(caller(A, B, job="gate2").splitlines()[7:]) + "\n"
    base = _commit(repo, {".github/workflows/ci.yml": caller() + second}, "base")
    result = run(repo, base)
    assert result.returncode == 1
    assert "gate2" in result.stderr


def test_no_caller_on_base_is_blocked_by_default(repo):
    base = _git(repo, "rev-parse", "HEAD")
    result = run(repo, base)
    assert result.returncode == 1
    assert "呼び出しが無い" in result.stderr


def test_unreadable_base_is_tool_error(repo):
    result = run(repo, "0" * 40)
    assert result.returncode == 2
    assert "読めない" in result.stderr


# --- 配線 ---


def test_reusable_workflow_runs_base_guard_on_event_base():
    text = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    assert (
        "tool/scripts/base_guard.py --repo target --base-ref refs/remotes/origin/preflight-event-base --allow-absent"
        in text
    )
    step = text.split("name: Check base workflow pins", 1)[1].split("- name:", 1)[0]
    assert "github.event.before" in step  # 初回 push だけ見送る


# --- repo-preflight 本体の自己検査（./ で PR の版の gate を呼ぶ形） ---

SELF_REPO = "nexus-ai-2045/repo-preflight"


def self_caller(uses="./.github/workflows/preflight-gate.yml", ref="${{ github.sha }}"):
    return (
        "name: self\n"
        "on: [pull_request]\n"
        "jobs:\n"
        "  preflight:\n"
        f"    uses: {uses}\n"
        "    with:\n"
        f"      preflight-ref: {ref}\n"
        "      baseline: .repo-preflight/ratchet-baseline.json\n"
    )


def test_self_caller_passes_only_in_repo_preflight_itself(repo):
    base = _commit(repo, {".github/workflows/rp.yml": self_caller()}, "base")
    result = run(repo, base, "--self-repository", SELF_REPO)
    assert result.returncode == 0, result.stderr
    assert "1 件" in result.stdout


def test_self_repository_match_ignores_case(repo):
    base = _commit(repo, {".github/workflows/rp.yml": self_caller()}, "base")
    assert run(repo, base, "--self-repository", SELF_REPO.upper()).returncode == 0


@pytest.mark.parametrize(
    "name",
    [
        None,
        "someone/repo-preflight",
        "nexus-ai-2045/repo-preflight-fork",
        "nexus-ai-2045/other",
    ],
)
def test_self_caller_is_foreign_outside_repo_preflight(repo, name):
    base = _commit(repo, {".github/workflows/rp.yml": self_caller()}, "base")
    extra = () if name is None else ("--self-repository", name)
    result = run(repo, base, *extra)
    assert result.returncode == 1
    assert "完全一致しない" in result.stderr


@pytest.mark.parametrize(
    "uses,ref",
    [
        ("./.github/workflows/preflight-gate.yml", "main"),
        ("./.github/workflows/preflight-gate.yml", "a" * 40),
        ("./.github/workflows/preflight-gate.yml@main", "${{ github.sha }}"),
        ("./.github/workflows/Preflight-Gate.yml", "${{ github.sha }}"),
        ("./.github/workflows/other/preflight-gate.yml", "${{ github.sha }}"),
    ],
)
def test_self_caller_other_shapes_still_blocked_in_repo_preflight(repo, uses, ref):
    base = _commit(repo, {".github/workflows/rp.yml": self_caller(uses, ref)}, "base")
    result = run(repo, base, "--self-repository", SELF_REPO)
    assert result.returncode == 1, result.stdout


def test_self_repository_does_not_relax_pinned_callers(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller(A, B)}, "base")
    result = run(repo, base, "--self-repository", SELF_REPO)
    assert result.returncode == 1
    assert "違う SHA" in result.stderr


def test_repo_preflight_own_caller_passes_base_guard():
    if subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True
    ).returncode:
        pytest.skip("git の作業木ではない")
    result = run(ROOT, "HEAD", "--allow-absent", "--self-repository", SELF_REPO)
    assert result.returncode == 0, result.stderr


def test_reusable_workflow_passes_github_repository_to_base_guard():
    text = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    assert "SELF_REPOSITORY: ${{ github.repository }}" in text
    assert '--self-repository "$SELF_REPOSITORY"' in text
