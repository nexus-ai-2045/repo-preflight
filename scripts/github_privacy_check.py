#!/usr/bin/env python3
"""Trusted workflow_run adapter. Candidate Git is data, never checked out or run."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from git_data_privacy_gate import scan_store

CHECK_NAME = "private-identity-guard"
WORKFLOW_PATH = ".github/workflows/public-privacy-preflight.yml"
SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
OID = re.compile(r"[0-9a-f]{40}")
PULL_LIST_PATH = re.compile(
    r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pulls\?state=open&per_page=100&page=[1-9][0-9]?"
)


class GuardError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise GuardError("api_redirect_rejected")


class API:
    def __init__(self, token: str):
        if not token:
            raise GuardError("token_missing")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def __call__(self, path: str, data=None, method="GET"):
        if not path.startswith("/repos/") or any(
            value in path for value in ("..", "#", "\\")
        ):
            raise GuardError("api_path_invalid")
        pull_list = bool(PULL_LIST_PATH.fullmatch(path))
        if "?" in path and not pull_list:
            raise GuardError("api_query_invalid")
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=None if data is None else json.dumps(data).encode(),
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "repo-preflight-private-identity-guard",
            },
        )
        # A list page includes up to 100 complete PR descriptions. Keep other
        # endpoints small, while allowing a bounded large page for this lookup.
        response_limit = (32 if pull_list else 2) * 1024 * 1024
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(response_limit + 1)
            if len(raw) > response_limit:
                raise GuardError("api_response_oversize")
            return json.loads(raw)
        except (urllib.error.URLError, ValueError, OSError):
            raise GuardError("api_request_failed") from None


def expected_workflow_path(value):
    """GitHub may return the file path alone or with a ref qualifier."""
    if not isinstance(value, str):
        return False
    path, separator, ref = value.partition("@")
    return path == WORKFLOW_PATH and (
        not separator
        or bool(ref)
        and all(char.isprintable() and not char.isspace() for char in ref)
    )


def validate_event(event: dict, repository: str, api):
    """Bind the check to the completed upstream run and current PR head."""
    if not SLUG.fullmatch(repository):
        raise GuardError("repository_invalid")
    prefix = "/repos/" + repository
    repo = api(prefix)
    # This rollout scans public repositories without passing credentials to Git.
    # Refuse private or unknown visibility before creating a check or fetching.
    if repo.get("private") is not False:
        raise GuardError("public_repository_required")
    run = event.get("workflow_run", {})
    if (
        event.get("action") != "completed"
        or event.get("repository", {}).get("id") != repo.get("id")
        or run.get("repository", {}).get("id") != repo.get("id")
        or run.get("status") != "completed"
        or run.get("event") not in {"pull_request", "push", "merge_group"}
        or not expected_workflow_path(run.get("path"))
        or not OID.fullmatch(str(run.get("head_sha", "")))
    ):
        raise GuardError("event_binding_invalid")
    # Re-read the authoritative run. Never trust an artifact or workflow output.
    run_id = run.get("id")
    if type(run_id) is not int or run_id <= 0:
        raise GuardError("run_id_invalid")
    actual = api(prefix + "/actions/runs/" + str(run_id))
    for key in ("head_sha", "head_branch", "event", "status", "conclusion"):
        if actual.get(key) != run.get(key):
            raise GuardError("run_readback_mismatch")
    if not expected_workflow_path(actual.get("path")):
        raise GuardError("run_readback_mismatch")
    head = run["head_sha"]
    if run["event"] == "push":
        if run.get("head_branch") != repo.get("default_branch"):
            raise GuardError("push_branch_invalid")
        return head, None, run.get("conclusion") == "success"
    if run["event"] == "merge_group":
        # Synthetic queue head is a separate commit, bound by the authoritative run.
        branch = run.get("head_branch", "")
        if not branch.startswith("gh-readonly-queue/" + repo["default_branch"] + "/"):
            raise GuardError("merge_group_branch_invalid")
        return head, None, run.get("conclusion") == "success"
    # A fork commit need not exist in the base repo's commit-association endpoint.
    # Enumerate the base repo's open PRs instead; incomplete pagination cannot pass.
    pulls = []
    for page in range(1, 21):
        batch = api(prefix + "/pulls?state=open&per_page=100&page=" + str(page))
        if not isinstance(batch, list) or len(batch) > 100:
            raise GuardError("pull_lookup_invalid")
        pulls.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise GuardError("pull_lookup_incomplete")
    matches = [
        pr
        for pr in pulls
        if pr.get("state") == "open"
        and pr.get("base", {}).get("repo", {}).get("id") == repo["id"]
        and pr.get("head", {}).get("sha") == head
    ]
    if len(matches) != 1:
        raise GuardError("current_pull_head_not_unique")
    number = matches[0].get("number")
    if type(number) is not int or number <= 0:
        raise GuardError("pull_number_invalid")
    current = api(prefix + "/pulls/" + str(number))
    if (
        current.get("state") != "open"
        or current.get("head", {}).get("sha") != head
        or current.get("base", {}).get("repo", {}).get("id") != repo["id"]
    ):
        raise GuardError("pull_head_changed")
    return head, number, run.get("conclusion") == "success"


def git_env():
    # No credential, policy, token, candidate configuration or Git override to Git.
    allowed = ("PATH", "SystemRoot", "TMPDIR", "TEMP", "TMP")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        LC_ALL="C",
    )
    return result


def fetch_store(path: Path, repository: str, head: str, pull: int | None):
    if not SLUG.fullmatch(repository) or not OID.fullmatch(head):
        raise GuardError("fetch_input_invalid")
    commands = [
        ["git", "init", "--bare", "--template=", str(path)],
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(path),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.file.allow=never",
            "fetch",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--force",
            "https://github.com/" + repository + ".git",
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
            "+refs/pull/*:refs/pull/*",
            *([f"+refs/pull/{pull}/head:refs/heads/__candidate__"] if pull else [head]),
        ],
    ]
    try:
        for command in commands:
            result = subprocess.run(
                command,
                env=git_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False,
            )
            if result.returncode:
                raise GuardError("git_fetch_failed")
    except (OSError, subprocess.TimeoutExpired):
        raise GuardError("git_fetch_failed") from None


def scan_candidate(
    repository: str, head: str, pull: int | None, denylist: str, policy: str
):
    # Unavailable policy is a failure, never a skipped successful check.
    if not denylist.strip() or not policy.strip():
        return {"status": "tool_error", "reasons": ["private_policy_missing"]}
    with tempfile.TemporaryDirectory(prefix="preflight-private-") as tmp:
        root = Path(tmp)
        deny = root / "denylist.txt"
        identities = root / "identities.json"
        for path, value in ((deny, denylist), (identities, policy)):
            with path.open("x", encoding="utf-8") as stream:
                os.chmod(path, 0o600)
                stream.write(value)
        store = root / "objects.git"
        fetch_store(store, repository, head, pull)
        return scan_store(store, deny, identities, head)


def execute(
    event: dict,
    repository: str,
    api,
    denylist: str,
    policy: str,
    scanner=scan_candidate,
):
    raw_app_id = os.environ.get("PREFLIGHT_CHECK_APP_ID", "")
    if not re.fullmatch(r"[1-9][0-9]*", raw_app_id) or int(raw_app_id) == 15368:
        raise GuardError("distinct_check_app_required")
    expected_app_id = int(raw_app_id)
    head, pull, upstream_ok = validate_event(event, repository, api)
    prefix = "/repos/" + repository
    check = api(
        prefix + "/check-runs",
        {
            "name": CHECK_NAME,
            "head_sha": head,
            "status": "in_progress",
            "external_id": "repo-preflight:" + str(event["workflow_run"]["id"]),
        },
        "POST",
    )
    check_id = check.get("id")
    if type(check_id) is not int or check_id <= 0:
        raise GuardError("check_create_failed")
    if check.get("app", {}).get("id") != expected_app_id:
        raise GuardError("check_issuer_mismatch")
    try:
        report = scanner(repository, head, pull, denylist, policy)
        passed = upstream_ok and report.get("status") == "pass"
    except Exception:
        # Never print candidate data, private policy, stderr or raw exceptions.
        passed = False
    api(
        prefix + "/check-runs/" + str(check_id),
        {
            "status": "completed",
            "conclusion": "success" if passed else "failure",
            "output": {
                "title": "登録済み個人情報の検査",
                "summary": (
                    "検査成功"
                    if passed
                    else "検査失敗または検査不能。非公開の管理経路で確認してください。"
                ),
            },
        },
        "PATCH",
    )
    return 0 if passed else 1


def main():
    try:
        if os.environ.get("GITHUB_EVENT_NAME") != "workflow_run":
            raise GuardError("workflow_run_required")
        event = json.loads(
            Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")
        )
        code = execute(
            event,
            os.environ.get("GITHUB_REPOSITORY", ""),
            API(os.environ.get("PREFLIGHT_CHECK_TOKEN", "")),
            os.environ.get("PREFLIGHT_PRIVATE_DENYLIST", ""),
            os.environ.get("PREFLIGHT_PRIVATE_IDENTITY_POLICY", ""),
        )
    except Exception:
        print("private_identity_guard: tool_error")
        return 1
    print("private_identity_guard: " + ("pass" if code == 0 else "blocked"))
    return code


if __name__ == "__main__":
    sys.exit(main())
