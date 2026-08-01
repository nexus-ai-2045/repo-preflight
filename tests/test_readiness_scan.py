import importlib.util
import json
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readiness_scan.py"
SPEC = importlib.util.spec_from_file_location("readiness_scan", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test Author")
    git(repo, "config", "user.email", "test-author@example.invalid")
    for name in MODULE.REQUIRED:
        (repo / name).write_text(f"# {name}\n", encoding="utf-8")
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", "https://github.com/example/repo.git")
    return repo


def test_clean_complete_repo_passes(tmp_path: Path):
    assert MODULE.scan(make_repo(tmp_path))["status"] == "pass"


def test_release_mode_autoruns_readme_design_gate(tmp_path: Path):
    report = MODULE.scan(make_repo(tmp_path), release=True)
    assert report["status"] == "blocked"
    assert report["checks"]["readme_release_design"]["status"] == "fail"
    assert report["checks"]["readme_release_design"]["design_status"] == "blocked"
    assert (
        report["checks"]["readme_release_design"]["human_visual_review_required"]
        is True
    )


def test_release_mode_missing_readme_fails_closed_without_crashing(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "README.md").unlink()
    report = MODULE.scan(repo, release=True)
    assert report["status"] == "blocked"
    assert (
        report["checks"]["readme_release_design"]["release_gate"]
        == "blocked_readme_missing"
    )


def test_expected_identity_detects_mismatch(tmp_path: Path):
    report = MODULE.scan(
        make_repo(tmp_path), expected_identity="Release Bot <release@example.invalid>"
    )

    assert report["status"] == "blocked"
    assert report["checks"]["commit_identity"]["status"] == "fail"


def test_deleted_history_secret_blocks_without_echoing_value(tmp_path: Path):
    repo = make_repo(tmp_path)
    token = "github_pat_" + "A" * 30
    (repo / "old.txt").write_text(token, encoding="utf-8")
    git(repo, "add", "old.txt")
    git(repo, "commit", "-m", "add")
    (repo / "old.txt").unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-m", "remove")
    report = MODULE.scan(repo)
    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert token not in str(report)


def test_cli_json_never_echoes_matched_secret(tmp_path: Path):
    repo = make_repo(tmp_path)
    token = "github_pat_" + "Z" * 30
    (repo / "leak.txt").write_text(token, encoding="utf-8")

    result = subprocess.run(
        ["python", str(SCRIPT), "--repo", str(repo), "--json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1
    assert token not in result.stdout
    assert json.loads(result.stdout)["checks"]["secret_scan"]["status"] == "fail"


def test_cli_json_never_echoes_secret_shaped_checkout_path(tmp_path: Path):
    token = "github_pat_" + "Y" * 30
    secret_parent = tmp_path / token
    secret_parent.mkdir()
    repo = make_repo(secret_parent)

    result = subprocess.run(
        ["python", str(SCRIPT), "--repo", str(repo), "--json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert token not in result.stdout
    assert json.loads(result.stdout)["repo"] == "repo"


def test_subdirectory_still_scans_repo_root(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "nested").mkdir()
    (repo / "leak.txt").write_text("sk-" + "B" * 30, encoding="utf-8")
    assert MODULE.scan(repo / "nested")["checks"]["secret_scan"]["status"] == "fail"


def test_remote_credentials_are_redacted(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", "https://TOKEN@example.com/org/repo.git")
    report = MODULE.scan(repo)
    assert report["checks"]["origin"]["url"] == "https://example.com/org/repo.git"
    assert "TOKEN" not in str(report)


def test_scp_remote_credentials_and_query_are_redacted(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "TOKEN@example.com:org/repo.git?credential=SECRET#fragment",
    )

    report = MODULE.scan(repo)

    assert report["checks"]["origin"]["url"] == "example.com:org/repo.git"
    assert "TOKEN" not in str(report)
    assert "SECRET" not in str(report)


def test_ignored_virtual_environment_is_not_scanned(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    dependency = repo / ".venv" / "Lib" / "site-packages" / "dependency.py"
    dependency.parent.mkdir(parents=True)
    personal_path = "C:/Us" + "ers/example"
    dependency.write_text("sk-" + "C" * 30 + f"\n{personal_path}\n", encoding="utf-8")

    report = MODULE.scan(repo)

    assert report["checks"]["secret_scan"]["status"] == "pass"
    assert report["checks"]["personal_path_scan"]["status"] == "pass"


def test_non_ascii_untracked_filename_is_scanned(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "日本語.txt").write_text("sk-" + "D" * 30, encoding="utf-8")

    report = MODULE.scan(repo)

    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert report["checks"]["secret_scan"]["finding_count"] == 1
    assert "files" not in report["checks"]["secret_scan"]


def test_percent_encoded_secret_is_scanned(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "encoded.txt").write_text("sk%2D" + "E" * 30, encoding="utf-8")

    report = MODULE.scan(repo)

    assert report["checks"]["secret_scan"]["status"] == "fail"


def test_secret_bearing_filename_is_redacted_from_evidence(tmp_path: Path):
    repo = make_repo(tmp_path)
    token = "sk-" + "F" * 30
    (repo / f"{token}.txt").write_text(token, encoding="utf-8")

    report = MODULE.scan(repo)

    assert token not in str(report)


def test_missing_repo_returns_sanitized_tool_error(tmp_path: Path):
    missing = tmp_path / "missing-secret-name"

    report = MODULE.scan(missing)

    assert report == {"status": "tool_error", "issues": ["not_git_repository"]}


def test_effective_identity_mismatch_is_reported(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "config", "user.email", "different@example.invalid")

    report = MODULE.scan(
        repo, expected_identity="Test Author <test-author@example.invalid>"
    )

    assert report["checks"]["commit_identity"]["status"] == "fail"
    assert report["checks"]["commit_identity"]["effective_mismatch_count"] == 1


def test_unreadable_working_tree_file_fails_closed(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    target = repo / "unreadable.txt"
    target.write_text("safe", encoding="utf-8")
    original = Path.read_bytes

    def fail_selected(path: Path):
        if path == target:
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_selected)
    report = MODULE.scan(repo)

    assert report["status"] == "tool_error"
    assert report["issues"] == ["worktree_file_unreadable:unreadable.txt"]


def test_malformed_history_batch_returns_tool_error(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    original = subprocess.run

    def malformed_batch(args, *pargs, **kwargs):
        if list(args[:3]) == ["git", "cat-file", "--batch"]:
            return CompletedProcess(args, 0, stdout=b"malformed\n", stderr=b"")
        return original(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", malformed_batch)
    report = MODULE.scan(repo)

    assert report["status"] == "tool_error"
    assert report["issues"] == ["git_history_inventory_failed"]


def test_missing_history_object_returns_tool_error(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    original = subprocess.run

    def missing_object(args, *pargs, **kwargs):
        if list(args[:3]) == ["git", "cat-file", "--batch-check"]:
            request = kwargs["input"].splitlines()[0]
            return CompletedProcess(args, 0, stdout=f"{request} missing\n", stderr="")
        return original(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", missing_object)
    report = MODULE.scan(repo)

    assert report["status"] == "tool_error"
    assert report["issues"] == ["git_history_inventory_failed"]


def test_gitlink_is_not_treated_as_unreadable_file(tmp_path: Path):
    repo = make_repo(tmp_path)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    git(repo, "update-index", "--add", "--cacheinfo", "160000", commit, "vendor")

    report = MODULE.scan(repo)

    assert report["status"] != "tool_error"
    assert report["checks"]["secret_scan"]["status"] == "pass"


def test_report_separates_automated_checks_from_publication_decision(tmp_path: Path):
    report = MODULE.scan(make_repo(tmp_path))

    assert report["status"] == "pass"
    assert report["publication_decision"] == "blocked_human_review_required"
    assert report["checks"]["human_visual_review"]["status"] == "unknown"
    assert report["checks"]["ci_runtime_result"]["status"] == "unknown"


def test_dependency_manifest_is_reported(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[project]\nname='example'\n", encoding="utf-8"
    )

    report = MODULE.scan(repo)

    assert report["checks"]["dependency_configuration"] == {
        "status": "pass",
        "files": ["pyproject.toml"],
    }
    assert report["checks"]["dependency_vulnerability_audit"]["status"] == "unknown"


def test_empty_ci_workflow_fails_configuration_check(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".github" / "workflows" / "ci.yml").write_text("", encoding="utf-8")

    report = MODULE.scan(repo)

    assert report["status"] == "blocked"
    assert report["checks"]["ci_configuration"]["status"] == "fail"
