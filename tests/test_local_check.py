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
    (tmp_path / ".test-selection.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "changes": ["README.md", "notes.txt", ".test-selection.json"],
                        "no_tests": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
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


def test_cli_default_runs_selected_set(repo):
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
    assert "pytest（全部）" not in out.stdout
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


def test_unknown_script_stops_instead_of_running_everything(repo):
    _commit(repo, "scripts/gamma.py", "Z = 1\n")
    with pytest.raises(ValueError, match="unresolved"):
        local_check.build_steps(repo, "quick", "base")


def test_configured_fixture_expands_to_declared_neighbors(repo):
    config = {
        "rules": [{"changes": ["tests/conftest.py"], "tests": ["tests/test_alpha.py"]}]
    }
    plan = local_check.select_tests(repo, ["tests/conftest.py"], config)
    assert plan["status"] == "selected"
    assert plan["selected_tests"] == ["tests/test_alpha.py"]
    assert plan["reasons"]["tests/test_alpha.py"][0]["file"] == "tests/conftest.py"


def test_unmapped_fixture_is_unresolved(repo):
    plan = local_check.select_tests(repo, ["tests/conftest.py"], {})
    assert plan["status"] == "unresolved"
    assert plan["selected_tests"] == []


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


def test_unknown_non_python_file_is_unresolved(repo):
    plan = local_check.select_tests(repo, ["LICENSE"], {})
    assert plan["status"] == "unresolved"


def test_no_test_rule_distinguishes_irrelevant_change(repo):
    plan = local_check.select_tests(
        repo, ["notes.txt"], {"rules": [{"changes": ["notes.txt"], "no_tests": True}]}
    )
    assert plan["status"] == "no_tests"
    assert plan["unresolved"] == []
    assert plan["file_reasons"]["notes.txt"]


def test_configured_consumer_roots_and_string_references(repo):
    (repo / "lib").mkdir()
    (repo / "spec").mkdir()
    (repo / "lib/core.js").write_text("export const value = 1", encoding="utf-8")
    (repo / "lib/use.js").write_text("require('./core')", encoding="utf-8")
    (repo / "spec/use.spec.js").write_text("const name = 'use'", encoding="utf-8")
    plan = local_check.select_tests(
        repo,
        ["lib/core.js"],
        {
            "source_roots": ["lib"],
            "test_roots": ["spec"],
            "test_patterns": ["*.spec.js"],
            "code_suffixes": [".js"],
        },
    )
    assert plan["selected_tests"] == ["spec/use.spec.js"]
    assert plan["status"] == "selected"


@pytest.mark.parametrize(
    "unsafe", ["../elsewhere", "/tmp/outside", "tests/../../outside"]
)
def test_selection_rejects_path_escape(repo, unsafe):
    with pytest.raises(ValueError):
        local_check.select_tests(repo, [unsafe], {})


def test_rename_and_deletion_keep_old_dependency_name(repo):
    _git(repo, "mv", "scripts/alpha.py", "scripts/renamed.py")
    assert "scripts/alpha.py" in local_check.changed_files(repo, "base")
    plan = local_check.plan_tests(repo, "base")
    assert "tests/test_alpha.py" in plan["selected_tests"]
    assert any(x["file"] == "scripts/renamed.py" for x in plan["unresolved"])


def test_deleted_test_is_reported_without_passing_empty_pytest_argv(repo):
    (repo / "tests/test_alpha.py").unlink()
    plan = local_check.select_tests(repo, ["tests/test_alpha.py"], {})
    assert plan["status"] == "no_tests"
    assert "deleted test" in plan["file_reasons"]["tests/test_alpha.py"][0]


def test_selection_cli_returns_json_and_fails_closed(repo):
    (repo / "scripts/gamma.py").write_text("Z = 1", encoding="utf-8")
    out = subprocess.run(
        [
            sys.executable,
            local_check.__file__,
            "--repo",
            str(repo),
            "--base-ref",
            "base",
            "--plan-tests",
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 2
    assert json.loads(out.stdout)["status"] == "unresolved"


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


def test_worktree_index_sees_same_size_rewrite_in_racy_window(repo):
    """コミット直後と同じ時刻・同じ大きさの書き換えも一時の索引に載る（時刻の粗い Mac・Windows の再現）。"""
    _git(repo, "config", "core.trustctime", "false")
    alpha = repo / "scripts" / "alpha.py"
    index = repo / ".git" / "index"
    t = 1_600_000_000 * 10**9
    os.utime(alpha, ns=(t, t))
    _git(repo, "update-index", "--refresh")  # 索引の記録も同じ時刻にする
    alpha.write_text("X = 7\n", encoding="utf-8")  # 同じ大きさで書き換え
    os.utime(alpha, ns=(t, t))
    os.utime(index, ns=(t, t))  # 索引もファイルと同じ時刻（racy の窓）
    with local_check.worktree_index_env(repo) as env:
        names = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.split()
    assert names == ["scripts/alpha.py"]


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
    assert "CI の結果は別に確認" in out.stdout


def test_policy_cannot_escape_target_repository(repo, tmp_path):
    outside = tmp_path.parent / "external-selection.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="target repository"):
        local_check.plan_tests(repo, "base", outside)


def test_empty_rule_glob_is_unresolved_even_with_check_id(repo):
    plan = local_check.select_tests(
        repo,
        ["requirements.txt"],
        {
            "rules": [
                {
                    "changes": ["requirements.txt"],
                    "tests": ["tests/test_missing.py"],
                    "checks": ["dependency-check"],
                }
            ]
        },
    )
    assert plan["status"] == "unresolved"
    assert plan["checks"] == ["dependency-check"]


def test_checks_only_plan_is_explicit_and_does_not_run_commands(repo):
    plan = local_check.select_tests(
        repo,
        ["schema.sql"],
        {"rules": [{"changes": ["*.sql"], "checks": ["sql-contract"]}]},
    )
    assert plan["status"] == "no_tests"
    assert plan["checks"] == ["sql-contract"]
    assert plan["unresolved"] == []


def test_source_symlink_escape_is_rejected(repo, tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("import alpha", encoding="utf-8")
    (repo / "scripts/escape.py").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        local_check.select_tests(repo, ["scripts/alpha.py"], {})


def test_empty_plan_never_invokes_pytest(repo, monkeypatch):
    monkeypatch.setattr(
        local_check,
        "plan_tests",
        lambda *a: {
            "unresolved": [],
            "selected_tests": [],
            "checks": [],
            "status": "no_tests",
        },
    )
    monkeypatch.setattr(
        local_check.subprocess,
        "run",
        lambda *a, **k: pytest.fail("empty plan ran pytest"),
    )
    assert local_check.main(["--run-selected-tests", "--repo", str(repo)]) == 0


def test_common_runner_requires_consumer_to_handle_check_ids(repo, monkeypatch):
    monkeypatch.setattr(
        local_check,
        "plan_tests",
        lambda *a: {
            "unresolved": [],
            "selected_tests": [],
            "checks": ["sql-contract"],
            "status": "no_tests",
        },
    )
    assert local_check.main(["--run-selected-tests", "--repo", str(repo)]) == 2


def test_each_explicit_test_pattern_must_resolve(repo):
    plan = local_check.select_tests(
        repo,
        ["requirements.txt"],
        {
            "rules": [
                {
                    "changes": ["requirements.txt"],
                    "tests": ["tests/test_alpha.py", "tests/test_missing.py"],
                }
            ]
        },
    )
    assert plan["status"] == "unresolved"
    assert plan["selected_tests"] == ["tests/test_alpha.py"]


def test_plan_binds_head_merge_base_policy_and_selector(repo):
    before = local_check.plan_tests(repo, "base")
    _commit(repo, "scripts/alpha.py", "X = 9\n")
    after = local_check.plan_tests(repo, "base")
    assert after["head_oid"] != before["head_oid"]
    assert after["base_oid"] == before["base_oid"]
    assert after["base_kind"] == "merge-base"
    assert after["policy_sha256"] == before["policy_sha256"]
    assert len(after["selector_source_sha256"]) == 64
    assert str(repo) not in json.dumps(after)
    policy = json.loads((repo / ".test-selection.json").read_text())
    policy["rules"][0]["changes"].append("ignored.txt")
    (repo / ".test-selection.json").write_text(json.dumps(policy), encoding="utf-8")
    assert (
        local_check.plan_tests(repo, "base")["policy_sha256"] != after["policy_sha256"]
    )


def test_json_pytest_runner_keeps_stdout_machine_readable(repo, monkeypatch, capsys):
    plan = {
        "unresolved": [],
        "selected_tests": ["tests/test_alpha.py"],
        "checks": [],
        "status": "selected",
    }
    monkeypatch.setattr(local_check, "plan_tests", lambda *a: plan)

    def run(command, **kwargs):
        assert command == [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--",
            "tests/test_alpha.py",
        ]
        assert kwargs["cwd"] == repo
        assert kwargs["stdout"] is sys.stderr
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(local_check.subprocess, "run", run)
    assert (
        local_check.main(["--run-selected-tests", "--json", "--repo", str(repo)]) == 0
    )
    assert json.loads(capsys.readouterr().out) == plan


@pytest.mark.parametrize("value", ["false", None, 0])
def test_invalid_no_tests_type_fails_closed(repo, value):
    with pytest.raises(ValueError, match="no_tests"):
        local_check.select_tests(
            repo,
            ["scripts/unmapped.py"],
            {"rules": [{"changes": ["scripts/unmapped.py"], "no_tests": value}]},
        )


@pytest.mark.parametrize("value", [None, "", "test-selection-policy/v2"])
def test_unknown_policy_schema_fails_closed(repo, value):
    with pytest.raises(ValueError, match="schema"):
        local_check.select_tests(repo, [], {"schema": value})


@pytest.mark.parametrize("check_id", ["", " ", " padded "])
def test_invalid_check_id_fails_closed(repo, check_id):
    with pytest.raises(ValueError, match="check"):
        local_check.select_tests(
            repo,
            ["schema.sql"],
            {"rules": [{"changes": ["schema.sql"], "checks": [check_id]}]},
        )


@pytest.mark.parametrize("deleted", [False, True])
def test_changed_test_provider_selects_importing_tests(repo, deleted):
    provider = repo / "tests/test_provider.py"
    provider.write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests/test_consumer.py").write_text(
        "from test_provider import VALUE\n", encoding="utf-8"
    )
    if deleted:
        provider.unlink()
    plan = local_check.select_tests(repo, ["tests/test_provider.py"], {})
    assert plan["status"] == "selected"
    assert "tests/test_consumer.py" in plan["selected_tests"]
    assert ("tests/test_provider.py" in plan["selected_tests"]) is not deleted
    assert "tests/test_beta.py" not in plan["selected_tests"]


@pytest.mark.parametrize("filename", ["config.json", "seed.txt"])
def test_data_filename_reference_follows_source_to_test(repo, filename):
    (repo / "scripts/data_reader.py").write_text(
        f"FILE = '{filename}'\n", encoding="utf-8"
    )
    (repo / "scripts/data_consumer.py").write_text(
        "import data_reader\n", encoding="utf-8"
    )
    (repo / "tests/test_data_consumer.py").write_text(
        "import data_consumer\n", encoding="utf-8"
    )
    plan = local_check.select_tests(repo, [filename], {})
    assert plan["status"] == "selected"
    assert plan["selected_tests"] == ["tests/test_data_consumer.py"]
    assert plan["reasons"]["tests/test_data_consumer.py"][0]["file"] == filename


def test_mapped_document_limits_automatic_consumers(repo):
    (repo / "scripts/reader.py").write_text("DOC = 'README.md'\n", encoding="utf-8")
    (repo / "tests/test_reader.py").write_text("import reader\n", encoding="utf-8")
    plan = local_check.select_tests(
        repo,
        ["README.md"],
        {
            "rules": [
                {
                    "changes": ["README.md"],
                    "tests": ["tests/test_alpha.py"],
                    "dependency_mode": "mapped",
                }
            ]
        },
    )
    assert plan["selected_tests"] == ["tests/test_alpha.py"]
    assert plan["status"] == "selected"
    assert any("mapped" in reason for reason in plan["file_reasons"]["README.md"])


def test_overlapping_auto_rule_preserves_automatic_consumers(repo):
    (repo / "scripts/reader.py").write_text("DOC = 'README.md'\n", encoding="utf-8")
    (repo / "tests/test_reader.py").write_text("import reader\n", encoding="utf-8")
    plan = local_check.select_tests(
        repo,
        ["README.md"],
        {
            "rules": [
                {
                    "changes": ["README.md"],
                    "tests": ["tests/test_alpha.py"],
                    "dependency_mode": "mapped",
                },
                {"changes": ["README.md"], "tests": ["tests/test_beta.py"]},
            ]
        },
    )
    assert set(plan["selected_tests"]) == {
        "tests/test_alpha.py",
        "tests/test_beta.py",
        "tests/test_reader.py",
    }


@pytest.mark.parametrize("mode", [None, "", "off", 0])
def test_invalid_dependency_mode_is_rejected(repo, mode):
    with pytest.raises(ValueError, match="dependency_mode"):
        local_check.select_tests(
            repo,
            [],
            {
                "rules": [
                    {
                        "changes": ["README.md"],
                        "no_tests": True,
                        "dependency_mode": mode,
                    }
                ]
            },
        )


def test_empty_mapped_rule_is_unresolved(repo):
    plan = local_check.select_tests(
        repo,
        ["README.md"],
        {
            "rules": [
                {"changes": ["README.md"], "dependency_mode": "mapped", "tests": []}
            ]
        },
    )
    assert plan["status"] == "unresolved"


def test_mapped_rule_cannot_omit_changed_test_itself(repo):
    plan = local_check.select_tests(
        repo,
        ["tests/test_alpha.py"],
        {
            "rules": [
                {
                    "changes": ["tests/test_alpha.py"],
                    "dependency_mode": "mapped",
                    "tests": ["tests/test_beta.py"],
                }
            ]
        },
    )
    assert plan["selected_tests"] == ["tests/test_alpha.py", "tests/test_beta.py"]
