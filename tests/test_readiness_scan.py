import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from subprocess import CompletedProcess
from urllib.parse import quote

import pytest

from identity_provider_fixture import configure_private_provider
from path_input_fixture import (
    fixture_path,
    fixture_root,
    fixture_relative,
    split_path_source,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readiness_scan.py"
SPEC = importlib.util.spec_from_file_location("readiness_scan", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def git_argv(args) -> list:
    argv = list(args)
    while len(argv) >= 3 and argv[0] == "git" and argv[1] == "-c":
        argv = [argv[0], *argv[3:]]
    return argv


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


def make_release_repo(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path)
    (repo / "README.md").write_text(
        "# Demo\n\nA tool for checking releases.\n\n"
        "## Overview\nCheck releases.\n\n## Features\nInspect files.\n\n"
        "## Quickstart\nAsk your assistant to inspect this repository.\n\n"
        "## Limits\nHuman review remains required.\n",
        encoding="utf-8",
    )
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "document release")
    return repo


def test_release_blocks_readable_but_stale_readme(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    (repo / "CHANGELOG.md").write_text("New release feature.\n", encoding="utf-8")
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-m", "new release without README")
    report = MODULE.scan(repo, release=True)
    assert report["checks"]["readme_release_design"]["status"] == "pass"
    assert report["checks"]["readme_release_freshness"]["status"] == "fail"
    assert report["status"] == "blocked"


def test_release_readme_update_includes_worktree_and_then_committed_content(
    tmp_path: Path,
):
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nCurrent release: 1.1.0.\n",
        encoding="utf-8",
    )
    report = MODULE.scan(repo, release=True, consistency_base_ref="v1.0.0")
    assert report["checks"]["readme_release_freshness"]["status"] == "pass"
    assert report["checks"]["clean_worktree"]["status"] == "fail"
    assert report["consistency_scope"]["resolved_base_ref"] == "<redacted-path>"
    assert (
        report["consistency_scope"]["base_oid"]
        == report["checks"]["readme_release_freshness"]["baseline"]["base_oid"]
    )
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "update release README")
    assert MODULE.scan(repo, release=True)["status"] == "pass"


def test_release_whitespace_only_readme_update_remains_blocked(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("\n", "\n\n"), encoding="utf-8"
    )
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "format only")
    report = MODULE.scan(repo, release=True)
    assert report["status"] == "blocked"
    assert (
        report["checks"]["readme_release_freshness"]["reason"]
        == "release_readme_update_missing"
    )


def test_release_after_candidate_tag_uses_explicit_previous_tag(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nCurrent release: 1.1.0.\n",
        encoding="utf-8",
    )
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "release candidate")
    git(repo, "tag", "v1.1.0")
    report = MODULE.scan(repo, release=True, consistency_base_ref="v1.0.0")
    assert report["status"] == "pass"
    baseline = report["checks"]["readme_release_freshness"]["baseline"]
    assert baseline["resolved_base_ref"] == "<redacted-path>"
    assert baseline["base_oid"] == report["consistency_scope"]["base_oid"]
    current = MODULE.scan(repo, release=True, consistency_base_ref="v1.1.0")
    assert current["status"] == "blocked"
    assert (
        current["checks"]["readme_release_freshness"]["reason"]
        == "release_readme_update_missing"
    )


def test_release_explicit_baseline_sha_drift_fails_closed(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    initial = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    report = MODULE.check_release_readme_freshness(
        repo, baseline=("refs/tags/v1.0.0", initial)
    )
    assert report["status"] == "tool_error"


def test_first_release_still_requires_readme_design(tmp_path: Path):
    repo = make_repo(tmp_path)
    report = MODULE.scan(repo, release=True)
    assert report["checks"]["readme_release_freshness"]["reason"] == "first_release"
    assert report["checks"]["readme_release_design"]["status"] == "fail"
    assert report["status"] == "blocked"


def test_release_consistency_baseline_rejects_missing_and_future_tags(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    (repo / "future.txt").write_text("future\n", encoding="utf-8")
    git(repo, "add", "future.txt")
    git(repo, "commit", "-m", "future")
    git(repo, "tag", "v2.0.0")
    git(repo, "checkout", "--detach", "v1.0.0")
    for ref in ("v9.0.0", "v2.0.0"):
        report = MODULE.scan(repo, release=True, consistency_base_ref=ref)
        assert report["status"] == "tool_error"
        assert report["issues"] == ["invalid_consistency_base_ref"]


def test_tag_permission_is_limited_to_release_consistency_baseline(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    assert MODULE.scan(repo, consistency_base_ref="v1.0.0")["status"] == "tool_error"
    assert MODULE.scan(repo, release=True, base_ref="v1.0.0")["issues"] == [
        "invalid_non_remote_or_non_ancestor_base_ref"
    ]
    assert (
        MODULE.scan(repo, release=True, consistency_base_ref="HEAD")["status"]
        == "tool_error"
    )


def test_release_tag_inventory_error_fails_closed(tmp_path: Path, monkeypatch):
    repo = make_release_repo(tmp_path)
    original = MODULE.run

    def fail_inventory(path, *args):
        if args[:2] == ("git", "for-each-ref"):
            return 1, ""
        return original(path, *args)

    monkeypatch.setattr(MODULE, "run", fail_inventory)
    report = MODULE.scan(repo, release=True)
    assert report["status"] == "tool_error"
    assert report["checks"]["readme_release_freshness"]["status"] == "tool_error"


def test_release_identity_base_separates_pending_commits_from_history(
    tmp_path: Path, monkeypatch
):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    (repo / "legacy.txt").write_text("historical commit\n", encoding="utf-8")
    git(repo, "add", "legacy.txt")
    git(
        repo,
        "-c",
        "user.name=Legacy",
        "-c",
        "user.email=legacy@example.invalid",
        "commit",
        "-m",
        "historical merge equivalent",
    )
    set_remote_base(repo)
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nVersion 1.1.0.\n", encoding="utf-8"
    )
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "document current release")
    expected = "Test Author <test-author@example.invalid>"

    unscoped = MODULE.scan(repo, release=True, expected_identity=expected)
    assert unscoped["checks"]["commit_identity"]["status"] == "fail"
    scoped = MODULE.scan(
        repo, release=True, expected_identity=expected, identity_base_ref="origin/main"
    )
    assert scoped["status"] == "pass"
    assert scoped["checks"]["commit_identity"]["status"] == "pass"
    assert scoped["checks"]["commit_identity"]["identity_count"] == 1
    assert scoped["checks"]["historical_identity_audit"]["mismatch_count"] == 1
    assert scoped["checks"]["secret_scan"]["status"] == "pass"
    assert scoped["checks"]["commit_identity"]["base_ref"] == "refs/remotes/origin/main"
    dialogue = MODULE.build_intent_dialogue(
        MODULE.ScanOptions(
            repo=repo,
            intent="release",
            expected_identity=expected,
            identity_base_ref="origin/main",
        )
    )
    assert dialogue["scan"]["checks"]["commit_identity"]["status"] == "pass"
    assert "review_existing_history_identity" in {
        proposal["id"] for proposal in dialogue["proposals"]
    }


def test_release_identity_base_still_blocks_new_mismatch(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    set_remote_base(repo)
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nVersion 1.1.0.\n", encoding="utf-8"
    )
    git(repo, "add", "README.md")
    git(
        repo,
        "-c",
        "user.name=Legacy",
        "-c",
        "user.email=legacy@example.invalid",
        "commit",
        "-m",
        "wrong release identity",
    )
    report = MODULE.scan(
        repo,
        release=True,
        expected_identity="Test Author <test-author@example.invalid>",
        identity_base_ref="origin/main",
    )
    assert report["status"] == "blocked"
    assert report["checks"]["commit_identity"]["mismatch_count"] == 1


def test_release_identity_base_requires_remote_ancestor_and_release(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    expected = "Test Author <test-author@example.invalid>"
    assert MODULE.scan(
        repo, expected_identity=expected, identity_base_ref="origin/main"
    )["issues"] == ["invalid_identity_base_ref"]
    for ref in ("HEAD", "origin/missing"):
        assert MODULE.scan(
            repo, release=True, expected_identity=expected, identity_base_ref=ref
        )["issues"] == ["invalid_identity_base_ref"]


def test_identity_base_cli_is_release_only(tmp_path: Path):
    repo = make_release_repo(tmp_path)
    for intent in ("publish", "push"):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                str(repo),
                "--intent",
                intent,
                "--expected-identity",
                "Test Author <test-author@example.invalid>",
                "--identity-base-ref",
                "origin/main",
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        assert result.returncode == 2
        assert "--identity-base-ref requires --intent release" in result.stderr


@pytest.mark.parametrize(
    "path",
    [
        fixture_relative("src", "home", "alice", "page.py"),
        fixture_relative("src", "Users", "alice", "page.py"),
        fixture_relative(".", "home", "alice", "page.py"),
        fixture_relative("..", "Users", "alice", "page.py"),
    ],
)
def test_relative_path_components_are_not_personal_paths(path: str):
    data = path.encode("utf-8")
    assert not MODULE.text_has(MODULE.PATH_PATTERNS, data)
    assert not MODULE._bytes_pattern_hit(MODULE.PATH_PATTERNS, data)
    assert not MODULE.personal_path_matches(data)


@pytest.mark.parametrize(
    "option",
    [
        "-I",
        "-L",
        "-F",
        "-o",
        "-isystem",
        "-iquote",
        "-iframework",
        "-include",
        "-imacros",
        "-idirafter",
        "-customoption",
        "--custom-option_42",
    ],
)
@pytest.mark.parametrize(
    "root", [fixture_root("linux").rstrip("/"), fixture_root("macos").rstrip("/")]
)
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_cli_option_absolute_path_keeps_match_hash(
    option: str, root: str, encoding: str
):
    absolute = f"{root}/alice/include"
    data = f"gcc {option}{absolute}".encode(encoding)
    relative = f"src/{option}{absolute}".encode(encoding)
    assert MODULE.text_has(MODULE.PATH_PATTERNS, data)
    if encoding == "utf-8":
        assert MODULE._bytes_pattern_hit(MODULE.PATH_PATTERNS, data)
        assert not MODULE._bytes_pattern_hit(MODULE.PATH_PATTERNS, relative)
    assert MODULE.personal_path_matches(data) == MODULE.personal_path_matches(
        absolute.encode(encoding)
    )
    assert not MODULE.text_has(MODULE.PATH_PATTERNS, relative)
    assert not MODULE.personal_path_matches(relative)


@pytest.mark.parametrize(
    "path",
    [
        (fixture_path("linux", "alice", "page.py")),
        (fixture_path("macos", "alice", "page.py")),
        (fixture_path("windows", "alice", "page.py", forward=True)),
        (fixture_path("windows", "alice", "page.py")),
        ("'" + fixture_path("linux", "alice", "page.py") + "'"),
        ('"' + fixture_path("macos", "alice", "page.py") + '"'),
        ("path=" + fixture_path("linux", "alice", "page.py")),
        ("file://" + fixture_path("linux", "alice", "page.py")),
        ("file://" + fixture_path("macos", "alice", "page.py")),
        quote((fixture_path("linux", "alice", "page.py")), safe=""),
        quote(("file://" + fixture_path("linux", "alice", "page.py")), safe=""),
    ],
)
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_absolute_personal_paths_remain_detected(path: str, encoding: str):
    data = path.encode(encoding)
    assert MODULE.text_has(MODULE.PATH_PATTERNS, data)
    assert MODULE.personal_path_matches(data)


def test_relative_fixture_path_passes_worktree_and_deleted_history(tmp_path: Path):
    repo = make_repo(tmp_path)
    fixture = repo / "fixture.txt"
    fixture.write_text(
        fixture_relative("src", "home", "alice", "page.py") + "\n", encoding="utf-8"
    )
    git(repo, "add", "fixture.txt")
    git(repo, "commit", "-m", "add relative fixture")
    assert MODULE.scan(repo)["checks"]["personal_path_scan"]["status"] == "pass"
    fixture.unlink()
    git(repo, "add", "fixture.txt")
    git(repo, "commit", "-m", "remove relative fixture")
    assert MODULE.scan(repo)["checks"]["personal_path_scan"]["status"] == "pass"


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


def test_configure_settings_cli_uses_profile_and_attaches_live_review(
    tmp_path: Path, monkeypatch
):
    repo = make_repo(tmp_path)

    class SettingsModule:
        PROFILES = ("solo_public", "team_public", "high_risk_public")

        @staticmethod
        def repository_from_repo(path):
            assert path == repo
            return "example/repo"

        @staticmethod
        def review_repository(repository, profile):
            assert repository == "example/repo"
            assert profile == "team_public"
            return {
                "schema_version": "repo-preflight.github-settings-review/v1",
                "status": "pass",
                "repository": repository,
                "profile": profile,
                "settings": [],
                "external_actions_performed": False,
            }

    monkeypatch.setattr(MODULE, "load_github_settings_module", lambda: SettingsModule)
    options = MODULE.ScanOptions(
        repo=repo,
        intent="configure_settings",
        github_settings_profile="team_public",
    )

    dialogue = MODULE.build_intent_dialogue(options)

    assert dialogue["intent"] == "configure_settings"
    assert dialogue["github_settings_review"]["repository"] == "example/repo"
    assert dialogue["github_settings_review"]["profile"] == "team_public"
    assert dialogue["github_settings_review"]["external_actions_performed"] is False
    assert dialogue["scan"] is None


def test_configure_settings_requires_repo_path():
    args = MODULE.build_parser().parse_args(["--intent", "configure_settings"])

    try:
        MODULE.resolve_options(args, stdin_is_tty=False)
    except SystemExit as exc:
        assert "--repo is required" in str(exc)
    else:
        raise AssertionError("configure_settings without --repo must fail closed")


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
        "sk-"
        + "A" * 30
        + "\n"
        + fixture_path("windows", "legacy-user", "project", forward=True)
        + "\n",
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
    assert report["scan_scope"]["base_ref"] == "<redacted-path>"
    assert report["scan_scope"]["resolved_base_ref"] == "<redacted-path>"
    assert report["scan_scope"]["base_oid"]
    assert report["checks"]["required_documents"]["status"] == "not_evaluated"
    assert report["checks"]["secret_scan"]["status"] == "pass"
    assert report["checks"]["personal_path_scan"]["status"] == "pass"


def test_target_diff_ignores_preexisting_path_in_changed_file(tmp_path: Path):
    repo = make_repo(tmp_path)
    registry = repo / "registry.yaml"
    registry.write_text(
        (
            "legacy: "
            + fixture_path("windows", "legacy-user", "project", forward=True)
            + "\n"
        ),
        encoding="utf-8",
    )
    git(repo, "add", registry.name)
    git(repo, "commit", "-m", "add legacy registry")
    base = set_remote_base(repo)
    with registry.open("a", encoding="utf-8") as stream:
        stream.write("canonical: Documents/.repos/example\n")
    git(repo, "add", registry.name)
    git(repo, "commit", "-m", "register canonical path")

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"]["personal_path_scan"]["status"] == "pass"


def test_target_diff_blocks_new_personal_path_line(tmp_path: Path):
    repo = make_repo(tmp_path)
    registry = repo / "registry.yaml"
    registry.write_text("canonical: Documents/.repos/example\n", encoding="utf-8")
    git(repo, "add", registry.name)
    git(repo, "commit", "-m", "add registry")
    base = set_remote_base(repo)
    with registry.open("a", encoding="utf-8") as stream:
        stream.write(
            (
                "checkout: "
                + fixture_path("windows", "new-user", "project", forward=True)
                + "\n"
            )
        )
    git(repo, "add", registry.name)
    git(repo, "commit", "-m", "add personal checkout")

    report = MODULE.scan(repo, base_ref=base)

    assert report["status"] == "blocked"
    assert report["checks"]["personal_path_scan"]["status"] == "fail"


def test_target_diff_detects_personal_path_embedded_in_binary_file(tmp_path: Path):
    """バイナリ内埋め込みの個人pathも検出する (回帰: PR#38レビュー指摘)。

    ``diff_added_lines_have`` の binary fallback は ``_decode_scannable_text``
    がblob全体をUTF-8/UTF-16としてdecodeできて初めて内容を検査する。
    非UTF-8 noiseを含む真のバイナリblob内に個人pathがASCIIで埋め込まれていても
    decode自体が失敗し、そのfileの内容検査が丸ごと ``continue`` で
    スキップされていた (unified=0 diffも 'Binary files ... differ' のみで
    +行を出さないため、二重に見逃す)。
    """
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    # UTF-8/UTF-16 のどちらでも全体decodeが失敗する noise (孤立surrogate) と
    # 個人pathを混在させたバイナリ file。noiseが弱いとUTF-16へのfallbackが
    # 文字化けしつつdecode成功してしまい、本来のバグ経路を再現できない。
    payload = (
        b"\x00\x01\x00\xd8"
        + fixture_path("windows", "newuser", "project", forward=True).encode("ascii")
        + b"\x00\x02noise"
    )
    (repo / "blob.bin").write_bytes(payload)
    git(repo, "add", "blob.bin")
    git(repo, "commit", "-m", "add binary with embedded personal path")

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"]["personal_path_scan"]["status"] == "fail"
    assert report["checks"]["personal_path_scan"]["files"]
    assert report["status"] == "blocked"


def test_target_diff_blocks_added_line_that_looks_like_patch_header(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    (repo / "header-like.txt").write_text(
        ("++ " + fixture_path("windows", "new-user", "project", forward=True) + "\n"),
        encoding="utf-8",
    )
    git(repo, "add", "header-like.txt")
    git(repo, "commit", "-m", "add header-like personal path")

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"]["personal_path_scan"]["status"] == "fail"


def test_target_diff_blocks_personal_path_in_utf16_addition(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    (repo / "utf16.txt").write_text(
        (
            "checkout: "
            + fixture_path("windows", "new-user", "project", forward=True)
            + "\n"
        ),
        encoding="utf-16",
    )
    git(repo, "add", "utf16.txt")
    git(repo, "commit", "-m", "add UTF-16 personal path")

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"]["personal_path_scan"]["status"] == "fail"


def test_target_diff_does_not_execute_textconv(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    marker = tmp_path / "textconv-ran"
    converter = tmp_path / "converter.py"
    converter.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    git(repo, "config", "diff.sideeffect.textconv", f'python "{converter}"')
    (repo / ".gitattributes").write_text("*.custom diff=sideeffect\n", encoding="utf-8")
    (repo / "safe.custom").write_text("safe\n", encoding="utf-8")
    git(repo, "add", ".gitattributes", "safe.custom")
    git(repo, "commit", "-m", "add textconv-classified file")

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"]["personal_path_scan"]["status"] == "pass"
    assert not marker.exists()


def test_target_diff_merge_compares_base_containing_parent(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "branch", "feature")
    (repo / "baseline.txt").write_text(
        (fixture_path("windows", "baseline-user", "project", forward=True) + "\n"),
        encoding="utf-8",
    )
    git(repo, "add", "baseline.txt")
    git(repo, "commit", "-m", "advance remote base")
    base = set_remote_base(repo)
    git(repo, "checkout", "feature")
    (repo / "feature.txt").write_text("safe\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "safe feature")
    git(repo, "merge", "--no-ff", "origin/main", "-m", "merge updated base")

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"]["personal_path_scan"]["status"] == "pass"


def historical_base_merge(repo: Path, feature_body: str = "safe\n") -> tuple[str, str]:
    """古いmainをmergeした後、進んだmainを再びmergeする履歴を作る。"""
    git(repo, "branch", "feature")
    (repo / "baseline.txt").write_text(
        (fixture_path("windows", "baseline-user", "project", forward=True) + "\n"),
        encoding="utf-8",
    )
    git(repo, "add", "baseline.txt")
    git(repo, "commit", "-m", "advance original base")
    old_base = set_remote_base(repo)
    git(repo, "checkout", "feature")
    (repo / "feature.txt").write_text(feature_body, encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feature change")
    # 削除済みのbranch導入内容も履歴上の検査対象に残す。
    (repo / "feature.txt").write_text("safe\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "--allow-empty", "-m", "clean feature tip")
    git(repo, "merge", "--no-ff", old_base, "-m", "merge original base")
    historical_merge = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    git(repo, "checkout", "main")
    (repo / "base-progress.txt").write_text("safe\n", encoding="utf-8")
    git(repo, "add", "base-progress.txt")
    git(repo, "commit", "-m", "advance current base")
    base = set_remote_base(repo)
    git(repo, "checkout", "feature")
    git(repo, "merge", "--no-ff", base, "-m", "merge current base")
    return base, historical_merge


def test_target_diff_historical_merge_uses_unique_base_ancestor(tmp_path: Path):
    repo = make_repo(tmp_path)
    base, historical_merge = historical_base_merge(repo)
    expected = subprocess.check_output(
        ["git", "rev-parse", f"{historical_merge}^2"], cwd=repo, text=True
    ).strip()

    assert MODULE.comparison_parent(repo, base, historical_merge) == expected
    report = MODULE.scan(repo, base_ref=base)
    assert report["checks"]["personal_path_scan"]["status"] == "pass"


@pytest.mark.parametrize(
    ("feature_body", "check"),
    [
        (
            (fixture_path("windows", "feature-user", "project", forward=True) + "\n"),
            "personal_path_scan",
        ),
        ("github_pat_" + "B" * 30 + "\n", "secret_scan"),
    ],
)
def test_target_diff_historical_merge_retains_transient_branch_findings(
    tmp_path: Path, feature_body: str, check: str
):
    repo = make_repo(tmp_path)
    base, _historical_merge = historical_base_merge(repo, feature_body)

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"][check]["status"] == "fail"
    assert report["status"] == "blocked"


def test_comparison_parent_keeps_first_parent_for_ambiguous_base_ancestors(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    _base, historical_merge = historical_base_merge(repo)
    expected = subprocess.check_output(
        ["git", "rev-parse", f"{historical_merge}^1"], cwd=repo, text=True
    ).strip()
    # このbaseは両親の子孫。候補を一意に決められない場合はfirst parentを保つ。
    assert MODULE.comparison_parent(repo, "HEAD", historical_merge) == expected


@pytest.mark.parametrize(
    ("parents", "relations", "expected"),
    [
        ("first", {}, "first"),
        ("first second", {}, "first"),
        # ancestor候補が先にあっても、baseを含む親を優先する。
        ("first second", {("first", "base"): 0, ("base", "second"): 0}, "second"),
    ],
)
def test_comparison_parent_preserves_existing_priority_and_fallback(
    tmp_path: Path, monkeypatch, parents: str, relations: dict, expected: str
):
    def run(command, **_kwargs):
        if command[1] == "show":
            return CompletedProcess(command, 0, stdout=parents)
        return CompletedProcess(command, relations.get(tuple(command[-2:]), 1))

    monkeypatch.setattr(MODULE, "run_subprocess", run)

    assert MODULE.comparison_parent(tmp_path, "base", "merge") == expected


def test_comparison_parent_ancestor_query_error_fails_closed(
    tmp_path: Path, monkeypatch
):
    def run(command, **_kwargs):
        if command[1] == "show":
            return CompletedProcess(command, 0, stdout="first second")
        return CompletedProcess(command, 128 if command[-1] == "base" else 1)

    monkeypatch.setattr(MODULE, "run_subprocess", run)

    with pytest.raises(RuntimeError, match="git_target_diff_inventory_failed"):
        MODULE.comparison_parent(tmp_path, "base", "merge")


def test_target_diff_scans_parentless_commit_against_empty_tree(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    git(repo, "checkout", "--orphan", "imported")
    git(repo, "rm", "-rf", ".")
    (repo / "imported.txt").write_text(
        (fixture_path("windows", "imported-user", "project", forward=True) + "\n"),
        encoding="utf-8",
    )
    git(repo, "add", "imported.txt")
    git(repo, "commit", "-m", "import root")
    git(repo, "checkout", "main")
    git(
        repo,
        "merge",
        "--allow-unrelated-histories",
        "--no-ff",
        "imported",
        "-m",
        "merge import",
    )

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"]["personal_path_scan"]["status"] == "fail"


def test_target_diff_scans_transient_personal_path_in_commit_history(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    path_file = repo / "temporary-path.txt"
    path_file.write_text(
        (fixture_path("windows", "new-user", "project", forward=True) + "\n"),
        encoding="utf-8",
    )
    git(repo, "add", path_file.name)
    git(repo, "commit", "-m", "introduce transient personal path")
    path_file.unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-m", "remove transient personal path")

    report = MODULE.scan(repo, base_ref=base)

    assert report["status"] == "blocked"
    assert report["checks"]["personal_path_scan"]["status"] == "fail"


def test_target_diff_blocks_personal_path_in_untracked_file(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    (repo / "untracked.txt").write_text(
        (fixture_path("windows", "new-user", "project", forward=True) + "\n"),
        encoding="utf-8",
    )

    report = MODULE.scan(repo, base_ref=base)

    assert report["status"] == "blocked"
    assert report["checks"]["personal_path_scan"]["status"] == "fail"


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


@pytest.mark.parametrize("scope", ["base", "consistency", "identity"])
@pytest.mark.parametrize(
    "name,hidden",
    [("ann", True), ("annual", False), ("github_pat_" + "R" * 30, True)],
)
def test_private_ref_labels_are_redacted_without_counting_content(
    tmp_path, monkeypatch, scope, name, hidden
):
    policy = tmp_path / "policy.txt"
    policy.write_text("ann\n")
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    repo = make_repo(tmp_path)
    base = set_remote_base(repo, name)
    (repo / "safe.txt").write_text("safe content\n")
    git(repo, "add", "safe.txt")
    git(repo, "commit", "-m", "safe target")
    kwargs = (
        {"intent": "open_pr", "base_ref": base}
        if scope == "base"
        else (
            {"intent": "publish", "consistency_base_ref": base}
            if scope == "consistency"
            else {
                "intent": "release",
                "release": True,
                "expected_identity": "Test Author <test-author@example.invalid>",
                "identity_base_ref": base,
            }
        )
    )
    report = MODULE.build_intent_dialogue(MODULE.ScanOptions(repo=repo, **kwargs))
    serialized = json.dumps(report)
    assert (base not in serialized) is hidden
    assert report["scan"]["checks"]["private_identity_scan"]["finding_count"] == 0
    if hidden:
        assert "<redacted-path>" in serialized


@pytest.mark.parametrize("policy_state", ["missing", "unreadable", "broken_provider"])
def test_unverified_private_ref_labels_never_fall_back_to_raw(
    tmp_path, monkeypatch, policy_state
):
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.delenv("PUBLIC_IDENTITY_DENYLIST", raising=False)
    if policy_state != "missing":
        policy = tmp_path / "policy.txt"
        if policy_state == "broken_provider":
            policy.write_text("ann\n")
            helper = tmp_path / "broken.py"
            helper.write_text(
                "def parse_denylist_text(text): return ['ann']\n"
                "def scan_private_text(*args): raise ValueError('private-output')\n"
            )
            monkeypatch.setenv("PUBLIC_IDENTITY_GUARD_SCRIPT", str(helper))
        monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    repo = make_repo(tmp_path)
    base = set_remote_base(repo, "ann")
    options = MODULE.ScanOptions(repo=repo, intent="open_pr", base_ref=base)
    serialized = json.dumps(MODULE.build_intent_dialogue(options))
    assert base not in serialized
    assert "private-output" not in serialized
    assert "<redacted-path>" in serialized


def test_ref_options_do_not_reveal_a_label_after_policy_snapshot_changes(
    tmp_path, monkeypatch
):
    policy = tmp_path / "policy.txt"
    policy.write_text("ann\n")
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    repo = make_repo(tmp_path)
    base = set_remote_base(repo, "ann")
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    policy.write_text("different-policy\n")
    enriched = MODULE.enrich_report(
        report, MODULE.ScanOptions(repo=repo, intent="open_pr", base_ref=base)
    )
    assert base not in json.dumps(enriched)
    assert enriched["options"]["base_ref"] == "<redacted-path>"


def test_ref_label_privacy_keeps_content_counters_and_longer_ref_names(
    tmp_path, monkeypatch
):
    policy = tmp_path / "policy.txt"
    policy.write_text("ann\n")
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    scanner = MODULE.PrivateIdentityScan(make_repo(tmp_path), True)
    before = scanner.result()
    labels = {
        "base_ref": "origin/ann",
        "resolved_base_ref": "refs/remotes/origin/ann",
        "git_object_scope": "origin/ann..HEAD",
        "other": "origin/annual",
    }
    sanitized = MODULE.sanitized_ref_report(
        labels,
        ("origin/ann", "refs/remotes/origin/ann", "origin/annual"),
        scanner,
    )
    assert sanitized["base_ref"] == sanitized["resolved_base_ref"] == "<redacted-path>"
    assert sanitized["git_object_scope"] == "<redacted-path>..HEAD"
    assert sanitized["other"] == "origin/annual"
    assert scanner.result() == before


@pytest.mark.parametrize(
    "ref", ["status", "pass", "blocked", "tool_error", "repo-preflight.scan/v3"]
)
def test_ref_label_redaction_preserves_fixed_report_schema_and_values(
    tmp_path, monkeypatch, ref
):
    policy = tmp_path / "policy.txt"
    policy.write_text(ref + "\n")
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    scanner = MODULE.PrivateIdentityScan(make_repo(tmp_path), True)
    report = {
        "status": "pass",
        "schema": "repo-preflight.scan/v3",
        "nested": {"status": "blocked", "reason": "tool_error"},
        "guarantees": [ref + " is explanatory text"],
        "options": {"base_ref": ref},
    }
    sanitized = MODULE.sanitized_ref_report(report, (ref,), scanner)
    assert sanitized["status"] == report["status"]
    assert sanitized["schema"] == report["schema"]
    assert sanitized["nested"] == report["nested"]
    assert sanitized["guarantees"] == report["guarantees"]
    assert sanitized["options"]["base_ref"] == "<redacted-path>"


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


def shallow_clone_with_deleted_secret(tmp_path: Path) -> tuple[Path, Path, str]:
    source = make_repo(tmp_path)
    token = "github_pat_" + "A" * 30
    (source / "old.txt").write_text(token, encoding="utf-8")
    git(source, "add", "old.txt")
    git(source, "commit", "-m", "add historical secret")
    (source / "old.txt").unlink()
    git(source, "add", "-u")
    git(source, "commit", "-m", "remove historical secret")
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--depth", "1", source.as_uri(), str(shallow))
    return source, shallow, token


def test_shallow_clone_cannot_pass_full_history_scan(tmp_path: Path):
    source, shallow, token = shallow_clone_with_deleted_secret(tmp_path)
    shallow_probe = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=shallow,
        check=True,
        capture_output=True,
        text=True,
    )
    assert shallow_probe.stdout.strip() == "true"
    assert MODULE.scan(source)["checks"]["secret_scan"]["status"] == "fail"

    report = MODULE.scan(shallow)

    assert report["status"] == "tool_error"
    assert report["issues"] == ["shallow_history_unavailable"]
    assert report["repo"] == shallow.name
    assert "checks" not in report
    assert token not in json.dumps(report)


def test_shallow_clone_blocks_publish_dialogue(tmp_path: Path, capsys):
    _source, shallow, token = shallow_clone_with_deleted_secret(tmp_path)

    exit_code = MODULE.main(
        ["--repo", str(shallow), "--intent", "publish", "--audience", "public"],
        stdin_is_tty=False,
    )
    output = capsys.readouterr().out
    dialogue = json.loads(output)

    assert exit_code == 1
    assert dialogue["status"] == "blocked"
    assert dialogue["scan"]["status"] == "tool_error"
    assert dialogue["scan"]["issues"] == ["shallow_history_unavailable"]
    assert "fix_tool_error" in {item["id"] for item in dialogue["proposals"]}
    assert token not in output


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(128, ""), (0, "--is-shallow-repository\n")],
    ids=["probe_failed", "option_unrecognized"],
)
def test_unknown_shallow_state_returns_tool_error(
    tmp_path: Path, monkeypatch, returncode: int, stdout: str
):
    repo = make_repo(tmp_path)
    original = subprocess.run

    def unknown_shallow_state(args, *pargs, **kwargs):
        if "--is-shallow-repository" in args:
            return CompletedProcess(args, returncode, stdout=stdout, stderr="")
        return original(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", unknown_shallow_state)
    report = MODULE.scan(repo)

    assert report["status"] == "tool_error"
    assert report["issues"] == ["git_history_inventory_failed"]


def test_history_scan_does_not_parse_object_path_text(monkeypatch, tmp_path: Path):
    """履歴pathのCR/LF断片をobject IDとして誤解しない。"""
    object_id = hashlib.sha1(b"blob 8\0ordinary", usedforsecurity=False).hexdigest()
    commands: list[list[str]] = []

    def fake_run(args, **kwargs):
        commands.append(args)
        assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
        if "--is-shallow-repository" in args:
            return CompletedProcess(args, 0, stdout="false", stderr="")
        if "--git-path" in args:
            return CompletedProcess(args, 0, stdout=".git/info/grafts", stderr="")
        if "--revs-only" in args:
            return CompletedProcess(args, 0, stdout=object_id, stderr="")
        if git_argv(args)[:3] == ["git", "rev-list", "--objects"]:
            output = (
                f"{object_id}\n".encode()
                if "--no-object-names" in args
                else f"{object_id} historical\r/path-fragment\n".encode()
            )
            return CompletedProcess(args, 0, stdout=output, stderr=b"")
        if git_argv(args)[:3] == ["git", "cat-file", "--batch-check"]:
            return CompletedProcess(args, 0, stdout=f"{object_id} blob 8\n", stderr="")
        if git_argv(args)[:3] == ["git", "cat-file", "--batch"]:
            return CompletedProcess(
                args, 0, stdout=f"{object_id} blob 8\nordinary\n".encode(), stderr=b""
            )
        raise AssertionError(args)

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    assert MODULE.history_hits(tmp_path) == ([], [])
    inventory = next(
        cmd for cmd in commands if git_argv(cmd)[:3] == ["git", "rev-list", "--objects"]
    )
    assert "--no-object-names" in inventory
    assert inventory[-1] == object_id


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


def test_subdirectory_cannot_be_used_to_dodge_a_repo_root_secret(tmp_path: Path):
    """subdirectory を指しても、repo root の secret を素通りさせない。

    以前は root へ暗黙に広げて検査することでこれを担保していたが、それだと
    利用者が指したのと別 repository の判定を返してしまうため、今は閉じて止める。
    どちらにせよ pass は返らない、が守るべき不変条件。
    """
    repo = make_repo(tmp_path)
    (repo / "nested").mkdir()
    (repo / "leak.txt").write_text("sk-" + "B" * 30, encoding="utf-8")

    report = MODULE.scan(repo / "nested")

    assert report["status"] != "pass"
    assert report["issues"] == ["repo_path_is_not_repository_root"]


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
    personal_path = fixture_path("windows", "example", forward=True)
    dependency.write_text("sk-" + "C" * 30 + f"\n{personal_path}\n", encoding="utf-8")

    report = MODULE.scan(repo)

    assert report["checks"]["secret_scan"]["status"] == "pass"
    assert report["checks"]["personal_path_scan"]["status"] == "pass"


def make_cmo3(xml: bytes) -> bytes:
    """Live2D Cubism の .cmo3 と同じ包み方をした最小のfileを作る。

    実物は ``CAFF`` 先頭で、全byteを 0xE3 で XOR すると途中に zip の
    ローカルファイルヘッダ (名前 ``contents``、method 8、flag 0x0808 のため
    ヘッダ上のサイズは0) が現れ、その直後が raw deflate の XML になる。
    ヘッダ位置は固定ではないので、前に余分なbyteを置いて位置決め打ちを落とす。
    """
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = compressor.compress(xml) + compressor.flush()
    name = b"contents"
    local_header = struct.pack(
        "<IHHHHHIIIHH", 0x04034B50, 20, 0x0808, 8, 0, 0, 0, 0, 0, len(name), 0
    )
    descriptor = struct.pack(
        "<IIII", 0x08074B50, zlib.crc32(xml), len(deflated), len(xml)
    )
    body = b"\x00" * 37 + local_header + name + deflated + descriptor
    return b"CAFF" + bytes(byte ^ 0xE3 for byte in body)


def cmo3_xml(psd_path: str) -> bytes:
    rows = "".join(f'<i xs.n="n{i}">{i * 7919}</i>\r\n' for i in range(300))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n<root>\r\n'
        f'{rows}<file xs.n="psdFile">{psd_path}</file>\r\n</root>\r\n'
    ).encode("utf-8")


def add_model(repo: Path, data: bytes, name: str = "avatar.cmo3") -> str:
    model = repo / "model" / name
    model.parent.mkdir(exist_ok=True)
    model.write_bytes(data)
    rel = f"model/{name}"
    git(repo, "add", rel)
    git(repo, "commit", "-m", f"add {name}")
    return rel


@pytest.mark.parametrize(
    "psd_path",
    [
        (fixture_path("macos", "cmo3-owner", "art", "avatar.psd")),
        (fixture_path("windows", "cmo3-owner", "art", "avatar.psd")),
    ],
)
def test_personal_path_inside_cmo3_is_detected_without_echoing_it(
    tmp_path: Path, psd_path: str
):
    """Cubism の .cmo3 内部の個人pathを検出し、利用者名は出力しない。

    .cmo3 は XOR + raw deflate で包まれているため、生byteにも UTF-8/UTF-16 の
    全体decodeにも個人pathが現れない。以前は ``psdFile`` 要素に Mac の絶対path
    が入った .cmo3 を pass させていた。
    """
    repo = make_repo(tmp_path)
    rel = add_model(repo, make_cmo3(cmo3_xml(psd_path)))

    result = subprocess.run(
        ["python", str(SCRIPT), "--repo", str(repo)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "blocked"
    assert report["checks"]["personal_path_scan"]["status"] == "fail"
    assert report["checks"]["personal_path_scan"]["files"] == [rel]
    assert "cmo3-owner" not in result.stdout
    assert "cmo3-owner" not in result.stderr


def test_cmo3_without_personal_path_passes(tmp_path: Path):
    repo = make_repo(tmp_path)
    add_model(repo, make_cmo3(cmo3_xml("art/avatar.psd")))

    report = MODULE.scan(repo)

    assert report["status"] == "pass"
    assert report["checks"]["personal_path_scan"]["status"] == "pass"
    assert report["checks"]["personal_path_scan"]["unscanned_files"] == []


@pytest.mark.parametrize(
    "damage",
    ["no_zip_entry", "truncated_deflate", "corrupted_deflate"],
)
def test_unreadable_cmo3_is_unknown_instead_of_pass(tmp_path: Path, damage: str):
    """展開できない .cmo3 を「個人pathなし」と扱わず、検査できなかったと出す。"""
    repo = make_repo(tmp_path)
    intact = make_cmo3(cmo3_xml("art/avatar.psd"))
    if damage == "no_zip_entry":
        data = b"CAFF" + b"\x00" * 64
    elif damage == "truncated_deflate":
        data = intact[:-40]
    else:
        middle = len(intact) // 2
        data = intact[:middle] + b"\xff" * 16 + intact[middle + 16 :]
    rel = add_model(repo, data, "broken.cmo3")

    report = MODULE.scan(repo)

    check = report["checks"]["personal_path_scan"]
    assert check["status"] == "unknown"
    assert check["files"] == []
    assert check["unscanned_files"] == [rel]
    assert report["status"] == "blocked"


def test_publish_dialogue_carries_unreadable_cmo3_as_blocking_scan(
    tmp_path: Path, monkeypatch
):
    configure_private_provider(tmp_path, monkeypatch)
    """公開直前の対話packetでも、検査できなかった .cmo3 を pass に丸めない。"""
    repo = make_repo(tmp_path)
    rel = add_model(repo, b"CAFF" + b"\x00" * 64, "broken.cmo3")

    dialogue = MODULE.build_intent_dialogue(
        MODULE.ScanOptions(repo=repo, audience="public", intent="publish")
    )

    assert dialogue["scan"]["status"] == "blocked"
    assert dialogue["scan"]["checks"]["personal_path_scan"]["unscanned_files"] == [rel]
    assert dialogue["status"] != "ready_after_confirmation"


def test_cmo3_expanding_past_the_size_cap_is_unknown(tmp_path: Path, monkeypatch):
    """展開量の上限を超えたら途中までの結果で pass にしない。"""
    monkeypatch.setattr(MODULE, "CMO3_MAX_XML_BYTES", 64)
    repo = make_repo(tmp_path)
    rel = add_model(repo, make_cmo3(cmo3_xml("art/avatar.psd")))

    check = MODULE.scan(repo)["checks"]["personal_path_scan"]

    assert check["status"] == "unknown"
    assert check["unscanned_files"] == [rel]


def test_cmo3_checks_every_contents_entry(tmp_path: Path):
    repo = make_repo(tmp_path)
    clean = make_cmo3(cmo3_xml("art/avatar.psd"))
    leaking = make_cmo3(
        cmo3_xml((fixture_path("macos", "cmo3-owner", "art", "avatar.psd")))
    )
    rel = add_model(repo, clean + leaking[len(b"CAFF") :])

    check = MODULE.scan(repo)["checks"]["personal_path_scan"]

    assert check["status"] == "fail"
    assert check["files"] == [rel]


def test_cmo3_size_cap_covers_all_entries_together(tmp_path: Path, monkeypatch):
    """entry を並べて上限を entry 数倍に膨らませられないようにする。"""
    xml = cmo3_xml("art/avatar.psd")
    monkeypatch.setattr(MODULE, "CMO3_MAX_XML_BYTES", len(xml) + len(xml) // 2)
    repo = make_repo(tmp_path)
    entry = make_cmo3(xml)
    rel = add_model(repo, entry + entry[len(b"CAFF") :])

    check = MODULE.scan(repo)["checks"]["personal_path_scan"]

    assert check["status"] == "unknown"
    assert check["unscanned_files"] == [rel]


def test_human_report_counts_unscanned_files():
    line = MODULE.format_check_line(
        "personal_path_scan",
        {"status": "unknown", "files": [], "unscanned_files": ["model/a.cmo3"]},
    )

    assert line == "- personal_path_scan: unknown (unscanned=1)"


def test_sparse_checkout_skips_non_materialized_tracked_files(tmp_path: Path):
    repo = make_repo(tmp_path)
    excluded = repo / "excluded" / "historical.txt"
    excluded.parent.mkdir()
    excluded.write_text("tracked but not materialized", encoding="utf-8")
    git(repo, "add", "excluded/historical.txt")
    git(repo, "commit", "-m", "add excluded path")
    git(repo, "sparse-checkout", "init", "--no-cone")
    git(repo, "sparse-checkout", "set", "--no-cone", "/*", "!/excluded/")

    assert not excluded.exists()
    assert MODULE.scan(repo)["status"] == "pass"


def test_materialized_skip_worktree_file_is_still_scanned(tmp_path: Path):
    repo = make_repo(tmp_path)
    hidden = repo / "hidden.txt"
    hidden.write_text("safe", encoding="utf-8")
    git(repo, "add", hidden.name)
    git(repo, "commit", "-m", "add hidden path")
    git(repo, "update-index", "--skip-worktree", hidden.name)
    hidden.write_text("sk-" + "S" * 30, encoding="utf-8")

    report = MODULE.scan(repo)

    assert report["checks"]["secret_scan"]["status"] == "fail"


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


def non_utf8_text(value: str, encoding: str) -> bytes:
    prefix = {"cp932": "メモ: ", "latin-1": "café: "}[encoding]
    return (prefix + value + "\n").encode(encoding)


@pytest.mark.parametrize("encoding", ["cp932", "latin-1"])
def test_non_utf8_worktree_secret_is_scanned(tmp_path: Path, encoding: str):
    repo = make_repo(tmp_path)
    token = "AKIA" + "G" * 16
    (repo / "notes.txt").write_bytes(non_utf8_text(token, encoding))
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-m", "add non-UTF-8 notes")

    report = MODULE.scan(repo)

    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert report["checks"]["secret_scan"]["finding_count"] == 2
    assert token not in json.dumps(report)


@pytest.mark.parametrize("encoding", ["cp932", "latin-1"])
def test_non_utf8_worktree_personal_path_is_scanned(tmp_path: Path, encoding: str):
    repo = make_repo(tmp_path)
    (repo / "notes.txt").write_bytes(
        non_utf8_text((fixture_path("windows", "taro", "notes")), encoding)
    )
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-m", "add non-UTF-8 notes")
    blob = subprocess.check_output(
        ["git", "rev-parse", "HEAD:notes.txt"], cwd=repo, text=True
    ).strip()

    check = MODULE.scan(repo)["checks"]["personal_path_scan"]

    assert check["status"] == "fail"
    assert check["files"] == ["notes.txt", f"history:{blob[:12]}"]


@pytest.mark.parametrize("encoding", ["cp932", "latin-1"])
def test_non_utf8_findings_in_deleted_history_are_scanned(
    tmp_path: Path, encoding: str
):
    repo = make_repo(tmp_path)
    token = "AKIA" + "H" * 16
    old = repo / "old.txt"
    old.write_bytes(
        non_utf8_text(token + "\n" + fixture_path("windows", "taro", "notes"), encoding)
    )
    git(repo, "add", old.name)
    git(repo, "commit", "-m", "add non-UTF-8 notes")
    blob = subprocess.check_output(
        ["git", "rev-parse", "HEAD:old.txt"], cwd=repo, text=True
    ).strip()
    old.unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-m", "remove non-UTF-8 notes")

    report = MODULE.scan(repo)

    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert report["checks"]["secret_scan"]["finding_count"] == 1
    assert report["checks"]["personal_path_scan"]["status"] == "fail"
    assert report["checks"]["personal_path_scan"]["files"] == [f"history:{blob[:12]}"]
    assert token not in json.dumps(report)


def test_reviewed_exceptions_still_detect_uncovered_non_utf8_secret(tmp_path: Path):
    repo = make_repo(tmp_path)
    reviewed = "sk-" + "a" * 24
    token = "AKIA" + "J" * 16
    data = non_utf8_text(token, "cp932")
    (repo / "reviewed.txt").write_text(reviewed, encoding="utf-8")
    (repo / "notes.txt").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add reviewed fixture and non-UTF-8 notes")
    config = tmp_path / "exceptions.json"
    entry = {
        "id": "reviewed_fixture",
        "repo": "example/repo",
        "path": "reviewed.txt",
        "content_sha256": hashlib.sha256(reviewed.encode()).hexdigest(),
        "rule": "openai_key",
        "match_sha256": hashlib.sha256(reviewed.encode()).hexdigest(),
        "occurrence_count": 1,
        "review_reason": "合成fixtureをレビュー済み",
    }
    config.write_text(json.dumps({"version": 1, "entries": [entry]}), encoding="utf-8")

    check = MODULE.scan(repo, reviewed_secret_exceptions=config)["checks"][
        "secret_scan"
    ]

    assert check["status"] == "fail"
    assert check["finding_count"] == 2
    assert check["reviewed_exceptions"] == [
        {"id": "reviewed_fixture", "occurrence_count": 2}
    ]
    assert MODULE.secret_matches(data) == {
        ("aws_access_key", hashlib.sha256(token.encode()).hexdigest()): 1
    }


@pytest.mark.parametrize("separate_file", [False, True])
def test_target_diff_non_utf8_addition_does_not_hide_personal_paths(
    tmp_path: Path, separate_file: bool
):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    personal_path = fixture_path("windows", "new-user", "project", forward=True)
    if separate_file:
        (repo / "memo.txt").write_bytes(non_utf8_text("safe", "cp932"))
        (repo / "checkout.txt").write_text(personal_path + "\n", encoding="utf-8")
    else:
        (repo / "memo.txt").write_bytes(non_utf8_text(personal_path, "cp932"))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add non-UTF-8 memo")

    report = MODULE.scan(repo, base_ref=base)

    assert report["checks"]["personal_path_scan"]["status"] == "fail"


def test_secret_in_non_utf8_filename_is_redacted_from_evidence():
    token = "sk-" + "K" * 30
    raw_name = "メモ".encode("cp932") + token.encode() + b".txt"
    label = raw_name.decode("utf-8", errors="surrogateescape")

    assert MODULE.sanitized_evidence_label(label) == "<redacted-path>"


# 合成値。実在の鍵ではない。旧形式 (sk- + 英数字48) と project 形式 (sk-proj-...)。
OPENAI_LEGACY_SHAPE = "sk-" + "Xy9" * 16
OPENAI_PROJECT_SHAPE = "sk-" + "proj-" + "Ab3_" * 10


@pytest.mark.parametrize(
    "text",
    [
        "task-" + "orchestra-2026-09-05",
        '{"id": "task-' + 'orchestra-2026-09-05", "status": "resolved"}',
        "references/task-" + "orchestra-theme-sweep.md",
        "elon-musk-" + "predicts-ai-will-surpass-humans",
    ],
)
def test_openai_rule_ignores_sk_inside_a_word(text: str):
    """英単語の途中の "sk-" ("task-" / "musk-") を OpenAI 形式の鍵と取り違えない。"""
    data = text.encode("utf-8")

    assert MODULE.secret_matches(data) == {}
    assert MODULE.text_has(MODULE.SECRET_PATTERNS, data) is False


@pytest.mark.parametrize("key", [OPENAI_LEGACY_SHAPE, OPENAI_PROJECT_SHAPE])
@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "note\n",
        "    ",
        "token ",
        '"',
        "'",
        "`",
        "(",
        "/",
        "key=",
        "OPENAI_API_KEY=",
        "api_key: ",
        "Authorization: Bearer ",
        # JSON や文字列 literal の空白 escape の直後 (例: "line1\nsk-...")
        "line1\\n",
        "line1\\r",
        "col1\\t",
    ],
)
def test_openai_rule_still_detects_key_after_a_boundary(prefix: str, key: str):
    data = (prefix + key + "\n").encode("utf-8")

    matches = MODULE.secret_matches(data)

    assert list(matches.elements()) == [
        ("openai_key", hashlib.sha256(key.encode("utf-8")).hexdigest())
    ]


@pytest.mark.parametrize("glue", ["x", "9", "_", "-"])
def test_openai_rule_does_not_match_sk_glued_to_a_word_character(glue: str):
    """意図した取りこぼし: 直前が英数字・"_"・"-" の "sk-" は検出しない。

    単語の途中の "sk-" を拾わないための代償で、この形に貼り付いた鍵は見逃す。
    他の規則 (gh*_ / github_pat_ / AKIA / xox*-) はこの制限を持たない。
    """
    assert MODULE.secret_matches((glue + OPENAI_LEGACY_SHAPE).encode("utf-8")) == {}


def test_task_slug_ledger_passes_secret_scan_in_tree_and_history(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    ledger = repo / "ledger.json"
    slug = "task-" + "orchestra-2026-09-05"
    ledger.write_text(json.dumps({"id": slug, "status": "open"}), encoding="utf-8")
    git(repo, "add", ledger.name)
    git(repo, "commit", "-m", "add ledger")
    ledger.write_text(json.dumps({"id": slug, "status": "done"}), encoding="utf-8")
    git(repo, "add", ledger.name)
    git(repo, "commit", "-m", "resolve ledger entry")

    for report in (MODULE.scan(repo), MODULE.scan(repo, base_ref=base)):
        assert report["checks"]["secret_scan"]["status"] == "pass"

    (repo / "settings.txt").write_text(
        "OPENAI_API_KEY=" + OPENAI_LEGACY_SHAPE + "\n", encoding="utf-8"
    )
    git(repo, "add", "settings.txt")
    git(repo, "commit", "-m", "add key")

    for report in (MODULE.scan(repo), MODULE.scan(repo, base_ref=base)):
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


def test_subdirectory_of_a_repo_does_not_silently_scan_the_parent(tmp_path: Path):
    """--repo に非repositoryのsubdirectoryを渡したら、囲っているrepoへ勝手に広げない。

    git rev-parse --show-toplevel は「そのpathを含むrepo」を返すため、放置すると
    利用者が指したのと別のrepositoryの判定を、指したpathの判定として返してしまう。
    """
    repo = make_repo(tmp_path)
    subdir = repo / "deploy-site"
    subdir.mkdir()
    (subdir / "index.html").write_text("<html></html>", encoding="utf-8")

    report = MODULE.scan(subdir)

    assert report["status"] == "tool_error"
    assert report["issues"] == ["repo_path_is_not_repository_root"]
    # 解決先を伝えないと利用者は再実行のしようがない。ただし個人pathは出さない
    assert report["repository_root"] == repo.name
    assert "checks" not in report


def test_repository_root_evidence_does_not_leak_the_absolute_path(tmp_path: Path):
    """解決先を伝えるときも、個人pathを含む絶対pathは出さない。"""
    repo = make_repo(tmp_path)
    subdir = repo / "deploy-site"
    subdir.mkdir()

    report = MODULE.scan(subdir)

    assert str(tmp_path) not in json.dumps(report)
    assert not MODULE.text_has(MODULE.PATH_PATTERNS, json.dumps(report).encode("utf-8"))


def test_worktree_root_is_accepted_as_repository_root(tmp_path: Path):
    """git worktree の root は .git が file でも repository root として扱う。"""
    repo = make_repo(tmp_path)
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-b", "probe", str(worktree))

    report = MODULE.scan(worktree)

    assert report["status"] != "tool_error"


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

    def explode(
        repo_path, expected_identity=None, release=False, require_private_identity=False
    ):
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
        if git_argv(args)[:3] == ["git", "cat-file", "--batch"]:
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
        if git_argv(args)[:3] == ["git", "cat-file", "--batch-check"]:
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
    assert report["scan"]["options"]["base_ref"] == "<redacted-path>"


def test_publish_keeps_repo_scan_with_separate_consistency_base(
    tmp_path: Path, monkeypatch
):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    token = "github_pat_" + "Q" * 30
    (repo / "legacy-secret.txt").write_text(token, encoding="utf-8")
    git(repo, "add", "legacy-secret.txt")
    git(repo, "commit", "-m", "legacy secret")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "legacy-secret.txt").unlink()
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
    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert token not in str(report)
    assert report["checks"]["repository_consistency"]["status"] == "pass"
    assert report["scan_scope"]["mode"] == "repository"
    assert report["consistency_scope"]["mode"] == "target_diff"
    assert report["consistency_scope"]["base_ref"] == "origin/main"
    assert report["consistency_scope"]["base_oid"]


def test_consistency_base_ref_is_restricted_to_repository_wide_intents(tmp_path: Path):
    repo = make_repo(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--intent",
            "push",
            "--consistency-base-ref",
            "origin/main",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "requires publish/release intent or a plain scan" in result.stderr


def test_base_ref_and_consistency_base_ref_are_exclusive(tmp_path: Path):
    repo = make_repo(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--intent",
            "release",
            "--base-ref",
            "origin/main",
            "--consistency-base-ref",
            "origin/main",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "are exclusive" in result.stderr


def test_history_hits_with_empty_revision_range_returns_no_hits(tmp_path: Path):
    repo = make_repo(tmp_path)
    assert MODULE.history_hits(repo, ("HEAD..HEAD",)) == ([], [])


def test_configure_settings_does_not_widen_to_the_enclosing_repository(tmp_path: Path):
    """configure_settings に subdirectory を渡しても囲っている repo を対象にしない。

    build_dialogue は configure_settings で local scan を意図的に飛ばすため、
    scan() 側の root 検証だけでは素通りする。origin 解決の前に閉じる側で止める。
    """
    repo = make_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", "https://github.com/someone/OUTER.git")
    subdir = repo / "nested"
    subdir.mkdir()

    dialogue = MODULE.build_intent_dialogue(
        MODULE.ScanOptions(repo=subdir, intent="configure_settings")
    )

    assert dialogue["status"] == "blocked"
    ids = {item["id"] for item in dialogue["proposals"]}
    assert "fix_repo_path_not_repository_root" in ids
    # 別 repository の識別子が packet のどこにも載らないことが本体
    assert "OUTER" not in json.dumps(dialogue, ensure_ascii=False)


def test_configure_settings_still_reviews_a_real_repository_root(tmp_path: Path):
    """root を指した通常経路は従来どおり settings review へ進む。"""
    repo = make_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", "https://github.com/someone/OUTER.git")

    dialogue = MODULE.build_intent_dialogue(
        MODULE.ScanOptions(repo=repo, intent="configure_settings")
    )

    ids = {item["id"] for item in dialogue["proposals"]}
    assert "fix_repo_path_not_repository_root" not in ids
    assert dialogue["github_settings_review"]["repository"] == "someone/OUTER"


def test_repository_root_error_returns_none_for_a_real_root(tmp_path: Path):
    repo = make_repo(tmp_path)
    assert MODULE.repository_root_error(repo) is None


def test_git_dir_env_cannot_substitute_another_repository(tmp_path: Path, monkeypatch):
    """GIT_DIR で別 repository を指しても、--repo の判定を差し替えない。

    subdirectory を囲っている repo へ広げるのと同じ取り違えの変種。
    親プロセスの GIT_DIR が別 repo の object DB を指すと、secret 判定まで
    差し替わる。#44 の保証「指定 path と異なる repository の判定を返さない」
    の残ギャップ。
    """
    clean_home = tmp_path / "clean"
    dirty_home = tmp_path / "dirty"
    clean_home.mkdir()
    dirty_home.mkdir()
    clean = make_repo(clean_home)
    dirty = make_repo(dirty_home)
    (dirty / "leak.txt").write_text("sk-" + "D" * 30, encoding="utf-8")
    git(dirty, "add", "leak.txt")
    git(dirty, "commit", "-m", "leak")

    clean_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clean,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=dirty,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert clean_head != dirty_head

    monkeypatch.setenv("GIT_DIR", str(dirty / ".git"))
    report = MODULE.scan(clean)

    assert report["head"] == clean_head
    assert report["head"] != dirty_head
    assert report["checks"]["secret_scan"]["status"] == "pass"
    assert report.get("repo") == clean.name


def test_git_dir_env_cannot_make_a_non_repo_look_scannable(tmp_path: Path, monkeypatch):
    """GIT_DIR があっても、.git の無い path を repository として通さない。"""
    outer = make_repo(tmp_path)
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    (lonely / "x.txt").write_text("hi", encoding="utf-8")

    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    report = MODULE.scan(lonely)

    assert report["status"] == "tool_error"
    assert report["issues"] == ["not_git_repository"]
    assert "checks" not in report


def test_configure_settings_origin_ignores_git_dir_override(
    tmp_path: Path, monkeypatch
):
    """configure_settings の origin 解決も GIT_DIR で差し替えない。"""
    target_home = tmp_path / "target"
    other_home = tmp_path / "other"
    target_home.mkdir()
    other_home.mkdir()
    target = make_repo(target_home)
    other = make_repo(other_home)
    git(target, "remote", "set-url", "origin", "https://github.com/someone/TARGET.git")
    git(other, "remote", "set-url", "origin", "https://github.com/someone/OTHER.git")

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    dialogue = MODULE.build_intent_dialogue(
        MODULE.ScanOptions(repo=target, intent="configure_settings")
    )

    packet = json.dumps(dialogue, ensure_ascii=False)
    assert "OTHER" not in packet
    assert dialogue["github_settings_review"]["repository"] == "someone/TARGET"


def test_plain_scan_accepts_consistency_base_ref_without_release(tmp_path: Path):
    """impact_map を持つ repo の plain scan は --consistency-base-ref で scope を上書きできる。

    既定では origin/main を試すが、明示指定も引き続き plain scan で受け付ける。
    --release は README release gate を足すだけなので、整合性 scope の代替にはならない。
    """
    repo = make_repo(tmp_path)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / ".repo-preflight-consistency.json").write_text(
        json.dumps(
            {
                "schema": "repo-preflight.consistency/v1",
                "mode": "shadow",
                "impact_map": [{"change": ["README.md"], "requires_any": ["docs/**"]}],
            }
        ),
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add change-sensitive consistency config")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--consistency-base-ref",
            "origin/main",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert result.returncode != 2, result.stderr
    report = json.loads(result.stdout)
    assert report["scan_scope"]["mode"] == "repository"  # repo 全体 scan を狭めていない
    assert report["checks"]["repository_consistency"]["status"] != "tool_error"
    assert (
        "readme_release_design" not in report["checks"]
    )  # --release を暗黙に足していない


def test_plain_scan_defaults_consistency_base_to_origin_main(tmp_path: Path):
    """最短コマンド（base 未指定）でも impact_map 付きで整合性が tool_error に落ちない。"""
    repo = make_repo(tmp_path)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / ".repo-preflight-consistency.json").write_text(
        json.dumps(
            {
                "schema": "repo-preflight.consistency/v1",
                "mode": "shadow",
                "impact_map": [{"change": ["README.md"], "requires_any": ["docs/**"]}],
            }
        ),
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add change-sensitive consistency config")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert result.returncode != 2, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] != "tool_error"
    assert report["scan_scope"]["mode"] == "repository"
    assert report["consistency_scope"]["mode"] == "target_diff"
    assert report["consistency_scope"]["base_ref"] == "<redacted-path>"
    assert report["consistency_scope"]["base_oid"]
    assert report["checks"]["repository_consistency"]["status"] != "tool_error"
    assert "change_sensitive_scope_unavailable" not in report["checks"][
        "repository_consistency"
    ].get("findings", [])


def test_plain_scan_without_usable_default_still_reports_scope_unavailable(
    tmp_path: Path,
):
    """origin/main が無い impact_map 付き repo は従来どおり tool_error。"""
    repo = make_repo(tmp_path)
    (repo / ".repo-preflight-consistency.json").write_text(
        json.dumps(
            {
                "schema": "repo-preflight.consistency/v1",
                "mode": "shadow",
                "impact_map": [{"change": ["README.md"], "requires_any": ["docs/**"]}],
            }
        ),
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add change-sensitive consistency config")

    report = MODULE.scan(repo)

    assert report["status"] == "tool_error"
    assert report["checks"]["repository_consistency"]["findings"] == [
        "change_sensitive_scope_unavailable"
    ]
    assert report["consistency_scope"] == {"mode": "same_as_scan"}


def test_scan_does_not_run_fsmonitor_command_from_target_config(tmp_path: Path):
    repo = make_repo(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))

    MODULE.scan(repo)

    assert not marker.exists()


def test_scan_cli_does_not_run_fsmonitor_command_from_target_config(tmp_path: Path):
    repo = make_repo(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))

    subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--intent", "publish"],
        capture_output=True,
        text=True,
    )

    assert not marker.exists()


def test_scan_does_not_rewrite_target_index(tmp_path: Path):
    repo = make_repo(tmp_path)
    tracked = repo / MODULE.REQUIRED[0]
    later = tracked.stat().st_mtime + 120
    os.utime(tracked, (later, later))
    index = repo / ".git" / "index"
    before = index.read_bytes()

    MODULE.scan(repo)

    assert index.read_bytes() == before


@pytest.mark.parametrize(
    "synthetic_root",
    (
        ("assert " + json.dumps(fixture_root("windows")) + " not in text").encode(),
        ("assert " + json.dumps(fixture_root("macos")) + " not in text").encode(),
        ("assert " + json.dumps(fixture_root("linux")) + " not in text").encode(),
    ),
)
def test_personal_path_patterns_ignore_quoted_synthetic_roots(synthetic_root: bytes):
    assert not MODULE.text_has(MODULE.PATH_PATTERNS, synthetic_root)
    assert not MODULE.personal_path_matches(synthetic_root)


@pytest.mark.parametrize(
    "personal_path",
    (
        (fixture_path("windows", "example-user", "project")).encode("latin1"),
        (fixture_path("macos", "example-user", "project")).encode("latin1"),
        (fixture_path("linux", "example-user", "project")).encode("latin1"),
    ),
)
def test_personal_path_patterns_detect_real_user_paths(personal_path: bytes):
    assert MODULE.text_has(MODULE.PATH_PATTERNS, personal_path)
    assert MODULE.personal_path_matches(personal_path)


@pytest.mark.parametrize(
    "root, separator",
    [
        (fixture_root("macos"), "/"),
        (fixture_root("linux"), "/"),
        (fixture_root("windows", forward=True), "/"),
        (fixture_root("windows"), "\\"),
    ],
)
@pytest.mark.parametrize(
    "quote, username",
    [
        ("'", "alice"),
        ('"', "alice"),
        ("'", "Alice Smith"),
        ('"', "Alice Smith"),
        ('"', "O'Brien"),
        ("'", 'Alice"Smith'),
    ],
)
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_quoted_username_personal_paths_remain_detected(
    root: str, separator: str, quote: str, username: str, encoding: str
):
    absolute = f"{root}{quote}{username}{quote}{separator}project"
    data = f"cd {absolute}".encode(encoding)
    assert MODULE.text_has(MODULE.PATH_PATTERNS, data)
    assert MODULE.personal_path_matches(data)
    assert MODULE.personal_path_matches(data) == MODULE.personal_path_matches(
        absolute.encode(encoding)
    )


@pytest.mark.parametrize(
    "root",
    [
        fixture_root("macos"),
        fixture_root("linux"),
        fixture_root("windows", forward=True),
    ],
)
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_empty_quoted_username_does_not_turn_root_into_personal_path(
    root: str, quote: str, encoding: str
):
    data = f"{root}{quote}{quote}/project".encode(encoding)
    assert not MODULE.text_has(MODULE.PATH_PATTERNS, data)
    assert not MODULE.personal_path_matches(data)


@pytest.mark.parametrize("surface", ["coauthor", "author", "message", "deleted_blob"])
def test_private_identity_provider_checks_real_git_objects(
    tmp_path, monkeypatch, surface
):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    email = "fixture-private-identity@example.invalid"
    if surface == "author":
        git(
            repo,
            "-c",
            "user.email=" + email,
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        )
    elif surface == "deleted_blob":
        (repo / "fixture.py").write_text(email, encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "fixture")
        git(repo, "rm", "fixture.py")
        git(repo, "commit", "-m", "remove fixture")
    else:
        message = (
            "fixture\n\nCo-authored-by: Fixture <" + email + ">"
            if surface == "coauthor"
            else email
        )
        git(repo, "commit", "--allow-empty", "-m", message)
    report = MODULE.scan(repo, require_private_identity=True)
    assert report["status"] == "blocked"
    check = report["checks"]["private_identity_scan"]
    assert check["status"] == "fail"
    assert check["finding_count"] > 0
    assert email not in json.dumps(report)


def test_expected_identity_includes_coauthor_trailer(tmp_path):
    repo = make_repo(tmp_path)
    git(
        repo,
        "commit",
        "--allow-empty",
        "-m",
        "fixture\n\nCo-authored-by: Fixture <other@example.invalid>",
    )
    report = MODULE.scan(
        repo, expected_identity="Test Author <test-author@example.invalid>"
    )
    assert report["checks"]["commit_identity"]["status"] == "fail"


@pytest.mark.parametrize("failure", ["unset", "empty", "missing", "inside_repo"])
def test_private_provider_missing_inputs_never_pass(tmp_path, monkeypatch, failure):
    helper, policy = configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    if failure == "unset":
        monkeypatch.delenv("PUBLIC_IDENTITY_DENYLIST")
    elif failure == "empty":
        policy.write_text("# empty\n")
    elif failure == "missing":
        policy.unlink()
    else:
        monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(repo / "README.md"))
    report = MODULE.scan(repo, require_private_identity=True)
    assert report["status"] == "tool_error"
    assert report["checks"]["private_identity_scan"]["status"] == "tool_error"


def test_private_identity_clean_brand_binds_provider_and_policy_hash(
    tmp_path, monkeypatch
):
    helper, policy = configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    report = MODULE.scan(repo, require_private_identity=True)
    check = report["checks"]["private_identity_scan"]
    assert check["status"] == "pass"
    assert check["provider_sha256"] == hashlib.sha256(helper.read_bytes()).hexdigest()
    assert check["policy_sha256"] == hashlib.sha256(policy.read_bytes()).hexdigest()
    assert check["objects_scanned"] > 0


def test_private_provider_runtime_error_is_unverified_not_pass(tmp_path, monkeypatch):
    helper, policy = configure_private_provider(tmp_path, monkeypatch)
    helper.write_text(
        "def parse_denylist_text(text): return ['fixture']\ndef scan_private_text(text, patterns): raise ValueError('private value must not escape')\n"
    )
    report = MODULE.scan(make_repo(tmp_path), require_private_identity=True)
    assert report["status"] == "tool_error"
    assert (
        report["checks"]["private_identity_scan"]["reason"]
        == "private_identity_execution_unverified"
    )
    assert "private value must not escape" not in json.dumps(report)


def test_private_provider_large_blob_is_unverified(tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    (repo / "large.txt").write_bytes(b"x" * 2_000_001)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "large fixture")
    report = MODULE.scan(repo, require_private_identity=True)
    assert report["status"] == "blocked"
    assert report["checks"]["private_identity_scan"]["status"] == "unknown"
    assert report["checks"]["private_identity_scan"]["oversize_objects_unscanned"] == 1


def test_private_identity_required_cli_unconfigured_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.delenv("PUBLIC_IDENTITY_DENYLIST", raising=False)
    repo = make_repo(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--require-private-identity",
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert (
        json.loads(completed.stdout)["checks"]["private_identity_scan"]["status"]
        == "tool_error"
    )


@pytest.mark.parametrize("encoding", ["utf-16", "url"])
def test_private_identity_reuses_supported_decodings(tmp_path, monkeypatch, encoding):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    value = "fixture-private-identity@example.invalid"
    content = (
        value.encode("utf-16")
        if encoding == "utf-16"
        else value.replace("@", "%40").encode()
    )
    (repo / "encoded.txt").write_bytes(content)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "encoded fixture")
    assert (
        MODULE.scan(repo, require_private_identity=True)["checks"][
            "private_identity_scan"
        ]["status"]
        == "fail"
    )


def test_private_scan_replacement_cannot_hide_raw_private_object(tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    (repo / "private.txt").write_text("fixture-private-identity@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "private fixture")
    bad = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / "private.txt").write_text("clean fixture")
    git(repo, "commit", "-am", "clean fixture")
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    clean = subprocess.run(
        ["git", "commit-tree", tree],
        input="clean independent\n",
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git(repo, "reset", "--hard", bad)
    git(repo, "replace", bad, clean)
    checker = MODULE.PrivateIdentityScan(repo, True)
    MODULE.history_hits(repo, private_identity=checker)
    assert checker.result()["status"] == "fail"
    assert checker.result()["finding_count"] > 0


def test_private_scan_rejects_grafts_context(tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / ".git/info/grafts").write_text(oid + "\n")
    checker = MODULE.PrivateIdentityScan(repo, True)
    with pytest.raises(RuntimeError):
        MODULE.history_hits(repo, private_identity=checker)


def test_private_scan_rejects_shallow_context_even_target_range(tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    _source, repo, _token = shallow_clone_with_deleted_secret(tmp_path)
    checker = MODULE.PrivateIdentityScan(repo, True)
    with pytest.raises(RuntimeError):
        MODULE.history_hits(repo, ("HEAD",), private_identity=checker)


def test_private_provider_reconstructed_path_is_not_suppressed(tmp_path, monkeypatch):
    helper, _policy = configure_private_provider(tmp_path, monkeypatch)
    helper.write_text(
        "def parse_denylist_text(text): return ['unrelated-fixture']\n"
        "def scan_private_text(text, patterns): return {'status':'fail','literal_match_count':0,'local_path_match':True,'reconstructed_path_match':True}\n"
    )
    checker = MODULE.PrivateIdentityScan(make_repo(tmp_path), True)
    checker.check(
        split_path_source(fixture_path("macos", "operator-fixture", "work"))
        .rstrip("\n")
        .encode(),
        literal_only=True,
    )
    assert checker.result()["status"] == "fail"


@pytest.mark.parametrize("mode", ["raw", "hidden", "normalized", "duplicate"])
def test_private_path_exception_does_not_approve_hidden_normalized_path(
    tmp_path, monkeypatch, mode
):
    import types

    repo = make_repo(tmp_path)
    raw_path = fixture_path("macos", "My", "work")
    content = "SAFE = " + repr(raw_path) + "\n"
    if mode == "hidden":
        content += split_path_source(fixture_path("macos", "operator-fixture", "work"))
    elif mode == "normalized":
        content = split_path_source(fixture_path("macos", "My", "work"))
    elif mode == "duplicate":
        content += split_path_source(fixture_path("macos", "My", "work"))
    # hash束縛するfixtureはWindowsでも同一bytesで保存する。
    (repo / "fixture.py").write_bytes(content.encode("utf-8"))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "path fixture")
    policy = tmp_path / "reviewed.json"
    expires = (
        MODULE.datetime.now(MODULE.timezone.utc) + MODULE.timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    policy.write_text(
        json.dumps(
            {
                "version": 3,
                "entries": [
                    {
                        "id": "SyntheticFixture",
                        "repo": "example/repo",
                        "path": "fixture.py",
                        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "rule": "macos_user_path",
                        "match_sha256": hashlib.sha256(
                            fixture_path("macos", "My").encode()
                        ).hexdigest(),
                        "occurrence_count": 1,
                        "review_reason": "reviewed public synthetic fixture",
                        "expires_at": expires,
                        "review_reference": "local-independent-review",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: types.SimpleNamespace(
            gh_api_get=lambda url: {
                "full_name": "example/repo",
                "private": False,
                "visibility": "public",
            }
        ),
    )
    private_policy = tmp_path / "private-policy.txt"
    private_policy.write_text("unrelated-fixture\n")
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(private_policy))
    report = MODULE.scan(
        repo, require_private_identity=True, reviewed_secret_exceptions=policy
    )
    check = report["checks"]["private_identity_scan"]
    assert check["status"] == ("fail" if mode in {"hidden", "duplicate"} else "pass")
    if mode in {"raw", "normalized"}:
        assert check["reviewed_path_exceptions"] == [
            {"id": "SyntheticFixture", "occurrence_count": 1}
        ]


def test_bundled_provider_works_without_private_workspace_or_installed_skill(
    tmp_path, monkeypatch
):
    repo = make_repo(tmp_path)
    policy = tmp_path / "private-policy.txt"
    policy.write_text("fixture-private-identity@example.invalid\n")
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("REPO_PREFLIGHT_ROOT", raising=False)
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--require-private-identity",
        ],
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    check = report["checks"]["private_identity_scan"]
    assert check["status"] == "pass"
    assert check["provider_kind"] == "bundled_canonical"
    assert (
        check["provider_sha256"]
        == hashlib.sha256(
            SCRIPT.with_name("identity_normalization.py").read_bytes()
        ).hexdigest()
    )


def test_bundled_self_scan_is_distinct_from_unreviewed_candidate_plugin(
    tmp_path, monkeypatch
):
    policy = tmp_path / "private-policy.txt"
    policy.write_text("unrelated-fixture\n")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    checker = MODULE.PrivateIdentityScan(SCRIPT.parents[1].resolve(), True)
    checker.check(b"clean brand")
    assert checker.result()["provider_kind"] == "bundled_canonical"
    assert checker.result()["status"] == "pass"
    repo = make_repo(tmp_path)
    marker = tmp_path / "must-not-execute"
    plugin = repo / "plugin.py"
    plugin.write_text(
        "__import__('pathlib').Path(" + repr(str(marker)) + ").write_text('bad')"
    )
    monkeypatch.setenv("PUBLIC_IDENTITY_GUARD_SCRIPT", str(plugin))
    assert MODULE.PrivateIdentityScan(repo, True).result()["status"] == "tool_error"
    assert not marker.exists()


def test_path_grammar_is_imported_from_canonical_engine():
    assert MODULE.PATH_PATTERNS is MODULE._PATH_GRAMMAR.PATH_PATTERNS
    assert MODULE.PATH_RULE_IDS is MODULE._PATH_GRAMMAR.PATH_RULE_IDS


@pytest.mark.parametrize("forward", [False, True])
def test_shared_grammar_repeated_windows_separators_remain_detected(forward):
    root = fixture_root("windows", forward=forward)
    separator = "/" if forward else "\\"
    value = root.replace(separator, separator * 2) + "fictional-user"
    assert MODULE.personal_path_matches(value.encode())
    assert MODULE._PATH_GRAMMAR.hit_local_absolute_path(value)


@pytest.mark.parametrize("surface", ["untracked", "tracked_dirty", "staged"])
def test_target_private_scan_checks_full_current_inventory(
    tmp_path, monkeypatch, surface
):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    tracked = repo / "existing-contact.txt"
    tracked.write_text("public brand\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "existing clean contact")
    base = set_remote_base(repo)
    (repo / "feature.txt").write_text("clean feature\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feature change")
    target = repo / "untracked-contact.txt" if surface == "untracked" else tracked
    target.write_text("fixture-private-identity@example.invalid\n")
    if surface == "staged":
        git(repo, "add", target.name)
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    assert report["checks"]["private_identity_scan"]["status"] == "fail"
    assert report["checks"]["private_identity_scan"]["finding_count"] >= 1
    assert (
        report["checks"]["private_identity_scan"]["worktree_scan_status"] == "complete"
    )
    assert report["checks"]["private_identity_scan"]["worktree_files_unscanned"] == 0


def test_target_private_scan_ignores_private_gitignored_storage(tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text("private-storage/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "exclude private storage")
    base = set_remote_base(repo)
    private = repo / "private-storage"
    private.mkdir()
    (private / "contact.txt").write_text("fixture-private-identity@example.invalid")
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    assert report["checks"]["private_identity_scan"]["status"] == "pass"


def test_target_private_scan_clean_inventory_keeps_empty_git_range(
    tmp_path, monkeypatch
):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    assert report["checks"]["private_identity_scan"]["status"] == "pass"
    assert report["checks"]["private_identity_scan"]["objects_scanned"] > 0


def test_target_private_scan_does_not_follow_untracked_symlink(tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    outside = tmp_path / "private-contact.txt"
    outside.write_text("fixture-private-identity@example.invalid")
    (repo / "contact-link").symlink_to(outside)
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    private = report["checks"]["private_identity_scan"]
    assert private["status"] == "pass"
    assert private["worktree_symlinks_excluded"] == 1
    assert private["worktree_files_unscanned"] == 0


def test_target_private_scan_keeps_git_history_base_boundary(tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    contact = repo / "contact.txt"
    contact.write_text("fixture-private-identity@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "old identity fixture")
    contact.write_text("public brand")
    git(repo, "commit", "-am", "clean before base")
    base = set_remote_base(repo)
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    private = report["checks"]["private_identity_scan"]
    assert private["status"] == "pass"
    assert private["git_object_scope"] == base + "..HEAD"


def test_target_private_scan_unreadable_full_inventory_is_tool_error(
    tmp_path, monkeypatch
):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    tracked = repo / "contact.txt"
    tracked.write_text("clean contact")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "track contact")
    base = set_remote_base(repo)
    original = Path.read_bytes

    def unreadable(path):
        if path == tracked:
            raise OSError("synthetic unreadable file")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    assert report["status"] == "tool_error"
    assert "worktree_file_unreadable:contact.txt" in report["issues"]


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
@pytest.mark.parametrize("location", ["outside", "ignored_inside"])
@pytest.mark.parametrize("range_mode", ["empty", "tracked_change", "untracked"])
def test_private_parent_directory_link_never_reads_original(
    tmp_path, monkeypatch, location, range_mode, link_kind
):
    import shutil

    if link_kind == "junction" and os.name != "nt":
        pytest.skip("Windows junctionの実検査はWindows環境で実施")
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    folder = repo / "folder"
    folder.mkdir()
    (folder / "document.txt").write_text("clean tracked content")
    (repo / ".gitignore").write_text("private-storage/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "track document")
    base = set_remote_base(repo)
    if range_mode == "tracked_change":
        (folder / "document.txt").write_text("new clean content")
        git(repo, "commit", "-am", "change document")
    outside = (
        tmp_path / "private-original"
        if location == "outside"
        else repo / "private-storage"
    )
    outside.mkdir()
    (outside / "document.txt").write_text("fixture-private-identity@example.invalid")
    if range_mode == "untracked":
        (outside / "untracked.txt").write_text(
            "fixture-private-identity@example.invalid"
        )
    shutil.rmtree(folder)
    if link_kind == "junction":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(folder), str(outside)],
            capture_output=True,
        )
        if created.returncode:
            pytest.skip("Windows junctionを作成できない実行環境")
    else:
        folder.symlink_to(outside, target_is_directory=True)
    attempts = []
    original = Path.read_bytes

    def trap(path):
        if path.resolve().is_relative_to(outside.resolve()):
            attempts.append(True)
            raise OSError("private original read trap")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", trap)
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    assert report["status"] == "tool_error"
    assert attempts == []


def test_private_scan_accepts_repository_root_alias(tmp_path, monkeypatch):
    configure_private_provider(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    alias = tmp_path / "repository-alias"
    alias.symlink_to(repo, target_is_directory=True)
    report = MODULE.scan(alias, base_ref=base, require_private_identity=True)
    assert report["checks"]["private_identity_scan"]["status"] == "pass"


def test_parent_guard_rejects_windows_reparse_attribute_without_following(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    import stat

    repo = tmp_path / "repo"
    repo.mkdir()
    folder = repo / "folder"
    folder.mkdir()
    original = Path.lstat

    def reparse(path):
        if path == folder:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(RuntimeError, match="git_worktree_inventory_failed"):
        MODULE._guard_worktree_parent_components(repo, folder / "document.txt")


def configure_filename_policy(tmp_path, monkeypatch):
    policy = tmp_path / "filename-private-policy.txt"
    policy.write_text(
        "ann\nfixture-private-identity@example.invalid\n", encoding="utf-8"
    )
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))


@pytest.mark.parametrize("surface", ["untracked", "staged"])
@pytest.mark.parametrize(
    "filename", ["ann.txt", "fixture-private-identity@example.invalid.txt"]
)
def test_private_current_filename_without_private_content_is_checked(
    tmp_path, monkeypatch, surface, filename
):
    configure_filename_policy(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    (repo / filename).write_text("public content\n")
    if surface == "staged":
        git(repo, "add", filename)
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    private = report["checks"]["private_identity_scan"]
    assert private["status"] == "fail" and private["finding_count"] > 0
    assert private["worktree_scan_status"] == "complete"
    assert private["worktree_files_unscanned"] == 0
    assert filename not in json.dumps(report)


@pytest.mark.parametrize(
    "directory", ["ann", "fixture-private-identity@example.invalid"]
)
def test_private_current_directory_component_is_checked(
    tmp_path, monkeypatch, directory
):
    configure_filename_policy(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    folder = repo / directory
    folder.mkdir()
    (folder / "public.txt").write_text("public content")
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    assert report["checks"]["private_identity_scan"]["status"] == "fail"
    assert directory + "/public.txt" not in json.dumps(report)


@pytest.mark.parametrize("filename", ["annual.txt", "Annette.txt", "ann.name.txt"])
def test_normal_filename_keeps_canonical_username_boundaries(
    tmp_path, monkeypatch, filename
):
    configure_filename_policy(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    (repo / filename).write_text("public content")
    assert (
        MODULE.scan(repo, base_ref=base, require_private_identity=True)["checks"][
            "private_identity_scan"
        ]["status"]
        == "pass"
    )


@pytest.mark.parametrize("name_kind", ["private_alias", "credential_like"])
def test_sensitive_workflow_name_is_not_exposed_in_report(
    tmp_path, monkeypatch, name_kind
):
    configure_filename_policy(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    name = "ann.yml" if name_kind == "private_alias" else "ghp_" + "A" * 24 + ".yml"
    (repo / ".github/workflows" / name).write_text("invalid workflow")
    report = MODULE.scan(repo, require_private_identity=True)
    assert name not in json.dumps(report)


def test_unreadable_private_filename_is_redacted_without_reading_content(
    tmp_path, monkeypatch
):
    configure_filename_policy(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    target = repo / "ann.txt"
    target.write_text("public content")
    original = Path.read_bytes

    def unreadable(path):
        if path == target:
            raise OSError("synthetic unreadable file")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    report = MODULE.scan(repo, base_ref=base, require_private_identity=True)
    assert report["status"] == "tool_error"
    assert "ann.txt" not in json.dumps(report)


def test_unverified_private_checker_does_not_expose_workflow_filename(
    tmp_path, monkeypatch
):
    helper, _ = configure_private_provider(tmp_path, monkeypatch)
    helper.write_text(
        helper.read_text()
        + '\ndef scan_private_text(text, patterns):\n    raise ValueError("synthetic provider error")\n'
    )
    repo = make_repo(tmp_path)
    name = "fixture-private-identity@example.invalid.yml"
    (repo / ".github/workflows" / name).write_text("invalid workflow")
    report = MODULE.scan(repo, require_private_identity=True)
    assert report["checks"]["private_identity_scan"]["status"] == "tool_error"
    assert name not in json.dumps(report)
