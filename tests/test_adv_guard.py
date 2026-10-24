"""敵対的レビュー C1〜C3 のカナリア（base-guard の別ワークフロー・保護パス・YAML の読み方）。

値はすべて仮の SHA と伏せ字の owner。GitHub には何も送らない。
BASE_GUARD_SCRIPT で差し替えると、何もしない版で落ちることを確かめられる。
"""

import json
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
TEMPLATE = ROOT / "templates/github-actions/preflight-guard.yml"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict, msg: str) -> str:
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


def good(uses_target=GATE, uses_ref=A, preflight_ref=A):
    return (
        "on: [pull_request]\n"
        "jobs:\n"
        "  gate:\n"
        f"    uses: {uses_target}@{uses_ref}\n"
        "    with:\n"
        f"      preflight-ref: {preflight_ref}\n"
    )


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


def wf(text):
    return {".github/workflows/ci.yml": text}


# --- C3: YAML の読み方と owner/リポの完全一致 ---


def test_c3_control_owner_case_insensitive_exact_passes(repo):
    upper = "Nexus-AI-2045/Repo-Preflight/.github/workflows/preflight-gate.yml"
    base = _commit(repo, wf(good(upper)), "base")
    assert run(repo, base).returncode == 0


@pytest.mark.parametrize(
    "target",
    [
        "zz-evil/repo-preflight/.github/workflows/preflight-gate.yml",
        "nexus-ai-2045/repo-preflight-x/.github/workflows/preflight-gate.yml",
        "nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate-copy.yml",
        "./.github/workflows/preflight-gate.yml",
    ],
)
def test_c3_foreign_caller_is_blocked(repo, target):
    base = _commit(repo, wf(good(target)), "base")
    result = run(repo, base, "--allow-absent")
    assert result.returncode == 1
    assert "完全一致しない" in result.stderr


def test_c3_uppercase_repo_with_mismatch_is_blocked(repo):
    upper = "nexus-ai-2045/Repo-Preflight/.github/workflows/preflight-gate.yml"
    base = _commit(repo, wf(good(upper, A, B)), "base")
    assert run(repo, base, "--allow-absent").returncode == 1


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  gate: {uses: '"
        + GATE
        + "@"
        + A
        + "', with: {preflight-ref: '"
        + B
        + "'}}\n",
        'jobs:\n  gate:\n    "uses": '
        + GATE
        + "@"
        + A
        + '\n    "with":\n      "preflight-ref": '
        + B
        + "\n",
        "jobs:\n  gate:\n    uses: >-\n      "
        + GATE
        + "@"
        + A
        + "\n    with:\n      preflight-ref: "
        + B
        + "\n",
    ],
    ids=["flow", "quoted-keys", "folded"],
)
def test_c3_other_yaml_forms_with_mismatch_are_blocked(repo, text):
    base = _commit(repo, wf(text), "base")
    result = run(repo, base, "--allow-absent")
    assert result.returncode == 1
    assert "違う SHA" in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        good() + "  gate:\n    uses: x\n",  # 重複キー
        "x: &a 1\n" + good() + "y: *a\n",  # アンカー・別名
        "jobs:\n  gate:\n    uses: !!str " + GATE + "@" + A + "\n",  # 明示タグ
        "jobs: [\n",  # 壊れた YAML
    ],
    ids=["duplicate", "alias", "tag", "broken"],
)
def test_c3_unreadable_is_red_even_with_allow_absent(repo, text):
    base = _commit(repo, wf(text), "base")
    result = run(repo, base, "--allow-absent")
    assert result.returncode == 1
    assert "YAML として読めない" in result.stderr
    assert "導入前" not in result.stdout


# --- C1・C2: PR 側の定義と保護パス（--head-ref） ---


def test_c1_control_head_unchanged_definitions_pass(repo):
    base = _commit(repo, wf(good()), "base")
    head = _commit(repo, {"src/app.py": "print(1)\n"}, "ふつうの変更")
    assert run(repo, base, "--head-ref", head).returncode == 0


def test_c1_head_retargets_owner_is_blocked(repo):
    base = _commit(repo, wf(good()), "base")
    evil = "zz-evil/repo-preflight/.github/workflows/preflight-gate.yml"
    head = _commit(repo, wf(good(evil)), "呼び出し先を変える")
    assert run(repo, base, "--head-ref", head).returncode == 1


def test_c1_head_replaces_gate_with_fake_job_is_blocked(repo):
    base = _commit(repo, wf(good()), "base")
    fake = "jobs:\n  base-guard:\n    runs-on: ubuntu-latest\n    steps:\n      - run: 'true'\n"
    head = _commit(repo, wf(fake), "同じ名前の偽 job")
    result = run(repo, base, "--head-ref", head)
    assert result.returncode == 1
    assert "呼び出しが消えた" in result.stderr


def test_c1_head_mismatch_is_blocked(repo):
    base = _commit(repo, wf(good()), "base")
    head = _commit(repo, wf(good(GATE, A, B)), "ずらす")
    assert run(repo, base, "--head-ref", head).returncode == 1


@pytest.mark.parametrize(
    "path",
    [
        ".repo-preflight/identity-policy.json",
        ".gitleaks.toml",
        ".GitHub/workflows/extra.yml",
        ".githooks/pre-push",
        ".repo-preflight/ratchet-baseline.json",
        ".github/CODEOWNERS",
        ".gitattributes",
    ],
)
def test_c2_protected_path_change_is_blocked(repo, path):
    base = _commit(repo, wf(good()), "base")
    head = _commit(repo, {path: "{}\n"}, "検査を変える")
    result = run(repo, base, "--head-ref", head)
    assert result.returncode == 1
    assert "検査を変えるパス" in result.stderr


def test_c2_control_similar_unprotected_path_passes(repo):
    base = _commit(repo, wf(good()), "base")
    head = _commit(repo, {"docs/github-notes.md": "x\n"}, "ふつうの文書")
    assert run(repo, base, "--head-ref", head).returncode == 0


def test_c2_repo_policy_file_no_longer_widens_identities(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import identity_gate as ig

    (tmp_path / ".repo-preflight").mkdir()
    outsider = "Zz Corp <zzzz@zz-corp.example>"
    (tmp_path / ".repo-preflight/identity-policy.json").write_text(
        json.dumps({"identities": [outsider], "allow_home_users": ["zzzz"]}),
        encoding="utf-8",
    )
    policy = ig.load_public_policy(None, str(tmp_path))
    assert outsider not in policy
    assert "zzzz" not in policy.allow_home_users


def test_c2_ci_workflow_does_not_swap_in_repo_policy():
    text = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    assert "identity-policy.json" not in text


# --- C1: pull_request_target の別ワークフロー（ひな形） ---


def _template():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def test_c1_template_runs_on_pull_request_target_only():
    data = _template()
    on = data.get("on", data.get(True))
    assert set(on) == {"pull_request_target"}
    assert data["permissions"] == {"contents": "read"}


def test_c1_template_job_name_matches_required_check():
    data = _template()
    assert list(data["jobs"]) == ["base-guard"]
    assert data["jobs"]["base-guard"]["name"] == "base-guard"


def test_c1_template_never_checks_out_or_runs_pr_code():
    text = TEMPLATE.read_text(encoding="utf-8")
    data = _template()
    steps = data["jobs"]["base-guard"]["steps"]
    for step in steps:
        ref = str((step.get("with") or {}).get("ref", ""))
        assert "head" not in ref, "PR 側を checkout しない"
        run = step.get("run", "")
        assert "pip install -e" not in run and "./" not in run
    assert "python tool/scripts/base_guard.py" in text
    assert "--head-ref" in text
    for step in steps:
        uses = step.get("uses", "")
        if uses:
            assert len(uses.split("@", 1)[1].split()[0]) == 40


def test_c1_template_pins_repo_preflight_sha_from_base_definition():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "repository: nexus-ai-2045/repo-preflight" in text
    assert 'rev-parse HEAD)" = "$PREFLIGHT_REF"' in text
