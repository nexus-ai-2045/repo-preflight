import json
import subprocess

import pytest

from test_readiness_scan import MODULE, git, make_repo, set_remote_base


def live_state(**changes):
    return {
        "nameWithOwner": "example/repo",
        "id": "sample-id",
        "visibility": "PRIVATE",
        "isEmpty": True,
        "heads": {"totalCount": 0},
        "tags": {"totalCount": 0},
        **changes,
    }


def github_queries(monkeypatch, *states):
    real_run = MODULE.run_subprocess
    calls = []

    def run(argv, **kwargs):
        if argv[0] != "gh":
            return real_run(argv, **kwargs)
        assert argv[:5] == ["gh", "api", "--hostname", "github.com", "graphql"]
        assert "owner=example" in argv and "name=repo" in argv
        assert any(arg.startswith("query=query(") for arg in argv)
        calls.append(argv)
        state = states[min(len(calls), len(states)) - 1]
        if isinstance(state, Exception):
            raise state
        response = {"data": {"repository": state}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(response).encode())

    monkeypatch.setattr(MODULE, "run_subprocess", run)
    return calls


def dialogue(repo, *args):
    parsed = MODULE.build_parser().parse_args(
        ["--repo", str(repo), "--intent", "push", *args]
    )
    return MODULE.build_intent_dialogue(
        MODULE.resolve_options(parsed, stdin_is_tty=False)
    )


def test_empty_private_save_from_cli_rechecks_proof_and_preserves_history(
    tmp_path, monkeypatch
):
    repo = make_repo(tmp_path)
    set_remote_base(repo)  # Stale tracking refs cannot supply a comparison baseline.
    calls = github_queries(monkeypatch, live_state())
    report = dialogue(repo, "--initial-private-save", "example/repo")
    scan = report["scan"]
    assert len(calls) == 2
    assert scan["status"] == "pass"
    assert scan["options"]["mode"] == "initial_private_save"
    assert "example/repo" not in json.dumps(report)
    scope = scan["scan_scope"]
    assert scope["mode"] == "initial_private_save"
    assert scope["initial_private_save"]["empty"] is True
    assert (
        scan["checks"]["private_identity_scan"]["git_object_scope"]
        == "all_reachable_refs"
    )
    assert (
        report["confirmations"][0]["proposed"]["operation_binding"]["head_oid"]
        == scan["head"]
    )


@pytest.mark.parametrize(
    "state",
    [
        live_state(visibility="PUBLIC"),
        live_state(isEmpty=False),
        live_state(tags={"totalCount": 1}),
        live_state(heads={"totalCount": 1}),
        live_state(nameWithOwner="different/repo"),
        live_state(isEmpty=1),
        live_state(tags=None),
        live_state(heads={"totalCount": False}),
        subprocess.TimeoutExpired("gh", 20),
    ],
)
def test_empty_private_save_requires_complete_remote_evidence(
    tmp_path, monkeypatch, state
):
    repo = make_repo(tmp_path)
    github_queries(monkeypatch, state)
    with pytest.raises(RuntimeError):
        MODULE.verify_initial_private_save(repo, "example/repo")


@pytest.mark.parametrize(
    "key,value",
    [
        ("remote.origin.pushurl", "https://github.com/example/different.git"),
        ("remote.origin.url", "https://github.com/example/another.git"),
    ],
)
def test_effective_origin_verified_before_network(tmp_path, monkeypatch, key, value):
    repo = make_repo(tmp_path)
    git(repo, "config", "--add", key, value)
    calls = github_queries(monkeypatch, live_state())
    with pytest.raises(RuntimeError, match="push_destination_origin_unverified"):
        MODULE.verify_initial_private_save(repo, "example/repo")
    assert calls == []


def test_initial_save_rejects_recreated_repository(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    github_queries(monkeypatch, live_state(), live_state(id="replacement"))
    result = MODULE.scan(repo, initial_private_save="example/repo")
    assert result["issues"] == ["initial_private_save_destination_changed"]


@pytest.mark.parametrize(
    "flags",
    [
        ["--intent", "publish"],
        ["--intent", "merge"],
        [],
        ["--intent", "push", "--release"],
        ["--intent", "push", "--base-ref", "origin/main"],
        ["--intent", "push", "--consistency-base-ref", "origin/main"],
        ["--intent", "push", "--identity-base-ref", "origin/main"],
        ["--intent", "push", "--private-submission"],
    ],
)
def test_initial_save_cli_rejects_other_scopes(flags):
    args = MODULE.build_parser().parse_args(
        ["--repo", ".", "--initial-private-save", "example/repo", *flags]
    )
    with pytest.raises(SystemExit):
        MODULE.resolve_options(args, stdin_is_tty=False)


@pytest.mark.parametrize(
    "state,verified",
    [
        (live_state(), False),
        (live_state(isEmpty=False, heads={"totalCount": 1}), True),
        (subprocess.TimeoutExpired("gh", 20), False),
    ],
)
@pytest.mark.parametrize("diff", [False, True])
def test_existing_push_checks_live_refs_for_selected_history(
    tmp_path, monkeypatch, state, verified, diff
):
    repo = make_repo(tmp_path)
    set_remote_base(repo)
    if diff and verified:
        oid = MODULE.run(repo, "git", "rev-parse", "origin/main")[1]
        state = {**state, "baseline": {"target": {"oid": oid}}}
    calls = github_queries(monkeypatch, state)
    result = dialogue(repo, *(["--base-ref", "origin/main"] if diff else []))
    assert ("base=refs/heads/main" in calls[0]) == diff
    assert result["scan"]["scan_scope"]["mode"] == (
        "target_diff" if diff else "repository"
    )
    assert (
        bool(result["scan"]["scan_scope"].get("existing_push_destination")) == verified
    )
    assert any(p["id"] == "verify_push_scope" for p in result["proposals"]) != verified
    if not verified:
        assert result["status"] == "blocked"


@pytest.mark.parametrize("remote_base", [None, {"target": {"oid": "f" * 40}}])
def test_existing_push_rejects_missing_or_changed_remote_comparison_base(
    tmp_path, monkeypatch, remote_base
):
    repo = make_repo(tmp_path)
    set_remote_base(repo)
    github_queries(
        monkeypatch,
        live_state(isEmpty=False, heads={"totalCount": 1}, baseline=remote_base),
    )
    result = dialogue(repo, "--base-ref", "origin/main")
    assert result["status"] == "blocked"
    assert "existing_push_destination" not in result["scan"]["scan_scope"]


def test_github_destination_accepts_owner_and_repository_case_differences(
    tmp_path, monkeypatch
):
    repo = make_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", "https://github.com/Example/REPO.git")
    github_queries(monkeypatch, live_state(nameWithOwner="EXAMPLE/Repo"))
    assert MODULE.verify_initial_private_save(repo, "example/Repo")["empty"]
    github_queries(
        monkeypatch,
        live_state(
            nameWithOwner="EXAMPLE/Repo", isEmpty=False, heads={"totalCount": 1}
        ),
    )
    assert dialogue(repo)["scan"]["scan_scope"]["existing_push_destination"]


def test_initial_private_save_blocks_secrets_reachable_only_from_other_branch(
    tmp_path, monkeypatch
):
    repo = make_repo(tmp_path)
    original_branch = MODULE.run(repo, "git", "symbolic-ref", "--short", "HEAD")[1]
    git(repo, "checkout", "-b", "side")
    (repo / "config.txt").write_text("sk-" + "A" * 48 + "\n", encoding="utf-8")
    git(repo, "add", "config.txt")
    git(repo, "commit", "-m", "side change")
    git(repo, "checkout", original_branch)
    github_queries(monkeypatch, live_state())
    result = MODULE.scan(repo, initial_private_save="example/repo")
    assert result["status"] == "blocked"
    assert result["checks"]["secret_scan"]["status"] == "fail"


def test_deleted_relative_path_is_scanned_across_tree_components(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    name = "fixture/submission"
    policy = tmp_path / "local-policy"
    policy.write_text(name + "\n", encoding="utf-8")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    path = repo / name
    path.parent.mkdir()
    path.with_suffix(".txt").write_text("content", encoding="utf-8")
    git(repo, "add", "fixture")
    git(repo, "commit", "-m", "add file")
    git(repo, "rm", "-r", "fixture")
    git(repo, "commit", "-m", "remove file")
    github_queries(monkeypatch, live_state())
    result = MODULE.scan(repo, initial_private_save="example/repo")
    assert result["checks"]["private_identity_scan"]["status"] == "fail"
    assert name not in json.dumps(result)


def test_initial_save_binds_private_checked_current_branch(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-b", "synthetic-private-identity-marker")
    github_queries(monkeypatch, live_state())
    result = MODULE.scan(repo, initial_private_save="example/repo")
    assert result["checks"]["private_identity_scan"]["status"] == "fail"
    assert "synthetic-private-identity-marker" not in json.dumps(result)
    git(repo, "checkout", "--detach")
    assert MODULE.scan(repo, initial_private_save="example/repo")["issues"] == [
        "initial_private_save_requires_current_branch"
    ]


def test_first_save_rejects_private_author_earlier_in_history(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    marker = "synthetic-private-identity-marker"
    git(repo, "-c", f"user.name={marker}", "commit", "--allow-empty", "-m", "earlier")
    git(repo, "commit", "--allow-empty", "-m", "current")
    github_queries(monkeypatch, live_state())
    result = dialogue(repo, "--initial-private-save", "example/repo")
    assert result["status"] == "blocked"
    assert result["scan"]["checks"]["identity_gate"]["status"] == "fail"
    assert marker not in json.dumps(result)


def test_first_save_requires_private_identity_configuration(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.delenv("PUBLIC_IDENTITY_DENYLIST")
    github_queries(monkeypatch, live_state())
    result = dialogue(repo, "--initial-private-save", "example/repo")
    assert result["status"] == "blocked"
    assert result["scan"]["checks"]["private_identity_scan"]["status"] == "tool_error"
