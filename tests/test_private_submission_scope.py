"""Live-verified private delivery scans committed objects, including transient history."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from synthetic_paths import synthetic_home_path

from test_readiness_scan import MODULE, git, make_repo, set_remote_base


@pytest.fixture
def submission(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    oid = MODULE.run(repo, "git", "rev-parse", base)[1]
    calls = []

    def api(endpoint):
        calls.append(endpoint)
        if "/git/ref/" in endpoint:
            return {"ref": "refs/heads/main", "object": {"sha": oid, "type": "commit"}}
        return {"full_name": "example/repo", "private": True, "visibility": "private"}

    monkeypatch.setattr(
        MODULE, "load_github_settings_module", lambda: SimpleNamespace(gh_api_get=api)
    )
    return repo, base, oid, calls


def commit(repo, name="change.txt", body="public change\n"):
    (repo / name).write_text(body, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", "public change")


def run_scan(repo, base, **kwargs):
    return MODULE.scan(
        repo,
        base_ref=base,
        private_submission=True,
        require_private_identity=True,
        **kwargs,
    )


@pytest.mark.parametrize("body", [b"name: missing jobs\n", b"\xff"])
def test_private_workflow_diagnostics_never_disclose_filename(submission, body):
    import json

    repo, base, _, _ = submission
    marker = "synthetic-private-identity-marker"
    workflow = repo / ".github" / "workflows" / (marker + ".yml")
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_bytes(body)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic workflow")
    report = run_scan(repo, base)
    assert report["checks"]["ci_configuration"]["invalid_files"] == ["<redacted-path>"]
    assert marker not in json.dumps(report)
    assert report["checks"]["private_identity_scan"]["status"] != "pass"


def test_live_proof_and_empty_range(submission):
    repo, base, oid, calls = submission
    report = run_scan(repo, base)
    assert report["status"] == "pass"
    scope = report["scan_scope"]
    assert scope["mode"] == "private_committed_submission"
    assert scope["base_oid"] == scope["head_oid"] == oid
    assert scope["private_submission"]["live_base_oid"] == oid
    assert scope["private_submission"]["visibility"] == "private"
    assert len(calls) == 2
    assert (
        report["checks"]["private_identity_scan"]["worktree_scan_status"]
        == "not_scanned"
    )


def test_private_reused_binary_diagnostics_use_oid_not_filename(
    submission, monkeypatch
):
    import json

    repo, _, _, _ = submission
    original = repo / "baseline.bin"
    original.write_bytes(
        b"\x00" + synthetic_home_path("linux", "old-fixture/work").encode()
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic baseline")
    base = rebase_submission(repo, monkeypatch)
    marker = "synthetic-private-identity-marker"
    git(repo, "mv", "baseline.bin", marker + ".bin")
    git(repo, "commit", "-m", "synthetic rename")
    report = run_scan(repo, base)
    assert report["checks"]["personal_path_scan"]["files"]
    assert marker not in json.dumps(report)
    assert report["checks"]["private_identity_scan"]["status"] != "pass"


@pytest.mark.parametrize(
    "live",
    [
        None,
        {},
        {"full_name": "example/repo", "private": False, "visibility": "public"},
        {"full_name": "other/repo", "private": True, "visibility": "private"},
        {"full_name": "example/repo", "private": 1, "visibility": "private"},
    ],
)
def test_non_private_or_unknown_fails_before_content(submission, monkeypatch, live):
    repo, base, _, _ = submission
    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(gh_api_get=lambda _: live),
    )
    monkeypatch.setattr(
        MODULE,
        "PrivateIdentityScan",
        lambda *a: pytest.fail("content scan before proof"),
    )
    assert run_scan(repo, base)["status"] == "tool_error"


@pytest.mark.parametrize("mode", ["api_error", "base_mismatch", "base_wrong_ref"])
def test_api_and_base_fail_closed(submission, monkeypatch, mode):
    repo, base, oid, _ = submission

    def api(endpoint):
        if mode == "api_error":
            raise RuntimeError("untrusted API text")
        if "/git/ref/" in endpoint:
            return {
                "ref": (
                    "refs/heads/other"
                    if mode == "base_wrong_ref"
                    else "refs/heads/main"
                ),
                "object": {
                    "type": "commit",
                    "sha": "0" * 40 if mode == "base_mismatch" else oid,
                },
            }
        return {"full_name": "example/repo", "private": True, "visibility": "private"}

    monkeypatch.setattr(
        MODULE, "load_github_settings_module", lambda: SimpleNamespace(gh_api_get=api)
    )
    report = run_scan(repo, base)
    assert report == {
        "status": "tool_error",
        "issues": ["private_submission_live_verification_failed"],
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other/repo.git",
        "https://noreply@github.com/example/repo.git",
        "https://github.com/example/repo.git?x=1",
        "ssh://noreply@github.com/example/repo.git",
    ],
)
def test_push_target_must_be_same_safe_repo(submission, url):
    repo, base, _, calls = submission
    git(repo, "remote", "set-url", "--push", "origin", url)
    assert run_scan(repo, base)["status"] == "tool_error"
    assert calls == []


def test_multiple_push_urls_rejected(submission):
    repo, base, _, calls = submission
    git(
        repo,
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        "https://github.com/example/repo.git",
    )
    git(
        repo,
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        "git@github.com:example/repo.git",
    )
    assert run_scan(repo, base)["status"] == "tool_error"
    assert calls == []


def test_unchanged_invalid_media_baseline_not_read(submission, monkeypatch):
    repo, _, _, _ = submission
    (repo / "old.jpg").write_bytes(b"\xff\xd8invalid baseline")
    git(repo, "add", "old.jpg")
    git(repo, "commit", "-m", "baseline")
    base = set_remote_base(repo)
    oid = MODULE.run(repo, "git", "rev-parse", base)[1]
    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(
            gh_api_get=lambda endpoint: (
                {"ref": "refs/heads/main", "object": {"type": "commit", "sha": oid}}
                if "/git/ref/" in endpoint
                else {
                    "full_name": "example/repo",
                    "private": True,
                    "visibility": "private",
                }
            )
        ),
    )
    commit(repo)
    original = Path.read_bytes

    def read(path):
        assert path.name != "old.jpg"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert run_scan(repo, base)["status"] == "pass"
    monkeypatch.setattr(Path, "read_bytes", original)
    assert (
        MODULE.scan(repo, require_private_identity=True)["checks"][
            "private_identity_scan"
        ]["status"]
        == "tool_error"
    )


def test_dirty_and_untracked_body_not_read_but_clean_gate_fails(
    submission, monkeypatch
):
    repo, base, _, _ = submission
    commit(repo)
    (repo / "change.txt").write_bytes(b"unsent worktree bytes")
    (repo / "untracked.jpg").write_bytes(b"\xff\xd8invalid unsent media")
    original = Path.read_bytes

    def read(path):
        assert path.name not in {"change.txt", "untracked.jpg"}
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    report = run_scan(repo, base)
    assert report["checks"]["private_identity_scan"]["status"] == "pass"
    assert report["checks"]["secret_scan"]["status"] == "pass"
    assert report["checks"]["personal_path_scan"]["status"] == "pass"
    assert report["checks"]["clean_worktree"]["status"] == "fail"
    assert report["status"] == "blocked"


@pytest.mark.parametrize("transient", [False, True])
def test_new_and_intermediate_sensitive_blob_blocked(submission, transient):
    repo, base, _, _ = submission
    commit(repo, body="synthetic-private-identity-marker\n")
    if transient:
        git(repo, "rm", "change.txt")
        git(repo, "commit", "-m", "remove temporary content")
    report = run_scan(repo, base)
    assert report["status"] == "blocked"
    assert report["checks"]["private_identity_scan"]["status"] == "fail"


def test_committed_secret_and_path_ignore_dirty_replacement(submission):
    repo, base, _, _ = submission
    commit(
        repo, body="sk-" + "A" * 48 + "\n" + "/" + "Users/fixture-user/private.txt\n"
    )
    (repo / "change.txt").write_text("clean looking unsent body\n")
    report = run_scan(repo, base)
    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert report["checks"]["personal_path_scan"]["status"] == "fail"


def test_renamed_sensitive_metadata_still_checked(submission):
    repo, base, _, _ = submission
    git(repo, "mv", "README.md", "synthetic-private-identity-marker.md")
    git(repo, "commit", "-m", "rename")
    assert run_scan(repo, base)["checks"]["private_identity_scan"]["status"] == "fail"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--intent", "publish"],
        ["--intent", "release"],
        ["--intent", "configure_settings"],
        ["--intent", "open_pr", "--base-ref", "origin/main", "--release"],
    ],
)
def test_cli_scope_restrictions(args):
    parser = MODULE.build_parser()
    parsed = parser.parse_args(["--repo", ".", "--private-submission", *args])
    with pytest.raises(SystemExit):
        MODULE.resolve_options(parsed, stdin_is_tty=False)


def test_direct_scan_requires_committed_scope(submission):
    repo, base, _, _ = submission
    assert MODULE.scan(repo, private_submission=True)["status"] == "tool_error"
    assert run_scan(repo, base, release=True)["status"] == "tool_error"


def test_cli_options_forward_private_submission(submission, monkeypatch):
    repo, base, _, _ = submission
    args = MODULE.build_parser().parse_args(
        [
            "--repo",
            str(repo),
            "--intent",
            "open_pr",
            "--base-ref",
            base,
            "--private-submission",
        ]
    )
    options = MODULE.resolve_options(args, stdin_is_tty=False)
    assert options.private_submission is True
    seen = {}
    original = MODULE.scan

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(MODULE, "scan", spy)
    dialogue = MODULE.build_intent_dialogue(options)
    assert seen["private_submission"] is True
    assert dialogue["scan"]["options"]["private_submission"] is True
    assert dialogue["scan"]["scan_scope"]["mode"] == "private_committed_submission"


def test_default_target_scope_does_not_call_live_api(submission, monkeypatch):
    repo, base, _, _ = submission
    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: pytest.fail("unexpected live verification"),
    )
    assert MODULE.scan(repo, base_ref=base)["scan_scope"]["mode"] == "target_diff"


def test_introduced_invalid_jpeg_remains_unverified(submission):
    repo, base, _, _ = submission
    (repo / "new.jpg").write_bytes(b"\xff\xd8invalid introduced media")
    git(repo, "add", "new.jpg")
    git(repo, "commit", "-m", "new media")
    report = run_scan(repo, base)
    assert report["status"] == "tool_error"
    assert report["checks"]["private_identity_scan"]["status"] == "tool_error"


def test_media_rename_of_baseline_blob_is_revalidated(submission, monkeypatch):
    repo, _, _, _ = submission
    commit(repo, "existing.txt", "plain baseline body\n")
    base = set_remote_base(repo)
    oid = MODULE.run(repo, "git", "rev-parse", base)[1]
    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(
            gh_api_get=lambda endpoint: (
                {"ref": "refs/heads/main", "object": {"type": "commit", "sha": oid}}
                if "/git/ref/" in endpoint
                else {
                    "full_name": "example/repo",
                    "private": True,
                    "visibility": "private",
                }
            )
        ),
    )
    git(repo, "mv", "existing.txt", "new.pdf")
    git(repo, "commit", "-m", "new declared media")
    report = run_scan(repo, base)
    assert report["status"] == "tool_error"
    assert (
        report["checks"]["private_identity_scan"]["reason"]
        == "private_identity_media_format_mismatch"
    )


def test_unchanged_subtree_names_are_not_rechecked(submission, monkeypatch):
    repo, _, _, _ = submission
    nested = repo / "old-subtree"
    nested.mkdir()
    commit(repo, "old-subtree/synthetic-private-identity-marker.txt", "old safe body\n")
    base = set_remote_base(repo)
    oid = MODULE.run(repo, "git", "rev-parse", base)[1]
    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(
            gh_api_get=lambda endpoint: (
                {"ref": "refs/heads/main", "object": {"type": "commit", "sha": oid}}
                if "/git/ref/" in endpoint
                else {
                    "full_name": "example/repo",
                    "private": True,
                    "visibility": "private",
                }
            )
        ),
    )
    commit(repo)
    assert run_scan(repo, base)["status"] == "pass"


def test_local_base_movement_during_scan_invalidates_proof(submission, monkeypatch):
    repo, base, _, _ = submission
    commit(repo)
    original = MODULE.verify_private_submission

    def verify(*args):
        proof = original(*args)
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        return proof

    monkeypatch.setattr(MODULE, "verify_private_submission", verify)
    assert run_scan(repo, base) == {
        "status": "tool_error",
        "issues": ["private_submission_local_refs_changed"],
    }


def rebase_submission(repo, monkeypatch):
    base = set_remote_base(repo)
    oid = MODULE.run(repo, "git", "rev-parse", base)[1]

    def api(endpoint):
        if "/git/ref/" in endpoint:
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": oid}}
        return {"full_name": "example/repo", "private": True, "visibility": "private"}

    monkeypatch.setattr(
        MODULE, "load_github_settings_module", lambda: SimpleNamespace(gh_api_get=api)
    )
    return base


def test_modified_text_does_not_reclassify_unchanged_fixtures(submission, monkeypatch):
    repo, _, _, _ = submission
    body = (
        'OLD = "synthetic-private-identity-marker"\nPATH = '
        + repr(synthetic_home_path("macos", "old-fixture/work"))
        + '\nKEY = "sk-'
        + "A" * 48
        + '"\n'
    )
    commit(repo, body=body)
    base = rebase_submission(repo, monkeypatch)
    commit(repo, body=body + 'SAFE = "public"\n')
    report = run_scan(repo, base)
    assert report["status"] == "pass"
    for gate in ("private_identity_scan", "personal_path_scan", "secret_scan"):
        assert report["checks"][gate]["status"] == "pass"
    assert MODULE.scan(repo, require_private_identity=True)["status"] == "blocked"


@pytest.mark.parametrize(
    "addition",
    [
        'NEW = "synthetic-private-identity-marker"\n',
        "PATH2 = " + repr(synthetic_home_path("macos", "new-fixture/work")) + "\n",
    ],
)
def test_modified_text_new_sensitive_value_blocks(submission, monkeypatch, addition):
    repo, _, _, _ = submission
    body = "OLD = " + repr(synthetic_home_path("macos", "old-fixture/work")) + "\n"
    commit(repo, body=body)
    base = rebase_submission(repo, monkeypatch)
    commit(repo, body=body + addition)
    assert run_scan(repo, base)["status"] == "blocked"


def test_modified_text_context_reconstruction_blocks(submission, monkeypatch):
    repo, _, _, _ = submission
    before = 'PATH = (\n    "/" + "Users/"\n)\n'
    commit(repo, body=before)
    base = rebase_submission(repo, monkeypatch)
    commit(repo, body=before.replace("\n)", '\n    + "operator-fixture/work"\n)'))
    report = run_scan(repo, base)
    assert report["checks"]["private_identity_scan"]["status"] == "fail"
    assert report["checks"]["personal_path_scan"]["status"] == "fail"


@pytest.mark.parametrize("operation", ["rename", "copy"])
def test_new_path_without_same_parent_baseline_is_full(
    submission, monkeypatch, operation
):
    repo, _, _, _ = submission
    body = 'OLD = "synthetic-private-identity-marker"\n'
    commit(repo, body=body)
    base = rebase_submission(repo, monkeypatch)
    if operation == "rename":
        git(repo, "mv", "change.txt", "new-name.txt")
        git(repo, "commit", "-m", "renamed text")
    else:
        commit(repo, "new-name.txt", body)
    assert run_scan(repo, base)["checks"]["private_identity_scan"]["status"] == "fail"


def test_modified_text_intermediate_add_then_remove_blocks(submission, monkeypatch):
    repo, _, _, _ = submission
    commit(repo)
    base = rebase_submission(repo, monkeypatch)
    commit(repo, body='public change\nNEW = "synthetic-private-identity-marker"\n')
    commit(repo)
    assert run_scan(repo, base)["checks"]["private_identity_scan"]["status"] == "fail"


def test_unchanged_sibling_name_not_reclassified(submission, monkeypatch):
    repo, _, _, _ = submission
    commit(repo, "synthetic-private-identity-marker.txt", "safe body\n")
    base = rebase_submission(repo, monkeypatch)
    commit(repo)
    assert run_scan(repo, base)["status"] == "pass"


def test_opaque_extension_retains_full_body_inspection(submission, monkeypatch):
    repo, _, _, _ = submission
    body = "synthetic-private-identity-marker\n"
    commit(repo, "fixture.zip", body)
    base = rebase_submission(repo, monkeypatch)
    commit(repo, "fixture.zip", body + "safe addition\n")
    assert run_scan(repo, base)["checks"]["private_identity_scan"]["status"] == "fail"
