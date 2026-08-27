from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "github_pr_review_snapshot", ROOT / "scripts" / "github_pr_review_snapshot.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


HEAD = "a" * 40


def snapshot(*, reviews=None, comments=None, threads=None, checks=None, head=HEAD):
    return MODULE.evaluate_snapshot(
        {
            "headRefOid": head,
            "reviews": reviews or [],
            "comments": comments or [],
            "statusCheckRollup": checks or [],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        },
        threads or [],
        expected_head=HEAD,
        required_reviewer="chatgpt-codex-connector",
    )


def test_fails_closed_when_head_changes() -> None:
    report = snapshot(head="b" * 40)
    assert report["status"] == "tool_error"
    assert report["reasons"] == ["head_mismatch"]


def test_blocks_unresolved_inline_thread_even_when_ci_and_review_pass() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "COMMENTED",
            }
        ],
        threads=[{"id": "T1", "isResolved": False, "isOutdated": False}],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "blocked"
    assert report["unresolved_thread_ids"] == ["T1"]


def test_review_for_old_head_does_not_satisfy_current_head() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "b" * 40},
            }
        ],
        comments=[{"body": "@codex review", "reactionGroups": [{"content": "EYES"}]}],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "pending"
    assert "required_review_pending" in report["reasons"]


def test_pass_requires_exact_review_clean_threads_and_green_ci() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "COMMENTED",
            }
        ],
        threads=[{"id": "T1", "isResolved": True, "isOutdated": False}],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "pass"
    assert report["reviewed_head"] == HEAD


def test_pending_ci_is_not_review_clean_completion() -> None:
    report = snapshot(
        reviews=[
            {"author": {"login": "chatgpt-codex-connector"}, "commit": {"oid": HEAD}}
        ],
        checks=[{"status": "IN_PROGRESS", "conclusion": ""}],
    )
    assert report["status"] == "pending"
    assert "ci_pending" in report["reasons"]


def test_outdated_unresolved_thread_still_blocks() -> None:
    report = snapshot(
        reviews=[
            {"author": {"login": "chatgpt-codex-connector"}, "commit": {"oid": HEAD}}
        ],
        threads=[{"id": "T1", "isResolved": False, "isOutdated": True}],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "blocked"
    assert report["unresolved_thread_ids"] == ["T1"]


def test_changes_requested_blocks_without_inline_thread() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-08-27T00:01:00Z",
            }
        ],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "blocked"
    assert "changes_requested" in report["reasons"]


def test_new_same_head_request_requires_newer_review() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "COMMENTED",
                "submittedAt": "2026-08-27T00:01:00Z",
            }
        ],
        comments=[{"body": "@codex review", "createdAt": "2026-08-27T00:02:00Z"}],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "pending"
    assert "required_review_pending" in report["reasons"]


def test_legacy_status_context_states_are_classified() -> None:
    success = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "COMMENTED",
            }
        ],
        checks=[{"state": "SUCCESS"}],
    )
    failure = snapshot(
        reviews=[
            {"author": {"login": "chatgpt-codex-connector"}, "commit": {"oid": HEAD}}
        ],
        checks=[{"state": "FAILURE"}],
    )
    assert success["status"] == "pass"
    assert failure["status"] == "blocked"
    assert "ci_failed" in failure["reasons"]


def test_collect_paginates_threads_and_rejects_head_drift(monkeypatch) -> None:
    calls = []

    def fake_gh(args):
        calls.append(args)
        pr_call_count = len([call for call in calls if call[:2] == ["pr", "view"]])
        if args[:2] == ["pr", "view"] and pr_call_count == 1:
            return {
                "headRefOid": HEAD,
                "reviews": [],
                "comments": [],
                "statusCheckRollup": [],
            }
        if args[:2] == ["pr", "view"]:
            return {
                "headRefOid": "b" * 40,
                "reviews": [],
                "comments": [],
                "statusCheckRollup": [],
            }
        cursor = next((value for value in args if value.startswith("cursor=")), None)
        nodes = [{"id": "T2"}] if cursor else [{"id": "T1"}]
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": cursor is None,
                                "endCursor": "next" if cursor is None else None,
                            },
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(MODULE, "_gh_json", fake_gh)
    try:
        MODULE.collect("owner/repo", 42)
    except RuntimeError as error:
        assert str(error) == "github_pr_surface_changed_during_collection"
    else:
        raise AssertionError("RuntimeError was not raised")


def test_missing_gh_is_reported_as_runtime_error(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", missing)
    try:
        MODULE._gh_json(["version"])
    except RuntimeError as error:
        assert str(error) == "github_cli_unavailable"
    else:
        raise AssertionError("RuntimeError was not raised")


def test_grace_requires_two_settled_pass_samples(monkeypatch, capsys) -> None:
    reports = iter([{"status": "pending"}, {"status": "pass"}, {"status": "pass"}])
    monkeypatch.setattr(MODULE, "collect", lambda repo, number: ({}, []))
    monkeypatch.setattr(
        MODULE, "evaluate_snapshot", lambda *args, **kwargs: next(reports)
    )
    monkeypatch.setattr(MODULE.time, "sleep", lambda seconds: None)
    assert (
        MODULE.main(
            [
                "--repo",
                "owner/repo",
                "--pr",
                "42",
                "--expected-head",
                HEAD,
                "--grace-seconds",
                "1",
            ]
        )
        == 0
    )
    assert '"status": "pass"' in capsys.readouterr().out


def test_commented_review_does_not_clear_changes_requested() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-08-27T00:01:00Z",
            },
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "COMMENTED",
                "submittedAt": "2026-08-27T00:02:00Z",
            },
        ],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "blocked"
    assert "changes_requested" in report["reasons"]


def test_unknown_mergeability_is_pending() -> None:
    pr = {
        "headRefOid": HEAD,
        "reviews": [
            {"author": {"login": "chatgpt-codex-connector"}, "commit": {"oid": HEAD}}
        ],
        "comments": [],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "mergeable": "UNKNOWN",
        "mergeStateStatus": "UNKNOWN",
    }
    report = MODULE.evaluate_snapshot(
        pr, [], expected_head=HEAD, required_reviewer="chatgpt-codex-connector"
    )
    assert report["status"] == "pending"
    assert "mergeability_pending" in report["reasons"]


def test_only_exact_non_minimized_comment_creates_review_generation() -> None:
    reviews = [
        {
            "author": {"login": "chatgpt-codex-connector"},
            "commit": {"oid": HEAD},
            "state": "COMMENTED",
            "submittedAt": "2026-08-27T00:01:00Z",
        }
    ]
    checks = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    discussed = snapshot(
        reviews=reviews,
        comments=[
            {
                "body": "Do not run @codex review yet",
                "createdAt": "2026-08-27T00:02:00Z",
            }
        ],
        checks=checks,
    )
    minimized = snapshot(
        reviews=reviews,
        comments=[
            {
                "body": "@codex review",
                "createdAt": "2026-08-27T00:02:00Z",
                "isMinimized": True,
            }
        ],
        checks=checks,
    )
    assert discussed["status"] == "pass"
    assert minimized["status"] == "pass"


def test_change_request_survives_new_review_generation() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-08-27T00:01:00Z",
            },
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "COMMENTED",
                "submittedAt": "2026-08-27T00:03:00Z",
            },
        ],
        comments=[{"body": "@codex review", "createdAt": "2026-08-27T00:02:00Z"}],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "blocked"
    assert "changes_requested" in report["reasons"]


def test_pending_draft_review_does_not_satisfy_requirement() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "PENDING",
                "submittedAt": "2026-08-27T00:01:00Z",
            }
        ],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "pending"
    assert "required_review_pending" in report["reasons"]


def test_reviewer_login_is_case_insensitive() -> None:
    report = MODULE.evaluate_snapshot(
        {
            "headRefOid": HEAD,
            "reviews": [
                {
                    "author": {"login": "ChatGPT-Codex-Connector"},
                    "commit": {"oid": HEAD},
                    "state": "COMMENTED",
                }
            ],
            "comments": [],
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        },
        [],
        expected_head=HEAD,
        required_reviewer="chatgpt-codex-connector",
    )
    assert report["status"] == "pass"


def test_blocked_merge_state_prevents_pass() -> None:
    pr = {
        "headRefOid": HEAD,
        "reviews": [
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "COMMENTED",
            }
        ],
        "comments": [],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
    }
    report = MODULE.evaluate_snapshot(
        pr, [], expected_head=HEAD, required_reviewer="chatgpt-codex-connector"
    )
    assert report["status"] == "blocked"
    assert "merge_state_blocked" in report["reasons"]


def test_same_second_review_does_not_satisfy_new_request() -> None:
    timestamp = "2026-08-27T00:01:00Z"
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "COMMENTED",
                "submittedAt": timestamp,
            }
        ],
        comments=[{"body": "@codex review", "createdAt": timestamp}],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "pending"
    assert "required_review_pending" in report["reasons"]


def test_dismissed_approval_reveals_prior_change_request() -> None:
    report = snapshot(
        reviews=[
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-08-27T00:01:00Z",
            },
            {
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": HEAD},
                "state": "DISMISSED",
                "submittedAt": "2026-08-27T00:02:00Z",
            },
        ],
        checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    assert report["status"] == "blocked"
    assert "changes_requested" in report["reasons"]


def test_gh_timeout_is_reported_as_runtime_error(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 30)

    monkeypatch.setattr(subprocess, "run", timeout)
    try:
        MODULE._gh_json(["version"])
    except RuntimeError as error:
        assert str(error) == "github_cli_timeout"
    else:
        raise AssertionError("RuntimeError was not raised")


def test_collect_rejects_thread_surface_drift(monkeypatch) -> None:
    graph_calls = 0
    stable_pr = {
        "headRefOid": HEAD,
        "reviews": [],
        "comments": [],
        "statusCheckRollup": [],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }

    def fake_gh(args):
        nonlocal graph_calls
        if args[:2] == ["pr", "view"]:
            return stable_pr
        graph_calls += 1
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "T1",
                                    "isResolved": graph_calls == 1,
                                    "isOutdated": False,
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(MODULE, "_gh_json", fake_gh)
    try:
        MODULE.collect("owner/repo", 42)
    except RuntimeError as error:
        assert str(error) == "github_thread_surface_changed_during_collection"
    else:
        raise AssertionError("RuntimeError was not raised")
