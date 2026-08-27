#!/usr/bin/env python3
"""PRのCI・review・inline threadをexact HEADへ束縛してread-only評価する。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any


def _login(value: object) -> str:
    return value.removesuffix("[bot]").casefold() if isinstance(value, str) else ""


def evaluate_snapshot(
    pr: dict[str, Any],
    threads: list[dict[str, Any]],
    *,
    expected_head: str,
    required_reviewer: str,
) -> dict[str, Any]:
    actual_head = pr.get("headRefOid")
    if actual_head != expected_head:
        return {
            "schema": "repo-preflight.pr-review-snapshot/v1",
            "status": "tool_error",
            "expected_head": expected_head,
            "actual_head": actual_head,
            "reasons": ["head_mismatch"],
        }

    unresolved = sorted(
        str(item.get("id")) for item in threads if not item.get("isResolved")
    )
    reviews = pr.get("reviews") if isinstance(pr.get("reviews"), list) else []
    comments = pr.get("comments") if isinstance(pr.get("comments"), list) else []
    request_times = sorted(
        str(comment.get("createdAt"))
        for comment in comments
        if isinstance(comment, dict)
        and str(comment.get("body", "")).strip().casefold() == "@codex review"
        and not comment.get("isMinimized")
        and comment.get("createdAt")
    )
    latest_request = request_times[-1] if request_times else None
    reviewed_head = None
    review_state = None
    same_head_reviews: list[dict[str, Any]] = []
    for review in reviews:
        author = review.get("author") if isinstance(review, dict) else None
        commit = review.get("commit") if isinstance(review, dict) else None
        if (
            isinstance(author, dict)
            and _login(author.get("login")) == _login(required_reviewer)
            and isinstance(commit, dict)
            and commit.get("oid") == expected_head
        ):
            same_head_reviews.append(review)
    matching_reviews = [
        review
        for review in same_head_reviews
        if str(review.get("state", "")).upper()
        in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}
        and (
            latest_request is None
            or str(review.get("submittedAt", "")) > latest_request
        )
    ]
    if matching_reviews:
        reviewed_head = expected_head
    decisive_reviews = [
        item
        for item in same_head_reviews
        if str(item.get("state", "")).upper() in {"APPROVED", "CHANGES_REQUESTED"}
    ]
    if decisive_reviews:
        latest_decisive_review = max(
            decisive_reviews, key=lambda item: str(item.get("submittedAt", ""))
        )
        review_state = str(latest_decisive_review.get("state", "")).upper()

    checks = (
        pr.get("statusCheckRollup")
        if isinstance(pr.get("statusCheckRollup"), list)
        else []
    )

    def check_state(item: dict[str, Any]) -> str:
        if "state" in item and "status" not in item:
            state = str(item.get("state", "")).upper()
            if state == "SUCCESS":
                return "success"
            if state in {"ERROR", "FAILURE"}:
                return "failed"
            return "pending"
        if item.get("status") != "COMPLETED":
            return "pending"
        if item.get("conclusion") in {"SUCCESS", "SKIPPED", "NEUTRAL"}:
            return "success"
        return "failed"

    check_states = [check_state(item) for item in checks if isinstance(item, dict)]
    ci_failed = "failed" in check_states
    ci_pending = not checks or "pending" in check_states

    reasons: list[str] = []
    status = "pass"
    if unresolved:
        reasons.append("unresolved_review_threads")
        status = "blocked"
    if ci_failed:
        reasons.append("ci_failed")
        status = "blocked"
    elif ci_pending:
        reasons.append("ci_pending")
        if status == "pass":
            status = "pending"
    if reviewed_head is None:
        reasons.append("required_review_pending")
        if status == "pass":
            status = "pending"
    elif review_state == "CHANGES_REQUESTED":
        reasons.append("changes_requested")
        status = "blocked"
    if pr.get("mergeable") == "UNKNOWN":
        reasons.append("mergeability_pending")
        if status == "pass":
            status = "pending"
    elif pr.get("mergeable") not in {None, "MERGEABLE"}:
        reasons.append("not_mergeable")
        status = "blocked"
    if pr.get("mergeStateStatus") == "BLOCKED":
        reasons.append("merge_state_blocked")
        status = "blocked"

    return {
        "schema": "repo-preflight.pr-review-snapshot/v1",
        "status": status,
        "expected_head": expected_head,
        "actual_head": actual_head,
        "reviewed_head": reviewed_head,
        "required_reviewer": _login(required_reviewer),
        "unresolved_thread_ids": unresolved,
        "ci_check_count": len(checks),
        "reasons": reasons,
        "mergeable": pr.get("mergeable"),
        "merge_state_status": pr.get("mergeStateStatus"),
    }


def _gh_json(args: list[str]) -> Any:
    try:
        completed = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("github_cli_timeout") from error
    except OSError as error:
        raise RuntimeError("github_cli_unavailable") from error
    if completed.returncode:
        raise RuntimeError("github_snapshot_failed")
    return json.loads(completed.stdout)


def collect(repo: str, number: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pr_args = [
        "pr",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "headRefOid,reviews,comments,statusCheckRollup,mergeable,mergeStateStatus",
    ]
    pr = _gh_json(pr_args)
    owner, name = repo.split("/", 1)
    query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{id isResolved isOutdated} pageInfo{hasNextPage endCursor}}}}}"""

    def collect_threads() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor = None
        while True:
            graph_args = [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={number}",
            ]
            if cursor:
                graph_args.extend(["-F", f"cursor={cursor}"])
            graph = _gh_json(graph_args)
            page = graph["data"]["repository"]["pullRequest"]["reviewThreads"]
            result.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return result
            cursor = page["pageInfo"]["endCursor"]
            if not cursor:
                raise RuntimeError("github_thread_pagination_failed")

    threads = collect_threads()
    middle_pr = _gh_json(pr_args)
    final_threads = collect_threads()
    final_pr = _gh_json(pr_args)
    if middle_pr != pr or final_pr != pr:
        raise RuntimeError("github_pr_surface_changed_during_collection")
    if final_threads != threads:
        raise RuntimeError("github_thread_surface_changed_during_collection")
    return pr, threads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="GitHub PRのreview surfaceをexact HEADへ束縛して確認する"
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--required-reviewer", default="chatgpt-codex-connector")
    parser.add_argument("--grace-seconds", type=int, default=0)
    args = parser.parse_args(argv)
    if args.grace_seconds < 0 or args.grace_seconds > 60:
        parser.error("--grace-seconds must be between 0 and 60")
    try:
        pr, threads = collect(args.repo, args.pr)
        report = evaluate_snapshot(
            pr,
            threads,
            expected_head=args.expected_head,
            required_reviewer=args.required_reviewer,
        )
        first_status = report["status"]
        if report["status"] in {"pending", "pass"} and args.grace_seconds:
            time.sleep(args.grace_seconds)
            pr, threads = collect(args.repo, args.pr)
            report = evaluate_snapshot(
                pr,
                threads,
                expected_head=args.expected_head,
                required_reviewer=args.required_reviewer,
            )
            if first_status != "pass" and report["status"] == "pass":
                time.sleep(args.grace_seconds)
                pr, threads = collect(args.repo, args.pr)
                report = evaluate_snapshot(
                    pr,
                    threads,
                    expected_head=args.expected_head,
                    required_reviewer=args.required_reviewer,
                )
    except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
        report = {
            "schema": "repo-preflight.pr-review-snapshot/v1",
            "status": "tool_error",
            "reasons": [str(error)],
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return {"pass": 0, "blocked": 1, "pending": 2, "tool_error": 3}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
