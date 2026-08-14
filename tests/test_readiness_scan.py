import importlib.util
import json
import subprocess
import sys
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
        body = f"# {name}\n"
        if name == MODULE.REVIEW_RECORD:
            body += f"{MODULE.REVIEW_RECORD_MARKER}\n"
        (repo / name).write_text(body, encoding="utf-8")
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


def set_remote_base(repo: Path, name: str = "main") -> str:
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git(repo, "update-ref", f"refs/remotes/origin/{name}", oid)
    return f"origin/{name}"


def test_output_is_json_without_any_output_flag(tmp_path: Path):
    """出力は常にJSON。formatを選ぶflagは受け付けない。

    以前は --json を定義しながらどこからも参照しておらず、付けないとJSONに
    ならないという誤解を生んでいた。曖昧さを消すためflag自体を削除する。
    """
    repo = make_repo(tmp_path)

    result = subprocess.run(
        ["python", str(SCRIPT), "--repo", str(repo)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "pass"


def test_removed_json_flag_is_rejected(tmp_path: Path):
    repo = make_repo(tmp_path)

    result = subprocess.run(
        ["python", str(SCRIPT), "--repo", str(repo), "--json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 2
    assert "--json" in result.stderr


def test_unrelated_preflight_file_does_not_satisfy_review_record(tmp_path: Path):
    """deployment preflight 等の無関係な同名fileでpassしない (Codex review P2)."""
    repo = make_repo(tmp_path)
    (repo / MODULE.REVIEW_RECORD).write_text(
        "# Deployment preflight\n\n1. drain traffic\n2. run migrations\n",
        encoding="utf-8",
    )

    documents = MODULE.scan(repo)["checks"]["required_documents"]

    assert documents["status"] == "fail"
    assert MODULE.REVIEW_RECORD in documents["invalid"]
    assert MODULE.REVIEW_RECORD not in documents["missing"]


def test_review_record_with_marker_passes(tmp_path: Path):
    documents = MODULE.scan(make_repo(tmp_path))["checks"]["required_documents"]

    assert documents["status"] == "pass"
    assert documents["invalid"] == []


def test_unreadable_review_record_fails_closed(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / MODULE.REVIEW_RECORD).write_bytes(b"\xff\xfe\x00binary")

    documents = MODULE.scan(repo)["checks"]["required_documents"]

    assert documents["status"] == "fail"
    assert MODULE.REVIEW_RECORD in documents["invalid"]


def test_clean_complete_repo_passes(tmp_path: Path):
    assert MODULE.scan(make_repo(tmp_path))["status"] == "pass"


def test_target_diff_ignores_preexisting_repo_baseline_findings(tmp_path: Path):
    repo = make_repo(tmp_path)
    for name in MODULE.REQUIRED:
        (repo / name).unlink()
    (repo / "legacy.txt").write_text(
        "sk-" + "A" * 30 + "\nC:/Us" + "ers/legacy-user/project\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "legacy baseline")
    base = set_remote_base(repo)
    (repo / "safe.txt").write_text("safe target change\n", encoding="utf-8")
    git(repo, "add", "safe.txt")
    git(repo, "commit", "-m", "safe target")

    report = MODULE.scan(repo, base_ref=base)

    assert report["status"] == "pass"
    assert report["scan_scope"]["mode"] == "target_diff"
    assert report["scan_scope"]["base_ref"] == base
    assert report["scan_scope"]["resolved_base_ref"] == "refs/remotes/origin/main"
    assert report["scan_scope"]["base_oid"]
    assert report["checks"]["required_documents"]["status"] == "not_evaluated"
    assert report["checks"]["secret_scan"]["status"] == "pass"
    assert report["checks"]["personal_path_scan"]["status"] == "pass"


def test_target_diff_scans_intermediate_commit_history(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    leaked = repo / "temporary-secret.txt"
    leaked.write_text("github_pat_" + "B" * 30, encoding="utf-8")
    git(repo, "add", leaked.name)
    git(repo, "commit", "-m", "introduce transient secret")
    leaked.unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-m", "remove transient secret")

    report = MODULE.scan(repo, base_ref=base)

    assert report["status"] == "blocked"
    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert report["checks"]["secret_scan"]["finding_count"] >= 1


def test_target_diff_rejects_non_ancestor_base(tmp_path: Path):
    repo = make_repo(tmp_path)

    report = MODULE.scan(repo, base_ref="refs/heads/missing")

    assert report["status"] == "tool_error"
    assert report["issues"] == ["invalid_non_remote_or_non_ancestor_base_ref"]


def test_target_diff_rejects_local_ancestor_ref(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "branch", "narrow-local-base")
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    git(repo, "add", "safe.txt")
    git(repo, "commit", "-m", "safe target")

    report = MODULE.scan(repo, base_ref="narrow-local-base")

    assert report["status"] == "tool_error"
    assert report["issues"] == ["invalid_non_remote_or_non_ancestor_base_ref"]


def test_target_diff_redacts_secret_shaped_base_ref_from_dialogue(tmp_path: Path):
    repo = make_repo(tmp_path)
    token = "github_pat_" + "D" * 30
    base = set_remote_base(repo, token)
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    git(repo, "add", "safe.txt")
    git(repo, "commit", "-m", "safe target")

    options = MODULE.ScanOptions(repo=repo, intent="open_pr", base_ref=base)
    serialized = json.dumps(MODULE.build_intent_dialogue(options))

    assert token not in serialized
    assert "<redacted-path>" in serialized


def test_target_diff_does_not_follow_changed_symlink(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    outside = tmp_path / "outside.txt"
    outside.write_text("github_pat_" + "C" * 30, encoding="utf-8")
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input="../outside.txt",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "outside-link").write_text("../outside.txt", encoding="utf-8")
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},outside-link")
    git(repo, "checkout-index", "--force", "--", "outside-link")
    git(repo, "commit", "-m", "add safe symlink")

    report = MODULE.scan(repo, base_ref=base)

    assert report["status"] == "pass"
    assert report["checks"]["secret_scan"]["status"] == "pass"


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
        ["python", str(SCRIPT), "--repo", str(repo)],
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
        ["python", str(SCRIPT), "--repo", str(repo)],
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


def clear_local_identity(repo: Path, tmp_path: Path, monkeypatch) -> None:
    # system/global configを遮断してもline ending判定が変わらないよう、
    # 現在有効なcore.autocrlfをlocal configへ固定してからidentityを消す
    autocrlf = subprocess.run(
        ["git", "config", "--get", "core.autocrlf"],
        cwd=repo,
        text=True,
        capture_output=True,
    ).stdout.strip()
    git(repo, "config", "core.autocrlf", autocrlf or "false")
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for variable in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_scan_works_without_configured_git_identity(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    clear_local_identity(repo, tmp_path, monkeypatch)

    report = MODULE.scan(repo)

    assert report["status"] == "pass"
    assert report["checks"]["commit_identity"]["status"] == "pass"


def test_missing_identity_with_expected_identity_reports_unknown(
    tmp_path: Path, monkeypatch
):
    repo = make_repo(tmp_path)
    clear_local_identity(repo, tmp_path, monkeypatch)

    report = MODULE.scan(
        repo, expected_identity="Test Author <test-author@example.invalid>"
    )

    assert report["status"] == "blocked"
    # Linux では probe 失敗→unknown。macOS runner 等は username@host を合成し
    # mismatch→fail。どちらも expected_identity 指定時は block で fail-closed。
    assert report["checks"]["commit_identity"]["status"] in {"unknown", "fail"}
    assert report["checks"]["commit_identity"]["effective_identity"] in {
        "unknown",
        "fail",
    }


def test_non_ascii_identity_matches_expected_identity(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "config", "user.name", "日本語 太郎")
    (repo / "extra.txt").write_text("extra\n", encoding="utf-8")
    git(repo, "add", "extra.txt")
    git(repo, "commit", "-m", "add extra")

    report = MODULE.scan(
        repo,
        expected_identity="日本語 太郎 <test-author@example.invalid>",
    )

    assert report["status"] != "tool_error"
    identity = report["checks"]["commit_identity"]
    assert identity["identity_count"] == 2
    assert identity["mismatch_count"] == 1
    assert identity["effective_mismatch_count"] == 0


def test_unexpected_exception_exits_with_tool_error_code(
    tmp_path: Path, monkeypatch, capsys
):
    repo = make_repo(tmp_path)

    def explode(repo_path, expected_identity=None, release=False):
        raise OSError("secret-bearing message")

    monkeypatch.setattr(MODULE, "scan", explode)
    monkeypatch.setattr(sys, "argv", ["readiness_scan.py", "--repo", str(repo)])

    exit_code = MODULE.main()
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert report["status"] == "tool_error"
    assert report["issues"] == ["unexpected_exception:OSError"]
    assert "secret-bearing message" not in json.dumps(report)


def test_non_utf8_log_output_encoding_does_not_corrupt_identity(tmp_path: Path):
    """i18n.logOutputEncoding が非UTF-8でも作者名を誤判定しない (Codex review P2)."""
    repo = make_repo(tmp_path)
    git(repo, "config", "i18n.logOutputEncoding", "ISO-8859-1")
    git(repo, "config", "user.name", "José")
    git(repo, "config", "user.email", "jose@example.invalid")
    (repo / "latin.txt").write_text("latin\n", encoding="utf-8")
    git(repo, "add", "latin.txt")
    git(repo, "commit", "-m", "add latin author commit")

    report = MODULE.scan(repo, expected_identity="José <jose@example.invalid>")

    identity = report["checks"]["commit_identity"]
    assert identity["identity_count"] == 2
    # 追加commitのみ一致想定。初期commitのTest Authorが1件mismatchになる
    assert identity["mismatch_count"] == 1
    assert identity["effective_mismatch_count"] == 0
    assert identity["effective_identity"] == "pass"


def test_successful_identity_probe_mismatch_survives_failed_probe(
    tmp_path: Path, monkeypatch
):
    """片方のprobeが失敗しても、成功したprobeのmismatchを握り潰さない (Codex review P2)."""
    repo = make_repo(tmp_path)
    clear_local_identity(repo, tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Other Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "other@example.invalid")

    report = MODULE.scan(
        repo, expected_identity="Test Author <test-author@example.invalid>"
    )

    identity = report["checks"]["commit_identity"]
    assert identity["status"] == "fail"
    assert identity["effective_identity"] == "fail"
    # author 側 mismatch は必ず残る。committer も合成 identity になる OS では 2 件。
    assert identity["effective_mismatch_count"] >= 1


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
    assert report["checks"]["repository_consistency"]["status"] == "not_configured"


def test_consistency_enforce_blocks_readiness_scan(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".repo-preflight-consistency.json").write_text(
        json.dumps(
            {
                "schema": "repo-preflight.consistency/v1",
                "mode": "enforce",
                "readme_contracts": {"required_paths": ["missing.py"]},
            }
        ),
        encoding="utf-8",
    )
    git(repo, "add", ".repo-preflight-consistency.json")
    git(repo, "commit", "-m", "add consistency config")

    report = MODULE.scan(repo)

    assert report["status"] == "blocked"
    assert report["checks"]["repository_consistency"]["status"] == "fail"


def test_consistency_shadow_does_not_block_readiness_scan(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".repo-preflight-consistency.json").write_text(
        json.dumps(
            {
                "schema": "repo-preflight.consistency/v1",
                "mode": "shadow",
                "readme_contracts": {"required_paths": ["missing.py"]},
            }
        ),
        encoding="utf-8",
    )
    git(repo, "add", ".repo-preflight-consistency.json")
    git(repo, "commit", "-m", "add consistency config")

    report = MODULE.scan(repo)

    assert report["status"] == "pass"
    assert report["checks"]["repository_consistency"]["status"] == "shadow_findings"


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


def test_merge_intent_accepts_remote_base_ref(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--intent",
            "merge",
            "--base-ref",
            "origin/main",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    report = json.loads(result.stdout)
    assert report["intent"] == "merge"
    assert report["scan"]["options"]["mode"] == "target_diff"
    assert report["scan"]["options"]["base_ref"] == "origin/main"


def test_publish_keeps_repo_scan_with_separate_consistency_base(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / ".repo-preflight-consistency.json").write_text(
        json.dumps(
            {
                "schema": "repo-preflight.consistency/v1",
                "mode": "enforce",
                "impact_map": [{"change": ["README.md"], "requires_any": ["tests/**"]}],
            }
        ),
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# Repo\n\n## 目的\n\ntext\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_docs.py").write_text("# evidence\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change docs contract")

    report = MODULE.scan(repo, consistency_base_ref="origin/main")

    assert report["checks"]["required_documents"]["status"] == "pass"
    assert report["checks"]["repository_consistency"]["status"] == "pass"
    assert report["scan_scope"]["mode"] == "repository"
