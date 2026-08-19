from __future__ import annotations

import importlib.util
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
            {"author": {"login": "chatgpt-codex-connector"}, "commit": {"oid": HEAD}}
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
            {"author": {"login": "chatgpt-codex-connector"}, "commit": {"oid": HEAD}}
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
