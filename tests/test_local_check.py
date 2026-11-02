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


def test_configured_fixture_requires_implicit_impact_review(repo):
    config = {
        "rules": [{"changes": ["tests/conftest.py"], "tests": ["tests/test_alpha.py"]}]
    }
    plan = local_check.select_tests(repo, ["tests/conftest.py"], config)
    assert plan["status"] == "unresolved"
    assert any("fixture-impact" in item["reason"] for item in plan["unresolved"])
    assert "tests/test_beta.py" not in plan["selected_tests"]


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


def test_repository_root_sources_select_top_level_consumers(repo):
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests/test_app.py").write_text("import app\n", encoding="utf-8")
    for directory in (".venv", "node_modules", ".worktrees", ".local"):
        ignored = repo / directory
        ignored.mkdir()
        (ignored / "bridge.py").write_text("import app\n", encoding="utf-8")
    (repo / "tests/test_ignored.py").write_text("import bridge\n", encoding="utf-8")
    plan = local_check.select_tests(repo, ["app.py"], {"source_roots": ["."]})
    assert plan["status"] == "selected"
    assert plan["selected_tests"] == ["tests/test_app.py"]


def test_non_python_consumers_require_owner_mapping(repo):
    (repo / "lib").mkdir()
    (repo / "spec").mkdir()
    (repo / "lib/core.js").write_text("export const value = 1", encoding="utf-8")
    (repo / "lib/use.js").write_text("require('./core')", encoding="utf-8")
    (repo / "spec/use.spec.js").write_text("require('../lib/use')", encoding="utf-8")
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
    assert plan["selected_tests"] == []
    assert plan["status"] == "unresolved"
    mapped = local_check.select_tests(
        repo,
        ["lib/core.js"],
        {
            "source_roots": ["lib"],
            "test_roots": ["spec"],
            "test_patterns": ["*.spec.js"],
            "code_suffixes": [".js"],
            "rules": [
                {
                    "changes": ["lib/*.js"],
                    "tests": ["spec/use.spec.js"],
                    "dependency_mode": "mapped",
                }
            ],
        },
    )
    assert mapped["selected_tests"] == ["spec/use.spec.js"]
    assert mapped["status"] == "selected"


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
        local_check.local_identity_guard, "collect_identities", lambda **kwargs: []
    )
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
    monkeypatch.setattr(
        local_check.local_identity_guard, "collect_identities", lambda **kwargs: []
    )
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


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "changed_files",
        "selected_tests",
        "file_reasons",
        "reasons",
        "unresolved",
        "error",
    ],
)
def test_cli_private_plan_stops_without_disclosing_or_executing(
    repo, monkeypatch, capsys, as_json, field
):
    guard = local_check.local_identity_guard

    private = "SyntheticPrivateOperator"
    identities = guard.collect_identities(sources={"os_account": [private]}, environ={})
    monkeypatch.setattr(guard, "collect_identities", lambda **kwargs: identities)
    plan = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    if field == "error":

        def make_plan(*args):
            raise ValueError("unknown " + private + "/file.py")

    else:
        plan[field] = {private + "/file.py": "unknown " + private}

        def make_plan(*args):
            return plan

    monkeypatch.setattr(local_check, "plan_tests", make_plan)

    def forbidden(*args, **kwargs):
        pytest.fail("private plan must never execute")

    monkeypatch.setattr(local_check.subprocess, "run", forbidden)
    args = ["--repo", str(repo), "--run-selected-tests"] + (
        ["--json"] if as_json else []
    )
    assert local_check.main(args) == 2
    output = capsys.readouterr()
    assert private not in output.out + output.err
    if as_json:
        report = json.loads(output.out)
        assert report["status"] == "unresolved"
        assert not report.get("selected_tests")


def test_cli_public_namespace_remains_selectable(repo, monkeypatch, capsys):
    guard = local_check.local_identity_guard

    collect = guard.collect_identities

    def public_collector(**kwargs):
        return collect(
            sources={"os_account": ["nexus-ai-2045", "nexus_ai"]},
            environ={},
            public_names=kwargs.get("public_names"),
        )

    monkeypatch.setattr(guard, "collect_identities", public_collector)
    plan = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    plan["changed_files"] = ["nexus_ai/public.md"]
    monkeypatch.setattr(local_check, "plan_tests", lambda *args: plan)
    assert local_check.main(["--repo", str(repo), "--plan-tests", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed_files"] == [
        "nexus_ai/public.md"
    ]


@pytest.mark.parametrize(
    "document,consumer",
    [
        ("CHANGELOG.md", "tests/test_release_metadata.py"),
        ("docs/ai-constitution-entry-contract.md", "tests/test_ai_entry_contract.py"),
    ],
)
def test_repository_contract_documents_keep_consumers(repo, document, consumer):
    policy = json.loads((local_check.ROOT / ".test-selection.json").read_text())
    for name in [
        consumer,
        "tests/test_public_narrative_contract.py",
        "tests/test_consistency_gate.py",
    ]:
        (repo / name).write_text("DOCUMENT = " + repr(Path(document).name))
    companions = {
        "tests/test_runtime_support.py",
        "tests/test_github_settings_guide.py",
        "tests/test_ai_entry_contract.py",
    }
    for name in companions:
        if not (repo / name).exists():
            (repo / name).write_text("CONTRACT = True\n")
    plan = local_check.select_tests(repo, [document], policy)
    assert plan["status"] == "selected"
    assert consumer in plan["selected_tests"]
    if document == "CHANGELOG.md":
        assert companions | {
            consumer,
            "tests/test_public_narrative_contract.py",
            "tests/test_consistency_gate.py",
        } <= set(plan["selected_tests"])


def test_mapped_conftest_cannot_hide_implicit_fixture_impact(repo):
    config = {
        "rules": [
            {
                "changes": ["tests/conftest.py"],
                "tests": ["tests/test_alpha.py"],
                "dependency_mode": "mapped",
            }
        ]
    }
    plan = local_check.select_tests(repo, ["tests/conftest.py"], config)
    assert plan["status"] == "unresolved"
    assert any("fixture-impact" in item["reason"] for item in plan["unresolved"])


def test_semantic_dependencies_ignore_prose_and_names(repo):
    (repo / "scripts/app.py").write_text("import alpha\n")
    (repo / "tests/test_real.py").write_text("import app as ui\n")
    (repo / "tests/test_false.py").write_text(
        "app = '/app'\nmessage = 'app input forbidden'\n"
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert "tests/test_real.py" in result["selected_tests"]
    assert "tests/test_false.py" not in result["selected_tests"]


def test_semantic_python_cli(repo):
    (repo / "tests/test_cli.py").write_text(
        "import subprocess, sys\nsubprocess.run([sys.executable, 'scripts/alpha.py'])\n"
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert "tests/test_cli.py" in result["selected_tests"]


@pytest.mark.parametrize(
    "body",
    [
        'import importlib as il\nil.import_module("alpha")',
        'from importlib import import_module as load\nload("alpha")',
        'import runpy\nrunpy.run_path("scripts/alpha.py")',
        'import importlib.util\nfrom pathlib import Path\nP = Path(__file__).resolve().parents[1] / "scripts" / "alpha.py"\nimportlib.util.spec_from_file_location("loaded", P)',
        'import subprocess, sys\nsubprocess.run([sys.executable, "-m", "alpha"])',
        'import subprocess, sys\nsubprocess.run([sys.executable, "-c", "import alpha"])',
    ],
)
def test_semantic_literal_loaders(repo, body):
    (repo / "tests/test_loader.py").write_text(body)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_loader.py" in result["selected_tests"]


@pytest.mark.parametrize(
    "body",
    [
        "import importlib\nimportlib.import_module(user_input)",
        "import runpy\nrunpy.run_path(user_input)",
        "import subprocess, sys\nsubprocess.run([sys.executable, user_input])",
        "exec(user_input)",
        "def malformed(",
    ],
)
def test_semantic_opaque_consumer_stops_even_without_known_edge(repo, body):
    (repo / "tests/test_loader.py").write_text(body)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any(x["file"] == "tests/test_loader.py" for x in result["unresolved"])


def test_semantic_same_stem_packages_and_relative_import(repo):
    for package in ("one", "two"):
        (repo / "scripts" / package).mkdir()
        (repo / "scripts" / package / "__init__.py").write_text("")
        (repo / "scripts" / package / "item.py").write_text("X=1")
    (repo / "scripts/one/reader.py").write_text("from . import item")
    (repo / "tests/test_one.py").write_text("from one import reader")
    (repo / "tests/test_two.py").write_text("from two import item")
    result = local_check.select_tests(repo, ["scripts/one/item.py"], {})
    assert result["selected_tests"] == ["tests/test_one.py"]


def test_semantic_computed_base_explicit_local_filename(repo):
    (repo / "tests/test_loader.py").write_text(
        'import importlib.util\nfrom pathlib import Path\nP = resolve_base() / "alpha.py"\nimportlib.util.spec_from_file_location("loaded", P)'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert "tests/test_loader.py" in result["selected_tests"]


def test_semantic_general_command_is_not_python_dependency(repo):
    (repo / "tests/test_git.py").write_text(
        'import subprocess\nsubprocess.run(command)\nsubprocess.run(["git", "-c", "core.hooksPath=", "status"])'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_alpha.py"]


@pytest.mark.parametrize(
    "expression,base",
    [
        ('_SOURCE / (name + ".py")', "Path(__file__).parent"),
        ('_SOURCE.with_name(name + ".py")', "Path(__file__)"),
    ],
)
@pytest.mark.parametrize(
    "invoke", ['load("alpha")', 'load(name="alpha")', 'NAME = "alpha"\nload(NAME)']
)
def test_semantic_parameterized_loader(repo, expression, base, invoke):
    (repo / "scripts/loader.py").write_text(
        f"import importlib.util\nfrom pathlib import Path\n_SOURCE = {base}\ndef load(name):\n    path = {expression}\n    return importlib.util.spec_from_file_location(name, path)\n"
    )
    (repo / "tests/test_loader.py").write_text("from loader import load\n" + invoke)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_loader.py" in result["selected_tests"]


def test_semantic_loader_alias_and_unknown_actual(repo):
    (repo / "scripts/loader.py").write_text(
        'import importlib.util\nfrom pathlib import Path\n_SOURCE = Path(__file__).parent\ndef load(name):\n    path = _SOURCE / (name + ".py")\n    return importlib.util.spec_from_file_location(name, path)\n'
    )
    (repo / "tests/test_loader.py").write_text(
        'from loader import load as fetch\nfetch(name="alpha")'
    )
    assert (
        "tests/test_loader.py"
        in local_check.select_tests(repo, ["scripts/alpha.py"], {})["selected_tests"]
    )
    (repo / "tests/test_loader.py").write_text(
        "from loader import load as fetch\nfetch(user_input)"
    )
    assert (
        local_check.select_tests(repo, ["scripts/alpha.py"], {})["status"]
        == "unresolved"
    )


def test_semantic_scope_shadow_never_borrows_other_function_value(repo):
    (repo / "tests/test_loader.py").write_text(
        'import importlib\ndef first():\n    name = "alpha"\ndef second(name):\n    importlib.import_module(name)\n'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert "tests/test_loader.py" not in result["selected_tests"]


def test_semantic_loader_callback_and_rebinding_stop(repo):
    source = 'import importlib.util\nfrom pathlib import Path\n_SOURCE = Path(__file__).parent\ndef load(name):\n    path = _SOURCE / (name + ".py")\n    return importlib.util.spec_from_file_location(name, path)\n'
    (repo / "scripts/loader.py").write_text(source)
    (repo / "tests/test_loader.py").write_text(
        "from loader import load\ncallback = load"
    )
    assert (
        local_check.select_tests(repo, ["scripts/alpha.py"], {})["status"]
        == "unresolved"
    )
    (repo / "scripts/loader.py").write_text(source + "\nload = bootstrap().load\n")
    (repo / "tests/test_loader.py").write_text('from loader import load\nload("alpha")')
    assert (
        local_check.select_tests(repo, ["scripts/alpha.py"], {})["status"]
        == "unresolved"
    )


def test_semantic_exec_compiled_local_read(repo):
    (repo / "tests/test_loader.py").write_text(
        'from pathlib import Path\nsource = Path(__file__).parents[1] / "scripts" / "alpha.py"\nraw = source.read_bytes()\nexec(compile(raw, str(source), "exec"), {})'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_loader.py" in result["selected_tests"]


def test_semantic_import_alias_shadow_is_unresolved(repo):
    (repo / "tests/test_loader.py").write_text(
        'import importlib as il\ndef run(il):\n    il.import_module("alpha")'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


def test_semantic_external_test_runner_payload_is_not_loader_target(repo):
    (repo / "tests/test_runner.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable, "-m", "unittest", *test_names])'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"


def test_semantic_loader_formal_reassignment_is_not_summary(repo):
    (repo / "scripts/loader.py").write_text(
        'import importlib.util\nfrom pathlib import Path\n_SOURCE = Path(__file__).parent\ndef load(name):\n    name = runtime_name()\n    path = _SOURCE / (name + ".py")\n    return importlib.util.spec_from_file_location(name, path)'
    )
    (repo / "tests/test_loader.py").write_text('from loader import load\nload("alpha")')
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


def test_semantic_known_external_file_does_not_block_local_change(repo):
    (repo / "tests/test_external.py").write_text(
        'import importlib.util\npath = workspace_root() / "shared/scripts/external_tool.py"\nimportlib.util.spec_from_file_location("helper", path)'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_alpha.py"]


def test_semantic_deleted_literal_loader_remains_local_edge(repo):
    (repo / "tests/test_deleted.py").write_text(
        'import runpy\nrunpy.run_path("scripts/deleted.py")'
    )
    result = local_check.select_tests(repo, ["scripts/deleted.py"], {})
    assert result["selected_tests"] == ["tests/test_deleted.py"]


def test_semantic_selected_opaque_test_is_already_covered(repo):
    (repo / "tests/test_alpha.py").write_text(
        "import alpha, importlib\nimportlib.import_module(runtime_name)"
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert result["selected_tests"] == ["tests/test_alpha.py"]


def test_semantic_opaque_source_in_closure_is_covered_but_other_source_is_not(repo):
    (repo / "scripts/reader.py").write_text(
        "import alpha, importlib\nimportlib.import_module(runtime_name)"
    )
    (repo / "tests/test_reader.py").write_text("import reader")
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_reader.py" in result["selected_tests"]
    (repo / "scripts/unrelated.py").write_text(
        "import importlib\nimportlib.import_module(runtime_name)"
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any(item["file"] == "scripts/unrelated.py" for item in result["unresolved"])


def test_semantic_finite_import_dictionary_preserves_local_members(repo):
    (repo / "tests/test_loader.py").write_text(
        'import importlib\nMODULES = {"alpha": "hash", "json": "hash"}\nfor name, digest in MODULES.items():\n    importlib.import_module(name)'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_loader.py" in result["selected_tests"]


def test_semantic_manual_test_coverage_requires_its_downstream_tail(repo):
    (repo / "tests/test_opaque.py").write_text(
        "import importlib\nimportlib.import_module(runtime_name)"
    )
    policy = {
        "rules": [{"changes": ["scripts/alpha.py"], "tests": ["tests/test_opaque.py"]}]
    }
    result = local_check.select_tests(repo, ["scripts/alpha.py"], policy)
    assert result["status"] == "selected"
    (repo / "tests/test_tail.py").write_text("import test_opaque")
    result = local_check.select_tests(repo, ["scripts/alpha.py"], policy)
    assert result["status"] == "unresolved"


def test_semantic_mutable_import_dictionary_is_unknown(repo):
    (repo / "tests/test_loader.py").write_text(
        'import importlib\nMODULES = {"alpha": "hash"}\nMODULES.update(user_modules)\nfor name in MODULES:\n    importlib.import_module(name)'
    )
    assert (
        local_check.select_tests(repo, ["scripts/alpha.py"], {})["status"]
        == "unresolved"
    )


def test_semantic_file_source_root_selects_flat_consumer(repo):
    (repo / "app.py").write_text("import alpha")
    (repo / "tests/test_app.py").write_text("import app")
    policy = {"source_roots": ["scripts/alpha.py", "app.py"]}
    result = local_check.select_tests(repo, ["scripts/alpha.py"], policy)
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_app.py" in result["selected_tests"]


def test_semantic_unlisted_sibling_source_is_unknown(repo):
    (repo / "app.py").write_text("X=1")
    (repo / "newproduct.py").write_text("X=1")
    (repo / "tests/test_new.py").write_text("import newproduct")
    result = local_check.select_tests(
        repo, ["newproduct.py"], {"source_roots": ["app.py"]}
    )
    assert result["status"] == "unresolved"
    assert any(item["file"] == "newproduct.py" for item in result["unresolved"])


def test_semantic_overlapping_file_directory_roots_parse_once(repo, monkeypatch):
    calls = []
    original = local_check.ast.parse

    def record(text, *args, **kwargs):
        calls.append(text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(local_check.ast, "parse", record)
    local_check.select_tests(
        repo, ["scripts/alpha.py"], {"source_roots": ["scripts", "scripts/alpha.py"]}
    )
    assert calls.count("X = 1\n") == 1


def test_semantic_file_root_symlink_escape_is_rejected(repo, tmp_path_factory):
    external = tmp_path_factory.mktemp("outside") / "external.py"
    external.write_text("X=1")
    (repo / "escape.py").symlink_to(external)
    with pytest.raises(ValueError, match="escapes repository"):
        local_check.select_tests(
            repo, ["scripts/alpha.py"], {"source_roots": ["escape.py"]}
        )


@pytest.mark.parametrize(
    "modes,expect_auto",
    [
        (["supplement"], True),
        (["mapped", "supplement"], False),
        (["mapped", "auto", "supplement"], True),
    ],
)
def test_supplement_adds_mandatory_checks_without_overriding_dependency_mode(
    repo, modes, expect_auto
):
    (repo / "tests/test_consumer.py").write_text("import alpha")
    rules = [
        {
            "changes": ["scripts/alpha.py"],
            "tests": ["tests/test_beta.py"],
            "checks": ["mandatory-contract"],
            "dependency_mode": mode,
        }
        for mode in modes
    ]
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {"rules": rules})
    assert result["status"] == "selected"
    assert "tests/test_beta.py" in result["selected_tests"]
    assert result["checks"] == ["mandatory-contract"]
    assert ("tests/test_consumer.py" in result["selected_tests"]) is expect_auto


@pytest.mark.parametrize("mode", ["invalid", None, 1, [], {}])
def test_dependency_mode_rejects_unknown_values(repo, mode):
    with pytest.raises(ValueError, match="dependency_mode"):
        local_check.select_tests(
            repo,
            ["scripts/alpha.py"],
            {
                "rules": [
                    {
                        "changes": ["scripts/alpha.py"],
                        "tests": ["tests/test_alpha.py"],
                        "dependency_mode": mode,
                    }
                ]
            },
        )


def test_embedded_python_alias_has_own_scope(repo):
    (repo / "tests/test_embedded.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable, "-c", "import importlib as il; il.import_module(\'alpha\')"])'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert "tests/test_embedded.py" in result["selected_tests"]


def test_embedded_python_does_not_inherit_outer_alias_or_file(repo):
    (repo / "tests/test_embedded.py").write_text(
        'import subprocess, sys, importlib as il\nsubprocess.run([sys.executable, "-c", "il.import_module(name)"])'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


def test_embedded_nested_alias_does_not_use_outer_shadow(repo):
    (repo / "tests/test_embedded.py").write_text(
        'import subprocess, sys\nil = object()\nsubprocess.run([sys.executable, "-c", "def load():\\n import importlib as il\\n il.import_module(\'alpha\')\\nload()"])'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert "tests/test_embedded.py" in result["selected_tests"]


def test_embedded_file_has_no_outer_file_value(repo):
    (repo / "tests/test_embedded.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable, "-c", "import runpy; runpy.run_path(__file__)"])'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


@pytest.mark.parametrize(
    "cwd,status", [('"."', "selected"), ("runtime_directory", "unresolved")]
)
def test_embedded_cwd_subset_is_explicit(repo, cwd, status):
    (repo / "tests/test_embedded.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable, "-c", "import json"], cwd='
        + cwd
        + ")"
    )
    assert local_check.select_tests(repo, ["scripts/alpha.py"], {})["status"] == status


def test_assignment_loader_alias_is_not_silently_skipped(repo):
    (repo / "tests/test_alias.py").write_text(
        'import importlib\nload = importlib.import_module\nload("alpha")'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert "tests/test_alias.py" in result["selected_tests"]


@pytest.mark.parametrize("package", ['"pkg"', 'package="pkg"'])
def test_relative_dynamic_import_has_package_context(repo, package):
    (repo / "scripts/pkg").mkdir()
    (repo / "scripts/pkg/alpha.py").write_text("X=1")
    (repo / "tests/test_direct.py").write_text("import pkg.alpha")
    (repo / "tests/test_dynamic.py").write_text(
        'import importlib\nimportlib.import_module(".alpha", ' + package + ")"
    )
    result = local_check.select_tests(repo, ["scripts/pkg/alpha.py"], {})
    assert "tests/test_dynamic.py" in result["selected_tests"]


def test_rebound_assignment_loader_alias_stays_unknown(repo):
    (repo / "tests/test_alias.py").write_text(
        'import importlib\nload = importlib.import_module\nload = runtime_loader\nload("alpha")'
    )
    assert (
        local_check.select_tests(repo, ["scripts/alpha.py"], {})["status"]
        == "unresolved"
    )


def test_relative_dynamic_import_unknown_package_stays_unknown(repo):
    (repo / "tests/test_dynamic.py").write_text(
        'import importlib\nimportlib.import_module(".alpha", package=runtime_package)'
    )
    assert (
        local_check.select_tests(repo, ["scripts/alpha.py"], {})["status"]
        == "unresolved"
    )


def test_toml_condition_requires_internal_base_proof(repo):
    rule = {
        "changes": ["pyproject.toml"],
        "tests": ["tests/test_alpha.py"],
        "dependency_mode": "mapped",
        "toml_only_keys": ["project.version"],
        "condition_verified": True,
    }
    result = local_check.select_tests(repo, ["pyproject.toml"], {"rules": [rule]})
    assert result["status"] == "unresolved"
    assert (
        local_check.select_tests(repo, ["scripts/alpha.py"], {"rules": [rule]})[
            "status"
        ]
        == "selected"
    )


@pytest.mark.parametrize(
    "extra,status", [("", "selected"), ('dependencies = ["new"]\n', "unresolved")]
)
@pytest.mark.parametrize("entry", ["plan", "full", "quick"])
def test_toml_version_only_condition_preserves_dependency_auto(
    repo, extra, status, entry
):
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0"\n')
    (repo / "tests/test_opaque.py").write_text(
        "import importlib\nimportlib.import_module(runtime_name)"
    )
    (repo / ".test-selection.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "changes": ["pyproject.toml"],
                        "tests": ["tests/test_alpha.py"],
                        "dependency_mode": "mapped",
                        "toml_only_keys": ["project.version"],
                    },
                    {
                        "changes": ["pyproject.toml"],
                        "tests": ["tests/test_beta.py"],
                        "dependency_mode": "supplement",
                    },
                ]
            }
        )
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "manifest base")
    _git(repo, "branch", "-f", "base")
    (repo / "pyproject.toml").write_text('[project]\nversion = "2.0"\n' + extra)
    if entry == "plan":
        result = local_check.plan_tests(repo, "base")
        assert result["status"] == status
    elif status == "unresolved":
        with pytest.raises(ValueError, match="unresolved test selection"):
            local_check.build_steps(repo, entry, "base")
    else:
        steps = local_check.build_steps(repo, entry, "base")
        commands = [command for name, command in steps if name.startswith("pytest")]
        assert len(commands) == 1
        assert commands[0][commands[0].index("--") + 1 :] == [
            "tests/test_alpha.py",
            "tests/test_beta.py",
        ]


@pytest.mark.parametrize(
    "before,after",
    [
        ("flag = 1", "flag = true"),
        ("flag = 1", ""),
        ("", "flag = 1"),
        ("flag = 1", "flag = ["),
    ],
)
def test_toml_condition_rejects_other_type_keys_and_parse_errors(repo, before, after):
    name = "pyproject.toml"
    (repo / name).write_text('[project]\nversion = "1.0"\n' + before)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "toml base")
    (repo / name).write_text('[project]\nversion = "2.0"\n' + after)
    base = local_check.git(repo, "rev-parse", "HEAD").strip()
    config, evidence = local_check._toml_scoped_config(
        repo,
        [name],
        {
            "rules": [
                {
                    "changes": [name],
                    "toml_only_keys": ["project.version"],
                    "dependency_mode": "mapped",
                    "tests": ["tests/test_alpha.py"],
                }
            ]
        },
        base,
    )
    assert config["rules"] == []
    assert evidence[0]["matched"] is False
    assert "flag" not in json.dumps(evidence)


def test_toml_version_condition_does_not_accept_symlink(repo):
    manifest = repo / "pyproject.toml"
    manifest.write_text('[project]\nversion = "1.0"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "toml base")
    base = local_check.git(repo, "rev-parse", "HEAD").strip()
    manifest.unlink()
    (repo / "target.toml").write_text('[project]\nversion = "2.0"\n')
    manifest.symlink_to("target.toml")
    config, evidence = local_check._toml_scoped_config(
        repo,
        ["pyproject.toml"],
        {
            "rules": [
                {
                    "changes": ["pyproject.toml"],
                    "toml_only_keys": ["project.version"],
                    "tests": ["tests/test_alpha.py"],
                    "dependency_mode": "mapped",
                }
            ]
        },
        base,
    )
    assert config["rules"] == []
    assert evidence[0]["matched"] is False


@pytest.mark.parametrize(
    "body,target",
    [
        (
            'import importlib\nNAME="beta"\ndef change():\n global NAME\n NAME="alpha"\nchange()\nimportlib.import_module(NAME)',
            "scripts/alpha.py",
        ),
        (
            'import importlib\nNAME="alpha"\nTARGET=NAME\ndef read():\n NAME="beta"\n importlib.import_module(TARGET)\nread()',
            "scripts/alpha.py",
        ),
        (
            'from pathlib import Path\n(Path(__file__).parent / "data/config.json").read_text()',
            "tests/data/config.json",
        ),
        ('__import__("pkg", fromlist=["alpha"])', "scripts/pkg/alpha.py"),
        (
            'import subprocess, sys\nsubprocess.run([sys.executable, "pkg/alpha.py"], cwd="scripts")',
            "scripts/pkg/alpha.py",
        ),
    ],
)
def test_reference_context_never_silently_omits_consumer(repo, body, target):
    path = repo / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("X=1" if target.endswith(".py") else "{}")
    (repo / "tests/test_direct.py").write_text("TARGET = " + repr(target))
    (repo / "tests/test_context.py").write_text(body)
    result = local_check.select_tests(repo, [target], {})
    assert (
        result["status"] == "unresolved"
        or "tests/test_context.py" in result["selected_tests"]
    )


def test_relative_imported_loader_alias_preserves_package(repo):
    (repo / "scripts/pkg").mkdir()
    (repo / "scripts/pkg/alpha.py").write_text("X=1")
    (repo / "scripts/pkg/helper.py").write_text(
        'from pathlib import Path\nimport importlib.util\nSOURCE=Path(__file__).parent\ndef load(name):\n return importlib.util.spec_from_file_location(name, SOURCE / (name + ".py"))'
    )
    (repo / "scripts/pkg/consumer.py").write_text(
        'from .helper import load\nload("alpha")'
    )
    (repo / "tests/test_context.py").write_text("import pkg.consumer")
    (repo / "tests/test_direct.py").write_text("import pkg.alpha")
    result = local_check.select_tests(repo, ["scripts/pkg/alpha.py"], {})
    assert (
        result["status"] == "unresolved"
        or "tests/test_context.py" in result["selected_tests"]
    )


@pytest.mark.parametrize(
    "body",
    [
        'import importlib\ndef outer():\n name="beta"\n def mutate():\n  nonlocal name\n  name="alpha"\n mutate()\n importlib.import_module(name)\nouter()',
        '__import__("pkg", fromlist=runtime_names)',
        '__import__("pkg", fromlist=["alpha"], level=runtime_level)',
        'import subprocess, sys\nsubprocess.run([sys.executable, "pkg/alpha.py"], cwd=runtime_cwd)',
        'import runpy\nNAME=runtime_path\nTARGET=NAME\ndef read():\n NAME="scripts/beta.py"\n runpy.run_path(TARGET)\nread()',
    ],
)
def test_unproven_reference_context_stays_unknown(repo, body):
    (repo / "tests/test_context.py").write_text(body)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


def test_global_mutation_does_not_invalidate_independent_local_binding(repo):
    (repo / "tests/test_context.py").write_text(
        'import importlib\nname="beta"\ndef mutate():\n global name\n name=runtime_name\ndef read():\n name="alpha"\n importlib.import_module(name)\nread()'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert "tests/test_context.py" in result["selected_tests"]
    assert result["status"] == "selected"


@pytest.mark.parametrize(
    "body",
    [
        'import runpy\nrunpy.run_path("scripts\\\\alpha.py")',
        'import subprocess, sys\nsubprocess.run([sys.executable, "alpha.py"], cwd="scripts")',
        'from pathlib import Path\nimport importlib.util\nsource=Path(__file__).parents[1] / "scripts" / "alpha.py"\nimportlib.util.spec_from_file_location("alpha", source)',
    ],
)
def test_file_reference_ids_are_platform_independent(repo, body):
    (repo / "tests/test_context.py").write_text(body)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert "tests/test_context.py" in result["selected_tests"]
    assert result["status"] == "selected"


def test_windows_reference_path_composition_is_posix():
    base = local_check._reference_path("scripts\\pkg\\loader.py")
    assert base.with_name("alpha.py").as_posix() == "scripts/pkg/alpha.py"
    assert (base.parent / "alpha.py").as_posix() == "scripts/pkg/alpha.py"


def test_composed_file_reference_does_not_use_repository_root_fragment(repo):
    for name in ["data/config.json", "tests/data/config.json"]:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    (repo / "tests/test_direct.py").write_text('DATA = "data/config.json"')
    (repo / "tests/test_context.py").write_text(
        'from pathlib import Path\nsource = Path(__file__).parent / "data/config.json"\nsource.read_text()'
    )
    result = local_check.select_tests(repo, ["data/config.json"], {})
    assert result["status"] == "selected"
    assert "tests/test_context.py" not in result["selected_tests"]
    local = local_check.select_tests(repo, ["tests/data/config.json"], {})
    assert "tests/test_context.py" in local["selected_tests"]


def test_python_cli_cwd_does_not_also_reference_root_script(repo):
    for name in ["pkg/alpha.py", "scripts/pkg/alpha.py"]:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("X=1")
    (repo / "tests/test_direct.py").write_text('SOURCE="pkg/alpha.py"')
    (repo / "tests/test_context.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable, "pkg/alpha.py"], cwd="scripts")'
    )
    result = local_check.select_tests(
        repo, ["pkg/alpha.py"], {"source_roots": ["scripts", "pkg"]}
    )
    assert result["status"] == "selected"
    assert "tests/test_context.py" not in result["selected_tests"]


def test_parent_segment_in_ordinary_marker_data_is_not_dependency(repo):
    (repo / "tests/test_marker.py").write_text(
        'original = "codex.exe"\nmarker = original.replace("codex.exe", "../codex.exe")'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_alpha.py"]


def test_static_file_parent_segments_resolve_within_repository(repo):
    (repo / "tests/data").mkdir()
    (repo / "tests/config.json").write_text("{}")
    (repo / "tests/test_reader.py").write_text(
        'from pathlib import Path\np = Path(__file__).parent / "data" / ".." / "config.json"\np.read_text()'
    )
    result = local_check.select_tests(repo, ["tests/config.json"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_reader.py" in result["selected_tests"]


def test_static_external_file_is_not_local_producer(repo):
    (repo / "config.json").write_text("{}")
    (repo / "tests/test_direct.py").write_text('DOC = "config.json"')
    (repo / "tests/test_external.py").write_text(
        'from pathlib import Path\np = Path(__file__).parents[1] / ".." / "config.json"\np.read_text()'
    )
    result = local_check.select_tests(repo, ["config.json"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_external.py" not in result["selected_tests"]


def test_changed_file_parent_segments_still_reject(repo):
    with pytest.raises(ValueError, match="repository-relative"):
        local_check.select_tests(repo, ["scripts/../scripts/alpha.py"], {})


@pytest.mark.parametrize(
    "definition",
    [
        'def reader(arg=importlib.import_module(NAME)):\n NAME="beta"',
        'def reader(*, arg=importlib.import_module(NAME)):\n NAME="beta"',
        '@importlib.import_module(NAME).decorate\ndef reader():\n NAME="beta"',
        'def reader() -> importlib.import_module(NAME):\n NAME="beta"',
        'def reader(arg: importlib.import_module(NAME)):\n NAME="beta"',
        "reader = lambda NAME=importlib.import_module(NAME): NAME",
        'class Reader(metaclass=importlib.import_module(NAME).Meta):\n NAME="beta"',
        'class Reader(importlib.import_module(NAME).Base):\n NAME="beta"',
        '@importlib.import_module(NAME).decorate\nclass Reader:\n NAME="beta"',
    ],
)
def test_definition_headers_use_enclosing_scope(repo, definition):
    (repo / "tests/test_header.py").write_text(
        'import importlib\nNAME="alpha"\n' + definition
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert "tests/test_header.py" in result["selected_tests"]
    assert result["status"] == "selected", result["unresolved"]


@pytest.mark.parametrize("embedded", [False, True])
def test_loader_callback_escape_is_checked_in_every_parsed_tree(repo, embedded):
    code = 'import importlib\nlist(map(importlib.import_module, ["alpha"]))'
    if embedded:
        code = (
            'import subprocess, sys\nsubprocess.run([sys.executable, "-c", '
            + repr(code)
            + "])"
        )
    (repo / "tests/test_callback.py").write_text(code)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any(
        item["file"] == "tests/test_callback.py" for item in result["unresolved"]
    )


def test_loader_parent_traversal_still_requires_owner_mapping(repo):
    (repo / "tests/test_loader.py").write_text(
        'import runpy\nrunpy.run_path("scripts/../scripts/alpha.py")'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any("path escape" in item["reason"] for item in result["unresolved"])


def test_method_default_uses_class_namespace_not_method_body(repo):
    (repo / "tests/test_header.py").write_text(
        'import importlib\nNAME="beta"\nclass Holder:\n NAME="alpha"\n def reader(arg=importlib.import_module(NAME)):\n  NAME="beta"'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_header.py" in result["selected_tests"]


def test_embedded_imported_callback_alias_remains_unknown(repo):
    code = 'from importlib import import_module as load\nlist(map(load, ["alpha"]))'
    (repo / "tests/test_callback.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable, "-c", '
        + repr(code)
        + "])"
    )
    assert (
        local_check.select_tests(repo, ["scripts/alpha.py"], {})["status"]
        == "unresolved"
    )


@pytest.mark.parametrize(
    "pattern,subject",
    [
        ("str(NAME)", '"alpha"'),
        ("[NAME]", '["alpha"]'),
        ('{"key": NAME}', '{"key": "alpha"}'),
        ("[*NAME]", '["alpha"]'),
        ("{**NAME}", '{"key": "alpha"}'),
        ('{"key": [str(NAME)]}', '{"key": ["alpha"]}'),
    ],
)
@pytest.mark.parametrize("embedded", [False, True])
def test_pattern_capture_invalidates_previous_loader_target(
    repo, pattern, subject, embedded
):
    code = (
        'import importlib\nNAME="beta"\nmatch '
        + subject
        + ":\n case "
        + pattern
        + ":\n  importlib.import_module(NAME)\n"
    )
    if embedded:
        code = (
            'import subprocess, sys\nsubprocess.run([sys.executable, "-c", '
            + repr(code)
            + "])"
        )
    (repo / "tests/test_capture.py").write_text(code)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any(item["file"] == "tests/test_capture.py" for item in result["unresolved"])


@pytest.mark.parametrize(
    "module,call",
    [
        ("importlib", 'import_module("alpha")'),
        ("importlib.util", 'spec_from_file_location("loaded", runtime_target)'),
        ("runpy", 'run_module("alpha")'),
    ],
)
@pytest.mark.parametrize("embedded", [False, True])
def test_supported_loader_star_import_is_not_silently_ignored(
    repo, module, call, embedded
):
    code = "from " + module + " import *\n" + call
    if embedded:
        code = (
            'import subprocess, sys\nsubprocess.run([sys.executable, "-c", '
            + repr(code)
            + "])"
        )
    (repo / "tests/test_star_loader.py").write_text(code)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any(
        item["file"] == "tests/test_star_loader.py" for item in result["unresolved"]
    )


def test_non_loader_star_import_does_not_block_known_local_consumer(repo):
    (repo / "tests/test_normal.py").write_text("from math import *\nimport alpha")
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert "tests/test_normal.py" in result["selected_tests"]


@pytest.mark.parametrize(
    "body",
    [
        'from pathlib import Path\nPath("assets/config").read_text()',
        'open("assets/config").read()',
        'FILE = "assets/config"',
    ],
)
def test_extensionless_exact_file_reference_keeps_reader(repo, body):
    (repo / "assets").mkdir()
    (repo / "assets/config").write_text("value")
    (repo / "scripts/reader.py").write_text(body)
    (repo / "tests/test_reader.py").write_text("import reader")
    policy = {
        "rules": [
            {
                "changes": ["assets/**"],
                "tests": ["tests/test_beta.py"],
                "dependency_mode": "supplement",
            }
        ]
    }
    result = local_check.select_tests(repo, ["assets/config"], policy)
    assert result["status"] == "selected"
    assert "tests/test_reader.py" in result["selected_tests"]
    assert "tests/test_beta.py" in result["selected_tests"]


@pytest.mark.parametrize(
    "mutation",
    [
        'CMD[1] = "pkg/alpha.py"',
        "CMD.insert(1, runtime_target)",
        "CMD += runtime_arguments",
    ],
)
def test_mutated_known_python_command_is_not_silently_ignored(repo, mutation):
    (repo / "scripts/pkg").mkdir()
    (repo / "scripts/pkg/alpha.py").write_text("X=1")
    (repo / "tests/test_direct.py").write_text("import pkg.alpha")
    (repo / "tests/test_command.py").write_text(
        'import subprocess, sys\nCMD=[sys.executable, "pkg/beta.py"]\n'
        + mutation
        + '\nsubprocess.run(CMD, cwd="scripts")'
    )
    result = local_check.select_tests(repo, ["scripts/pkg/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any(item["file"] == "tests/test_command.py" for item in result["unresolved"])


def test_unresolved_external_command_does_not_become_python_loader(repo):
    (repo / "tests/test_command.py").write_text(
        'import subprocess\nCMD=["git", "status"]\nCMD[1]=runtime_option\nsubprocess.run(CMD)\nsubprocess.run(runtime_external_command)'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_alpha.py"]


def test_command_provenance_respects_local_shadowing(repo):
    (repo / "tests/test_command.py").write_text(
        'import subprocess, sys\nCMD=[sys.executable,"unrelated.py"]\n'
        'def external():\n CMD=["git","status"]\n CMD[1]=runtime_option\n subprocess.run(CMD)\n'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_alpha.py"]


def test_inherited_python_command_mutation_remains_unknown(repo):
    (repo / "tests/test_command.py").write_text(
        'import subprocess, sys\nCMD=[sys.executable,"pkg/beta.py"]\n'
        'def execute():\n CMD[1]=runtime_target\n subprocess.run(CMD,cwd="scripts")\n'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


def test_extensionless_basename_does_not_guess_another_directory(repo):
    (repo / "assets").mkdir()
    (repo / "assets/config").write_text("value")
    (repo / "tests/test_message.py").write_text('MESSAGE="config"')
    policy = {
        "rules": [
            {
                "changes": ["assets/**"],
                "tests": ["tests/test_beta.py"],
                "dependency_mode": "supplement",
            }
        ]
    }
    result = local_check.select_tests(repo, ["assets/config"], policy)
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_beta.py"]


@pytest.mark.parametrize("option", ["-O", "-OO", "-X"])
def test_unsupported_python_option_is_not_a_filename(repo, option):
    (repo / "tests/test_command.py").write_text(
        "import subprocess, sys\nsubprocess.run([sys.executable, "
        + repr(option)
        + ', "-m", "alpha"])'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


@pytest.mark.parametrize(
    "loader", ["runpy.run_path", "importlib.util.spec_from_file_location"]
)
def test_unknown_loader_prefix_is_not_proven_external(repo, loader):
    (repo / "scripts/pkg").mkdir()
    (repo / "scripts/pkg/alpha.py").write_text("VALUE=1")
    (repo / "tests/test_direct.py").write_text("import pkg.alpha")
    target = 'Path(os.environ["SOURCE_ROOT"]) / "pkg/alpha.py"'
    arguments = target if loader == "runpy.run_path" else '"loaded", ' + target
    (repo / "tests/test_prefix.py").write_text(
        "import os, runpy, importlib.util\nfrom pathlib import Path\n"
        + loader
        + "("
        + arguments
        + ")"
    )
    result = local_check.select_tests(repo, ["scripts/pkg/alpha.py"], {})
    assert result["status"] == "unresolved"


@pytest.mark.parametrize(
    "alias",
    [
        "OTHER=CMD\nOTHER[1]=runtime_target",
        "OTHER=CMD\nTHIRD=OTHER\nTHIRD.insert(1,runtime_target)",
    ],
)
def test_mutable_command_alias_does_not_leave_original_resolved(repo, alias):
    (repo / "tests/test_command.py").write_text(
        'import subprocess, sys\nCMD=[sys.executable,"pkg/beta.py"]\n'
        + alias
        + '\nsubprocess.run(CMD,cwd="scripts")'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


def test_python_version_query_is_not_an_unresolved_script(repo):
    (repo / "tests/test_version.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable,"--version"])'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"


@pytest.mark.parametrize(
    "method", ["run", "Popen", "call", "check_call", "check_output"]
)
@pytest.mark.parametrize("embedded", [False, True])
def test_subprocess_args_keyword_keeps_python_consumer(repo, method, embedded):
    (repo / "scripts/pkg").mkdir()
    (repo / "scripts/pkg/alpha.py").write_text("VALUE=1")
    (repo / "tests/test_direct.py").write_text("import pkg.alpha")
    arguments = (
        '[sys.executable,"-c","import scripts.pkg.alpha"]'
        if embedded
        else '[sys.executable,"pkg/alpha.py"],cwd="scripts"'
    )
    (repo / "tests/test_keyword.py").write_text(
        "import subprocess,sys\nsubprocess." + method + "(args=" + arguments + ")"
    )
    result = local_check.select_tests(repo, ["scripts/pkg/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_keyword.py" in result["selected_tests"]


@pytest.mark.parametrize("alias", ["OTHER=CMD", "MIDDLE=CMD\nOTHER=MIDDLE"])
def test_immutable_python_command_alias_keeps_target(repo, alias):
    (repo / "scripts/pkg").mkdir()
    (repo / "scripts/pkg/alpha.py").write_text("VALUE=1")
    (repo / "tests/test_direct.py").write_text("import pkg.alpha")
    (repo / "tests/test_command.py").write_text(
        'import subprocess,sys\nCMD=(sys.executable,"pkg/alpha.py")\n'
        + alias
        + '\nsubprocess.run(OTHER,cwd="scripts")'
    )
    result = local_check.select_tests(repo, ["scripts/pkg/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_command.py" in result["selected_tests"]


@pytest.mark.parametrize(
    "arguments",
    [
        '[sys.executable,"other.py"],args=[sys.executable,"pkg/alpha.py"]',
        'args=[sys.executable,"pkg/alpha.py"],**runtime_options',
    ],
)
def test_ambiguous_python_subprocess_arguments_remain_unknown(repo, arguments):
    (repo / "tests/test_command.py").write_text(
        "import subprocess,sys\nsubprocess.run(" + arguments + ")"
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


def test_subprocess_result_object_and_generic_keyword_command_are_not_python_loaders(
    repo,
):
    (repo / "tests/test_result.py").write_text(
        'import subprocess,sys\nsubprocess.CompletedProcess(args=[sys.executable,"-c","import alpha"],returncode=0)\n'
        "subprocess.run(args=runtime_external_command,**runtime_options)"
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_alpha.py"]


@pytest.mark.parametrize(
    "helper,consumer",
    [
        (
            "from importlib import import_module as load",
            'from helper import load\nload("alpha")',
        ),
        (
            "from runpy import run_module as fetch",
            'import helper\nhelper.fetch("alpha")',
        ),
        (
            "from importlib.util import spec_from_file_location as load",
            'from helper import *\nload("loaded",runtime_target)',
        ),
        (
            "import importlib as library",
            'import helper\nhelper.library.import_module("alpha")',
        ),
        (
            "from importlib import import_module as fetch\nfetch=lambda name: None",
            'from helper import fetch\nfetch("alpha")',
        ),
    ],
)
def test_standard_loader_reexport_requires_owner_mapping(repo, helper, consumer):
    (repo / "scripts/helper.py").write_text(helper)
    (repo / "tests/test_reexport.py").write_text(consumer)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any(
        "re-export" in item["reason"] or "star import" in item["reason"]
        for item in result["unresolved"]
    )


@pytest.mark.parametrize(
    "consumer", ["from helper import load\nload(4)", "import helper\nhelper.load(4)"]
)
def test_non_loader_reexport_with_same_alias_does_not_block(repo, consumer):
    (repo / "scripts/helper.py").write_text("from math import sqrt as load")
    (repo / "tests/test_math.py").write_text(consumer)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_alpha.py"]


def test_star_module_reexport_cannot_hide_a_supported_loader(repo):
    (repo / "scripts/helper.py").write_text("import importlib as library")
    (repo / "tests/test_reexport.py").write_text(
        'from helper import *\nlibrary.import_module("alpha")'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


@pytest.mark.parametrize(
    "helper,consumer",
    [
        (
            "import importlib\nload=importlib.import_module",
            'from helper import load\nload("alpha")',
        ),
        (
            "import importlib as lib\nfetch=lib.import_module",
            'import helper\nhelper.fetch("alpha")',
        ),
        (
            "import importlib as lib\nload=lib.import_module\nload=lambda name: None",
            'from helper import load\nload("alpha")',
        ),
        (
            "import importlib as lib\nfirst=lib.import_module\nfirst=lambda name: None\nload=first",
            'from helper import load\nload("alpha")',
        ),
        (
            "import importlib\nlibrary=importlib\nlibrary=object()\nload=library.import_module",
            'import helper\nhelper.load("alpha")',
        ),
    ],
)
def test_assigned_standard_loader_reexport_remains_unknown(repo, helper, consumer):
    (repo / "scripts/helper.py").write_text(helper)
    (repo / "tests/test_reexport.py").write_text(consumer)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"


@pytest.mark.parametrize(
    "helper",
    [
        "import math\nload=math.sqrt",
        "from math import sqrt\nload=sqrt\nload=lambda value: value",
    ],
)
def test_assigned_non_loader_reexport_does_not_block(repo, helper):
    (repo / "scripts/helper.py").write_text(helper)
    (repo / "tests/test_math.py").write_text("from helper import load\nload(4)")
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected"
    assert result["selected_tests"] == ["tests/test_alpha.py"]


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("namespace", [False, True])
@pytest.mark.parametrize("execution", ["runpy", "cli"])
def test_package_execution_tracks_main_file(repo, deleted, namespace, execution):
    package = repo / "scripts/pkg"
    package.mkdir()
    if not namespace:
        (package / "__init__.py").write_text("")
    if not deleted:
        (package / "__main__.py").write_text("VALUE = 1\n")
    code = (
        'import runpy\nrunpy.run_module("pkg", run_name="__main__")'
        if execution == "runpy"
        else 'import subprocess, sys\nsubprocess.run([sys.executable, "-m", "pkg"])'
    )
    (repo / "tests/test_package.py").write_text(code)
    result = local_check.select_tests(repo, ["scripts/pkg/__main__.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_package.py" in result["selected_tests"]


@pytest.mark.parametrize(
    "code", ["import pkg", 'import importlib\nimportlib.import_module("pkg")']
)
def test_package_import_does_not_execute_main(repo, code):
    package = repo / "scripts/pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text("VALUE = 1\n")
    (repo / "tests/test_package.py").write_text(code)
    result = local_check.select_tests(repo, ["scripts/pkg/__main__.py"], {})
    assert "tests/test_package.py" not in result["selected_tests"]


def test_python_module_execution_with_changed_cwd_requires_mapping(repo):
    (repo / "tests/test_package.py").write_text(
        'import subprocess, sys\nsubprocess.run([sys.executable, "-m", "alpha"], cwd="scripts")'
    )
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any("module cwd" in item["reason"] for item in result["unresolved"])


def test_regular_module_execution_does_not_execute_package_main(repo):
    (repo / "scripts/pkg.py").write_text("")
    (repo / "tests/test_package.py").write_text('import runpy\nrunpy.run_module("pkg")')
    result = local_check.select_tests(repo, ["scripts/pkg/__main__.py"], {})
    assert "tests/test_package.py" not in result["selected_tests"]


@pytest.mark.parametrize(
    "comprehension",
    [
        "[importlib.import_module(NAME) for _ in (0,)]",
        "{importlib.import_module(NAME) for _ in (0,)}",
        "{_: importlib.import_module(NAME) for _ in (0,)}",
        "(importlib.import_module(NAME) for _ in (0,))",
    ],
)
@pytest.mark.parametrize("embedded", [False, True])
def test_class_comprehension_cannot_borrow_class_loader_binding(
    repo, comprehension, embedded
):
    code = (
        'import importlib\nNAME="alpha"\nclass Holder:\n NAME="beta"\n values='
        + comprehension
    )
    if embedded:
        code = (
            'import subprocess, sys\nsubprocess.run([sys.executable, "-c", '
            + repr(code)
            + "])"
        )
    (repo / "tests/test_comprehension.py").write_text(code)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert (
        result["status"] == "unresolved"
        or "tests/test_comprehension.py" in result["selected_tests"]
    )


@pytest.mark.parametrize("embedded", [False, True])
def test_class_comprehension_shadowed_loader_alias_requires_mapping(repo, embedded):
    code = (
        'from importlib import import_module as load\nNAME="alpha"\n'
        'class Holder:\n load=lambda name: None\n NAME="beta"\n values=[load(NAME) for _ in (0,)]'
    )
    if embedded:
        code = (
            'import subprocess, sys\nsubprocess.run([sys.executable, "-c", '
            + repr(code)
            + "])"
        )
    (repo / "tests/test_comprehension.py").write_text(code)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "unresolved"
    assert any("class comprehension" in item["reason"] for item in result["unresolved"])


@pytest.mark.parametrize(
    "code",
    [
        "values=[str(_) for _ in (0,)]",
        "def helper():\n return [str(_) for _ in (0,)]",
        "class Holder:\n values=(1, 2)",
        "class Holder:\n def helper(self):\n  return [str(_) for _ in (0,)]",
    ],
)
def test_nonclass_comprehensions_and_class_data_remain_supported(repo, code):
    (repo / "tests/test_comprehension.py").write_text("import alpha\n" + code)
    result = local_check.select_tests(repo, ["scripts/alpha.py"], {})
    assert result["status"] == "selected", result["unresolved"]
    assert "tests/test_comprehension.py" in result["selected_tests"]
