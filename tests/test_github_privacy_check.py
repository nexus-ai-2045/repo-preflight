"""The privileged adapter only scans data, and reports on the bound commit."""

import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import github_privacy_check as guard

HEAD = "a" * 40
REPO = "example/project"


def test_api_accepts_large_complete_pull_list_page():
    rows = [{"number": n, "body": "x" * 60000} for n in range(100)]
    raw = json.dumps(rows).encode()
    assert len(raw) > 2 * 1024 * 1024
    api = guard.API("synthetic")
    api.opener = SimpleNamespace(open=lambda *a, **kw: io.BytesIO(raw))
    assert api(f"/repos/{REPO}/pulls?state=open&per_page=100&page=1") == rows


@pytest.mark.parametrize(
    "path,limit",
    [
        (f"/repos/{REPO}/pulls/1", 2 * 1024 * 1024),
        (f"/repos/{REPO}/pulls?state=open&per_page=100&page=1", 32 * 1024 * 1024),
    ],
)
def test_api_retains_endpoint_specific_response_caps(path, limit):
    reads = []

    class Oversized(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return b" " * (limit + 1)

    api = guard.API("synthetic")
    api.opener = SimpleNamespace(open=lambda *a, **kw: Oversized())
    with pytest.raises(guard.GuardError, match="api_response_oversize"):
        api(path)
    assert reads == [limit + 1]


@pytest.fixture(autouse=True)
def distinct_test_app(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_CHECK_APP_ID", "42")


def event():
    return {
        "action": "completed",
        "repository": {"id": 1},
        "workflow_run": {
            "id": 12,
            "repository": {"id": 1},
            "status": "completed",
            "event": "pull_request",
            "path": ".github/workflows/public-privacy-preflight.yml",
            "head_sha": HEAD,
            "conclusion": "success",
            "head_branch": "feature",
        },
    }


class FakeAPI:
    def __init__(self, value):
        self.value = value
        self.calls = []
        self.pull = {
            "number": 3,
            "state": "open",
            "base": {"repo": {"id": 1}},
            "head": {"sha": HEAD},
        }

    def __call__(self, path, data=None, method="GET"):
        self.calls.append((path, data, method))
        if path.endswith("/actions/runs/12"):
            return copy.deepcopy(self.value["workflow_run"])
        if path.endswith("/pulls/3"):
            return copy.deepcopy(self.pull)
        if "/pulls?state=open&per_page=100&page=" in path:
            return [copy.deepcopy(self.pull)]
        if path.endswith("/check-runs"):
            return {"id": 21, "app": {"id": 42}}
        return {"id": 1, "default_branch": "main", "private": False}


@pytest.mark.parametrize(
    "event_ref,actual_ref",
    [("", "@main"), ("@main", ""), ("@refs/pull/3/merge", "@refs/pull/3/merge")],
)
def test_authoritative_run_accepts_ref_qualified_workflow_paths(event_ref, actual_ref):
    value = event()
    value["workflow_run"]["path"] = guard.WORKFLOW_PATH + event_ref
    actual = copy.deepcopy(value)
    actual["workflow_run"]["path"] = guard.WORKFLOW_PATH + actual_ref
    assert guard.validate_event(value, REPO, FakeAPI(actual)) == (HEAD, 3, True)


@pytest.mark.parametrize("suffix", ["@", "@main\n", "@main branch", ".other@main"])
def test_invalid_workflow_path_or_qualifier_is_rejected(suffix):
    value = event()
    value["workflow_run"]["path"] = guard.WORKFLOW_PATH + suffix
    with pytest.raises(guard.GuardError, match="event_binding_invalid"):
        guard.validate_event(value, REPO, FakeAPI(value))


def test_authoritative_workflow_must_still_match_expected_file():
    value = event()
    actual = copy.deepcopy(value)
    actual["workflow_run"]["path"] = ".github/workflows/other.yml@main"
    with pytest.raises(guard.GuardError, match="run_readback_mismatch"):
        guard.validate_event(value, REPO, FakeAPI(actual))


@pytest.mark.parametrize("private", [True, None, "false"])
def test_non_public_repository_is_rejected_before_check_creation(private):
    value = event()
    original = FakeAPI(value)

    def api(path, *args, **kwargs):
        if path == f"/repos/{REPO}":
            return {"id": 1, "default_branch": "main", "private": private}
        return original(path, *args, **kwargs)

    with pytest.raises(guard.GuardError, match="public_repository_required"):
        guard.execute(
            value, REPO, api, "x", "y", lambda *a: pytest.fail("must not scan")
        )
    assert not original.calls


@pytest.mark.parametrize(
    "status,code",
    [("pass", 0), ("blocked", 1), ("tool_error", 1), ("unknown", 1), ("skip", 1)],
)
def test_only_scanned_pass_can_succeed(status, code):
    value = event()
    api = FakeAPI(value)
    calls = []

    def scan(*args):
        calls.append(args)
        return {"status": status}

    assert guard.execute(value, REPO, api, "private", "policy", scan) == code
    assert calls == [(REPO, HEAD, 3, "private", "policy")]
    create = [c for c in api.calls if c[2] == "POST"][0][1]
    assert create["head_sha"] == HEAD
    assert api.calls[-1][1]["conclusion"] == ("success" if code == 0 else "failure")
    assert "private" not in str(api.calls[-1][1])


@pytest.mark.parametrize(
    "key,value",
    [
        ("head_sha", "bad"),
        ("status", "in_progress"),
        ("event", "workflow_dispatch"),
        ("path", ".github/workflows/attacker.yml"),
    ],
)
def test_event_rejected_before_scan(key, value):
    data = event()
    data["workflow_run"][key] = value
    api = FakeAPI(data)
    with pytest.raises(guard.GuardError):
        guard.validate_event(data, REPO, api)
    assert not any(c[2] == "POST" for c in api.calls)


def test_stale_head_does_not_pass_new_head():
    data = event()
    api = FakeAPI(data)
    api.pull["head"]["sha"] = "b" * 40
    with pytest.raises(guard.GuardError):
        guard.validate_event(data, REPO, api)
    assert not any(c[2] == "POST" for c in api.calls)


def test_authoritative_run_readback_required():
    data = event()
    api = FakeAPI(copy.deepcopy(data))
    api.value["workflow_run"]["head_sha"] = "b" * 40
    with pytest.raises(guard.GuardError):
        guard.validate_event(data, REPO, api)


def test_upstream_failure_cannot_become_success():
    data = event()
    data["workflow_run"]["conclusion"] = "failure"
    assert (
        guard.execute(
            data, REPO, FakeAPI(data), "x", "y", lambda *a: {"status": "pass"}
        )
        == 1
    )


def test_scanner_exception_has_no_values_in_api():
    data = event()
    api = FakeAPI(data)

    def broken(*args):
        raise RuntimeError("never-public-canary")

    assert guard.execute(data, REPO, api, "x", "y", broken) == 1
    assert "never-public-canary" not in str(api.calls)


def test_missing_policy_never_fetches(monkeypatch):
    monkeypatch.setattr(guard, "fetch_store", lambda *a: pytest.fail("must not fetch"))
    assert guard.scan_candidate(REPO, HEAD, 3, "", "x")["status"] == "tool_error"


def test_git_receives_no_token_or_policy(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "never-public-canary")
    monkeypatch.setenv("PREFLIGHT_PRIVATE_DENYLIST", "never-public-canary")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    env = guard.git_env()
    assert "never-public-canary" not in str(env)
    assert "GIT_CONFIG_COUNT" not in env
    assert env["GIT_CONFIG_GLOBAL"] == guard.os.devnull


def test_fetch_has_no_checkout_or_candidate_execution(tmp_path, monkeypatch):
    calls = []

    class Result:
        returncode = 0

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(guard.subprocess, "run", run)
    guard.fetch_store(tmp_path / "objects.git", REPO, HEAD, 3)
    assert len(calls) == 2
    assert all("checkout" not in call[0] for call in calls)
    assert "--bare" in calls[0][0]
    assert "--no-recurse-submodules" in calls[1][0]
    assert "+refs/pull/*:refs/pull/*" in calls[1][0]
    assert "+refs/pull/3/head:refs/heads/__candidate__" in calls[1][0]


def test_api_redirects_rejected():
    with pytest.raises(guard.GuardError):
        guard.NoRedirect().redirect_request(None, None, None, None, None, None)


@pytest.mark.parametrize("private_body, expected", [(False, 0), (True, 1)])
def test_adapter_with_real_bare_store(tmp_path, monkeypatch, private_body, expected):
    import shutil

    spec = importlib.util.spec_from_file_location(
        "_fixture_store", Path(__file__).with_name("test_git_data_privacy_gate.py")
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    store = fixture.Store(tmp_path)
    if private_body:
        store.head = store.commit(fixture.CANARY.encode())
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)

    def fetch(destination, repository, head, pull):
        shutil.copytree(store.repo, destination)

    monkeypatch.setattr(guard, "fetch_store", fetch)
    report = guard.scan_candidate(
        REPO, store.head, 3, store.denylist.read_text(), store.policy.read_text()
    )
    assert report["status"] == ("pass" if expected == 0 else "blocked")
    assert fixture.CANARY not in str(report)


def test_fork_pull_resolution_does_not_use_base_commit_association():
    data = event()
    api = FakeAPI(data)
    api.pull["head"]["repo"] = {"id": 99, "full_name": "fork-owner/project"}
    assert guard.validate_event(data, REPO, api) == (HEAD, 3, True)
    assert not any("/commits/" in path for path, _, _ in api.calls)


def test_open_pull_lookup_paginates_before_passing():
    data = event()
    original = FakeAPI(data)

    def api(path, payload=None, method="GET"):
        if path.endswith("&page=1"):
            return [
                {
                    "state": "open",
                    "number": 100 + i,
                    "base": {"repo": {"id": 1}},
                    "head": {"sha": "b" * 40},
                }
                for i in range(100)
            ]
        return original(path, payload, method)

    assert guard.validate_event(data, REPO, api) == (HEAD, 3, True)


def test_incomplete_open_pull_lookup_never_passes():
    data = event()
    original = FakeAPI(data)

    def api(path, payload=None, method="GET"):
        if "/pulls?" in path:
            return [copy.deepcopy(original.pull) for _ in range(100)]
        return original(path, payload, method)

    with pytest.raises(guard.GuardError, match="pull_lookup_incomplete"):
        guard.validate_event(data, REPO, api)


@pytest.mark.parametrize(
    "branch,valid", [("gh-readonly-queue/main/pr-3-a", True), ("feature", False)]
)
def test_merge_group_sha_is_bound_without_pr_association(branch, valid):
    data = event()
    data["workflow_run"].update(event="merge_group", head_branch=branch)
    api = FakeAPI(data)
    if valid:
        assert guard.validate_event(data, REPO, api) == (HEAD, None, True)
        assert (
            guard.execute(data, REPO, api, "x", "y", lambda *a: {"status": "pass"}) == 0
        )
        assert [c[1] for c in api.calls if c[2] == "POST"][0]["head_sha"] == HEAD
    else:
        with pytest.raises(guard.GuardError):
            guard.validate_event(data, REPO, api)
    assert not any("/pulls" in path for path, _, _ in api.calls)


@pytest.mark.parametrize("app_id", ["", "0", "15368", "not-an-id"])
def test_missing_or_actions_issuer_cannot_start_a_successful_check(monkeypatch, app_id):
    monkeypatch.setenv("PREFLIGHT_CHECK_APP_ID", app_id)
    data = event()
    api = FakeAPI(data)
    with pytest.raises(guard.GuardError, match="distinct_check_app_required"):
        guard.execute(data, REPO, api, "x", "y", lambda *a: {"status": "pass"})
    assert not any(c[2] == "POST" for c in api.calls)


def test_check_creation_must_confirm_actual_app_issuer():
    data = event()
    original = FakeAPI(data)

    def wrong_app(path, payload=None, method="GET"):
        result = original(path, payload, method)
        if method == "POST":
            return {"id": 21, "app": {"id": 15368}}
        return result

    with pytest.raises(guard.GuardError, match="check_issuer_mismatch"):
        guard.execute(
            data,
            REPO,
            wrong_app,
            "x",
            "y",
            lambda *a: pytest.fail("must not scan under wrong issuer"),
        )
    assert not any(
        c[2] == "PATCH" and c[1].get("conclusion") == "success" for c in original.calls
    )
