"""scripts/local_check.py：途中の作業は差分に絞り、個人情報ゲートはどのモードでも全差分で回す。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from synthetic_paths import synthetic_home_path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import local_check  # noqa: E402

# 呼び出し元の GIT_DIR などを引き継ぐと一時リポの外を書き換えるので外す
_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


@pytest.fixture(autouse=True)
def _isolated_git_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=_ENV)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "alpha.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "scripts" / "beta.py").write_text("Y = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "import alpha\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_beta.py").write_text("import beta\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# t\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "branch", "base")
    return tmp_path


def _names(steps):
    return [name for name, _ in steps]


def _commit(repo, rel, text):
    (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")


def test_cli_default_runs_full_set(repo):
    out = subprocess.run(
        [
            sys.executable,
            str(Path(local_check.__file__)),
            "--repo",
            str(repo),
            "--base-ref",
            "base",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert out.returncode == 0, out.stderr
    assert "pytest（全部）" in out.stdout
    assert "identity_gate（全差分）" in out.stdout


@pytest.mark.parametrize("mode", ["full", "quick"])
def test_identity_gate_always_covers_whole_diff(repo, mode):
    _commit(repo, "scripts/alpha.py", "X = 2\n")
    steps = dict(local_check.build_steps(repo, mode, "base"))
    cmd = steps["identity_gate（全差分）"]
    base = subprocess.run(
        ["git", "rev-parse", "base"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert cmd[-2:] == ["--range", f"{base}..HEAD"]
    assert "--public" in cmd


@pytest.mark.parametrize("mode", ["full", "quick"])
def test_staged_changes_are_checked_in_every_mode(repo, mode):
    (repo / "scripts" / "alpha.py").write_text("X = 3\n", encoding="utf-8")
    _git(repo, "add", "scripts/alpha.py")
    assert "identity_gate（ステージ）" in _names(
        local_check.build_steps(repo, mode, "base")
    )


def test_quick_runs_only_related_tests(repo):
    _commit(repo, "scripts/alpha.py", "X = 2\n")
    steps = dict(local_check.build_steps(repo, "quick", "base"))
    pytest_steps = [v for k, v in steps.items() if k.startswith("pytest")]
    assert len(pytest_steps) == 1
    assert pytest_steps[0][-1] == "tests/test_alpha.py"
    assert "tests/test_beta.py" not in pytest_steps[0]


def test_quick_falls_back_to_full_tests_for_shared_fixtures(repo):
    _commit(repo, "tests/conftest.py", "\n")
    assert "pytest（全部・安全側）" in _names(
        local_check.build_steps(repo, "quick", "base")
    )


def test_quick_falls_back_when_no_test_mentions_the_script(repo):
    _commit(repo, "scripts/gamma.py", "Z = 1\n")
    assert "pytest（全部・安全側）" in _names(
        local_check.build_steps(repo, "quick", "base")
    )


def test_quick_runs_readme_gate_when_readme_changes(repo):
    _commit(repo, "README.md", "# t2\n")
    names = _names(local_check.build_steps(repo, "quick", "base"))
    assert "readme_release_gate" in names


def test_full_always_runs_readme_gate(repo):
    assert "readme_release_gate" in _names(
        local_check.build_steps(repo, "full", "base")
    )


def test_quick_includes_tests_of_indirect_consumers(repo):
    (repo / "scripts" / "beta.py").write_text("import alpha\nY = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "beta uses alpha")
    _git(repo, "branch", "-f", "base")
    _commit(repo, "scripts/alpha.py", "X = 2\n")
    steps = dict(local_check.build_steps(repo, "quick", "base"))
    cmd = next(v for k, v in steps.items() if k.startswith("pytest"))
    assert "tests/test_beta.py" in cmd and "tests/test_alpha.py" in cmd


def test_log_path_in_linked_worktree_is_reported(repo, tmp_path):
    wt = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    _commit(wt, "README.md", "# side\n")
    out = subprocess.run(
        [
            sys.executable,
            str(Path(local_check.__file__)),
            "--repo",
            str(wt),
            "--base-ref",
            "base",
            "--mode",
            "quick",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "Traceback" not in out.stderr
    assert "全文のログ: ../" in out.stdout


def test_log_redacts_home_user_and_repo_paths(repo):
    text = f"{Path.home()}/x {repo}/scripts/a.py"
    out = local_check.redact(text, repo)
    assert str(Path.home()) not in out and str(repo) not in out
    assert "<repo>/scripts/a.py" in out


def test_log_does_not_contain_interpreter_path(repo):
    _commit(repo, "README.md", "# t3\n")
    subprocess.run(
        [
            sys.executable,
            str(Path(local_check.__file__)),
            "--repo",
            str(repo),
            "--base-ref",
            "base",
            "--mode",
            "quick",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    logs = list((repo / ".git" / "local-check").glob("*.log"))
    assert logs
    body = logs[0].read_text(encoding="utf-8")
    assert sys.executable not in body
    assert str(Path.home()) not in body


def test_doc_change_selects_tests_that_watch_that_file(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "digests.txt").write_text("a\n", encoding="utf-8")
    (repo / "tests" / "test_digest.py").write_text(
        "P = 'docs/digests.txt'\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "digests")
    _git(repo, "branch", "-f", "base")
    _commit(repo, "docs/digests.txt", "b\n")
    cmd = next(
        v
        for k, v in local_check.build_steps(repo, "quick", "base")
        if k.startswith("pytest")
    )
    assert "tests/test_digest.py" in cmd


def test_unknown_non_python_file_falls_back_to_full(repo):
    _commit(repo, "LICENSE", "x\n")
    assert "pytest（全部・安全側）" in _names(
        local_check.build_steps(repo, "quick", "base")
    )


def test_non_ascii_and_space_filenames_are_kept(repo):
    (repo / "tests" / "test_回帰.py").write_text("\n", encoding="utf-8")
    (repo / "b c.txt").write_text("\n", encoding="utf-8")
    assert local_check.changed_files(repo, "base") == ["b c.txt", "tests/test_回帰.py"]


# ---- 作業ツリー（未ステージ・未追跡）も個人情報の検査に載せる（品質係 2026-10-26） ----
ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "identity_gate.py"
FIXTURE_ID = "Fixture Bot <fixture@example.invalid>"
needs_gitleaks = pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="--staged は固定した gitleaks で動く"
)


def _home_path_line():
    # 実行時に組み立てる（このファイル自身が検査に掛からないように）
    return "P = '" + synthetic_home_path("linux", "alice/proj") + "'\n"


@pytest.mark.parametrize("mode", ["full", "quick"])
@pytest.mark.parametrize("kind", ["unstaged", "untracked"])
def test_worktree_changes_are_checked_in_every_mode(repo, mode, kind):
    if kind == "unstaged":
        (repo / "scripts" / "alpha.py").write_text("X = 4\n", encoding="utf-8")
    else:
        (repo / "notes.txt").write_text("x\n", encoding="utf-8")
    steps = dict(local_check.build_steps(repo, mode, "base"))
    cmd = steps["identity_gate（作業ツリー）"]
    assert cmd[-1] == "--identity-worktree"


@pytest.mark.parametrize("mode", ["full", "quick"])
def test_clean_worktree_adds_no_worktree_step(repo, mode):
    assert "identity_gate（作業ツリー）" not in _names(
        local_check.build_steps(repo, mode, "base")
    )


def test_worktree_index_holds_unstaged_and_untracked_without_touching_index(repo):
    (repo / "scripts" / "alpha.py").write_text("X = 5\n", encoding="utf-8")
    (repo / "notes.txt").write_text("x\n", encoding="utf-8")
    with local_check.worktree_index_env(repo) as env:
        names = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.split()
    assert names == ["notes.txt", "scripts/alpha.py"]
    real = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_ENV,
    ).stdout
    assert real == ""


def _gate_on_worktree(repo, tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {"schema": "public-commit-identities/v1", "identities": [FIXTURE_ID]}
        ),
        encoding="utf-8",
    )
    with local_check.worktree_index_env(repo) as env:
        env.update(
            GIT_AUTHOR_NAME="Fixture Bot",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture Bot",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        return subprocess.run(
            [
                sys.executable,
                str(GATE),
                "--public",
                "--allowed-identities",
                str(policy),
                "--repo",
                str(repo),
                "--staged",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )


@needs_gitleaks
@pytest.mark.parametrize("kind", ["unstaged", "untracked"])
def test_worktree_home_path_is_red(repo, tmp_path, kind):
    target = repo / "scripts" / "alpha.py" if kind == "unstaged" else repo / "notes.py"
    target.write_text(_home_path_line(), encoding="utf-8")
    result = _gate_on_worktree(repo, tmp_path)
    assert result.returncode == 1, result.stderr


@needs_gitleaks
def test_worktree_plain_change_is_green(repo, tmp_path):
    (repo / "scripts" / "alpha.py").write_text("X = 6\n", encoding="utf-8")
    (repo / "notes.txt").write_text("plain\n", encoding="utf-8")
    result = _gate_on_worktree(repo, tmp_path)
    assert result.returncode == 0, result.stderr


def test_quick_summary_says_it_is_not_a_ci_substitute(repo):
    out = subprocess.run(
        [
            sys.executable,
            str(Path(local_check.__file__)),
            "--repo",
            str(repo),
            "--base-ref",
            "base",
            "--mode",
            "quick",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_ENV,
    )
    assert "CI の代わりではありません" in out.stdout
