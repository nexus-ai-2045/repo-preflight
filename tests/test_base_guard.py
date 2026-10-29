"""base-guard のカナリア。比較元（保護された側）の呼び出し定義で、uses の @ と preflight-ref がそろうかを見る。

値はすべて仮の SHA。GitHub には何も送らない。
BASE_GUARD_SCRIPT で差し替えると、何もしない版で落ちることを確かめられる。
"""

from __future__ import annotations

import os
import importlib.util
import json
import types
import hashlib
from datetime import datetime, timedelta, timezone
import subprocess
import sys
from pathlib import Path

import pytest


def guard_module():
    spec = importlib.util.spec_from_file_location("guard_integration", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registered_decision_allows_only_protected_change_finding(repo, monkeypatch):
    _commit(repo, {".github/workflows/ci.yml": caller(A)}, "base")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(
        repo,
        {".github/workflows/tests.yml": "name: tests\non: push\njobs: {}\n"},
        "candidate",
    )
    verifier = types.SimpleNamespace(
        verify_decisions=lambda **kwargs: {
            "status": "approved",
            "reason": "verified",
            "decision_ids": ["review"],
            "assurance": "registered-signature",
        }
    )
    monkeypatch.setitem(sys.modules, "protected_change_decisions", verifier)
    result = guard_module().evaluate(
        repo, base, "HEAD", decision_context={"repository": "example/project"}
    )
    assert result["status"] == "pass"
    assert result["protected_change_decision"]["assurance"] == "registered-signature"


def test_approval_cannot_disable_mandatory_caller(repo, monkeypatch):
    before = caller(A)
    _commit(repo, {".github/workflows/ci.yml": before}, "base")
    base = _git(repo, "rev-parse", "HEAD")
    after = before.replace("    uses:", "    if: false\n    uses:")
    assert before != after
    _commit(repo, {".github/workflows/ci.yml": after}, "disable gate")
    monkeypatch.setitem(
        sys.modules,
        "protected_change_decisions",
        types.SimpleNamespace(
            verify_decisions=lambda **kwargs: {
                "status": "approved",
                "reason": "verified",
            }
        ),
    )
    result = guard_module().evaluate(
        repo, base, "HEAD", decision_context={"repository": "example/project"}
    )
    assert result["status"] == "blocked"
    assert any(x["kind"] == "verification_policy_changed" for x in result["findings"])


def test_unverified_decision_keeps_protection(repo, monkeypatch):
    _commit(repo, {".github/workflows/ci.yml": caller(A)}, "base")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(
        repo,
        {".github/workflows/tests.yml": "name: tests\non: push\njobs: {}\n"},
        "candidate",
    )
    monkeypatch.setitem(
        sys.modules,
        "protected_change_decisions",
        types.SimpleNamespace(
            verify_decisions=lambda **kwargs: {
                "status": "blocked",
                "reason": "trust_unavailable",
            }
        ),
    )
    result = guard_module().evaluate(
        repo, base, "HEAD", decision_context={"repository": "example/project"}
    )
    assert result["status"] == "blocked"
    assert any(x["kind"] == "protected_path_changed" for x in result["findings"])


def test_normal_decision_cannot_replace_self_gate_with_noop(repo, monkeypatch):
    path = ".github/workflows/preflight-gate.yml"
    gate = "name: gate\non: workflow_call\njobs:\n  preflight:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo scanning\n"
    base = _commit(
        repo, {".github/workflows/rp.yml": self_caller(), path: gate}, "base"
    )
    _commit(
        repo,
        {path: gate.replace("echo scanning", "echo skipped")},
        "disable implementation",
    )
    monkeypatch.setitem(
        sys.modules,
        "protected_change_decisions",
        types.SimpleNamespace(
            verify_decisions=lambda **kwargs: {
                "status": "approved",
                "reason": "verified",
            }
        ),
    )
    result = guard_module().evaluate(
        repo,
        base,
        "HEAD",
        self_repository=SELF_REPO,
        decision_context={"repository": SELF_REPO},
    )
    assert result["status"] == "blocked"
    assert any(x["kind"] == "verification_policy_changed" for x in result["findings"])


def test_request_is_unsigned_and_does_not_authorize_the_change(repo):
    base = _commit(repo, {".github/workflows/ci.yml": caller(A)}, "base")
    _commit(
        repo,
        {".github/workflows/tests.yml": "name: tests\non: push\njobs: {}\n"},
        "candidate",
    )
    result = run(
        repo,
        base,
        "--head-ref",
        "HEAD",
        "--repository",
        "example/project",
        "--repository-id",
        "123",
        "--pull-request",
        "7",
        "--base-branch",
        "refs/heads/main",
        "--decision-request",
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "unsigned_request_not_authorization"
    assert data["human_verified"] is False
    assert data["trust_status"] == "unavailable"
    assert len(data["scope"]["protected_delta_sha256"]) == 64
    assert data["scope"]["paths"] == [".github/workflows/tests.yml"]
    assert run(repo, base, "--head-ref", "HEAD").returncode == 1


@pytest.mark.parametrize("mismatch", ["base", "head", "repository", "branch", "closed"])
def test_live_context_rejects_changed_snapshot(monkeypatch, mismatch):
    module = guard_module()
    context = {
        "repository": "example/project",
        "repository_id": 123,
        "pull_request": 7,
        "base_ref": "refs/heads/main",
    }
    data = {
        "state": "open",
        "number": 7,
        "base": {
            "sha": A,
            "ref": "main",
            "repo": {"id": 123, "full_name": "example/project"},
        },
        "head": {"sha": B},
    }
    if mismatch in ("base", "head"):
        data[mismatch]["sha"] = "c" * 40
    elif mismatch == "repository":
        data["base"]["repo"]["id"] = 456
    elif mismatch == "branch":
        data["base"]["ref"] = "other"
    else:
        data["state"] = "closed"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return json.dumps(data).encode()

    monkeypatch.setenv("REPO_PREFLIGHT_CONTEXT_TOKEN", "fixture")
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(module.GuardError, match="live_context"):
        module.assert_live_context(context, A, B)


def test_actual_signature_chain_reused_only_for_same_protected_delta(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "decision_fixture_helpers", ROOT / "tests/test_protected_change_decisions.py"
    )
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    fixture = helpers.fixture.__wrapped__(tmp_path)
    repo, _, _, trust, design, leaf, _, bundle = fixture
    base = helpers.commit(repo, {".github/workflows/ci.yml": caller(A)})
    path = ".github/workflows/tests.yml"
    candidate = helpers.commit(repo, {path: "name: tests\non: push\njobs: {}\n"})
    stamp = datetime.now(timezone.utc)
    for node in (design, leaf):
        node["scope"]["paths"] = [path]
        node["issued_at"] = (stamp - timedelta(minutes=1)).isoformat()
        node["expires_at"] = (stamp + timedelta(hours=1)).isoformat()
    leaf["parent_payload_sha256"] = hashlib.sha256(helpers.canon(design)).hexdigest()
    leaf["scope"]["protected_delta_sha256"] = helpers.protected_delta(
        repo, base, candidate, [path]
    )
    head = bundle()
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = guard_module()
    assert (
        module.evaluate(repo, base, head, decision_context=helpers.CTX)["status"]
        == "pass"
    )
    head = helpers.commit(repo, {"README.md": "非保護ファイルの変更"})
    assert (
        module.evaluate(repo, base, head, decision_context=helpers.CTX)["status"]
        == "pass"
    )
    head = helpers.commit(repo, {path: "name: tests\non: push\njobs: {}\n# changed\n"})
    assert (
        module.evaluate(repo, base, head, decision_context=helpers.CTX)["status"]
        == "blocked"
    )


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
