#!/usr/bin/env python3
"""プリリポを通したときだけ GitHub repository を公開に切り替える専用コマンド。

GitHub には「公開に切り替える操作そのもの」を止める仕組みが無い。公開はこのコマンド経由に限ると
運用で決め、ここで次をすべて満たしたときだけ `gh repo edit --visibility public` を実行する。

1. 手元の clone が origin の default branch と同じ commit で、origin の全 branch / tag を取得済み
   （検査した履歴と公開される履歴を一致させる）
2. readiness_scan.py の全体検査（削除済みを含む全履歴）が `pass`。ラチェットの基準値は使わない
3. 公開後も検査が走り続けるよう、HEAD に共通 CI ゲート（`preflight-gate.yml` を呼ぶ workflow）と
   ラチェットの基準値ファイルがある
4. `--apply` と、対象 repository 名をもう一度打った `--confirm OWNER/NAME` がある

既定は dry-run で、何も変更しない。結果は `repo-preflight.publish/v1` の JSON で返す。
「pass」は機械で調べた範囲の結果で、公開してよいという承認ではない。承認は人が出す。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

SCHEMA = "repo-preflight.publish/v1"
SCAN_SCRIPT = Path(__file__).resolve().parent / "readiness_scan.py"
GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


class GateStop(Exception):
    """公開へ進まない理由。status と issue を結果へそのまま載せる。"""

    def __init__(self, status: str, issue: str):
        super().__init__(issue)
        self.status = status
        self.issue = issue


def git(run: Runner, repo: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", "-c", "core.fsmonitor=", "-C", str(repo), *args])


def origin_slug(run: Runner, repo: Path) -> str:
    proc = git(run, repo, "remote", "get-url", "origin")
    if proc.returncode != 0:
        raise GateStop("tool_error", "origin_unavailable")
    match = GITHUB_REMOTE_RE.match(proc.stdout.strip())
    if not match:
        raise GateStop("blocked", "origin_not_github")
    return match.group("slug")


def remote_state(run: Runner, slug: str) -> dict:
    proc = run(
        [
            "gh",
            "repo",
            "view",
            slug,
            "--json",
            "visibility,isArchived,defaultBranchRef",
        ]
    )
    if proc.returncode != 0:
        raise GateStop("tool_error", "gh_repo_view_failed")
    try:
        data = json.loads(proc.stdout)
        return {
            "visibility": str(data["visibility"]).upper(),
            "archived": bool(data.get("isArchived")),
            "default_branch": data["defaultBranchRef"]["name"],
        }
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateStop("tool_error", "gh_repo_view_unreadable") from exc


def check_remote_refs_fetched(run: Runner, repo: Path, default_branch: str) -> str:
    """origin の全 ref が手元にあり、HEAD が default branch と同じなら HEAD を返す。"""
    listed = git(run, repo, "ls-remote", "--heads", "--tags", "origin")
    if listed.returncode != 0:
        raise GateStop("tool_error", "ls_remote_failed")
    default_sha = None
    for line in listed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            raise GateStop("tool_error", "ls_remote_unreadable")
        sha, ref = parts
        if git(run, repo, "cat-file", "-e", sha).returncode != 0:
            raise GateStop("blocked", "remote_refs_not_fetched")
        if ref == f"refs/heads/{default_branch}":
            default_sha = sha
    if default_sha is None:
        raise GateStop("blocked", "default_branch_missing_on_origin")
    head = git(run, repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise GateStop("tool_error", "head_unavailable")
    head_sha = head.stdout.strip()
    if head_sha != default_sha:
        raise GateStop("blocked", "head_differs_from_origin_default_branch")
    return head_sha


GATE_USES_RE = re.compile(
    r"^\s*uses:\s*['\"]?(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|\.)"
    r"/\.github/workflows/preflight-gate\.ya?ml(?:@\S+)?['\"]?\s*(?:#.*)?$",
    re.M,
)
BASELINE_INPUT_RE = re.compile(r"^\s*baseline:\s*['\"]?([^'\"#\s]+)", re.M)
DEFAULT_BASELINE = ".repo-preflight/ratchet-baseline.json"


def check_ci_gate(run: Runner, repo: Path, head: str) -> str:
    """HEAD に共通 CI ゲートと基準値があるかを確かめ、ゲートの workflow の path を返す。

    publish_gate を通しても、ゲートが無ければ公開後の push / PR では検査が一度も走らない。
    """
    listed = git(
        run, repo, "ls-tree", "-r", "--name-only", head, "--", ".github/workflows"
    )
    if listed.returncode != 0:
        raise GateStop("tool_error", "workflow_list_failed")
    for path in sorted(listed.stdout.splitlines()):
        if not path.endswith((".yml", ".yaml")):
            continue
        shown = git(run, repo, "show", f"{head}:{path}")
        if shown.returncode != 0:
            raise GateStop("tool_error", "workflow_read_failed")
        if not GATE_USES_RE.search(shown.stdout):
            continue
        match = BASELINE_INPUT_RE.search(shown.stdout)
        baseline = match.group(1) if match else DEFAULT_BASELINE
        if git(run, repo, "cat-file", "-e", f"{head}:{baseline}").returncode != 0:
            raise GateStop("blocked", "ratchet_baseline_missing")
        return path
    raise GateStop("blocked", "ci_gate_missing")


def run_scan(run: Runner, repo: Path, scan_args: Sequence[str]) -> dict:
    proc = run([sys.executable, str(SCAN_SCRIPT), "--repo", str(repo), *scan_args])
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GateStop("tool_error", "scan_output_unreadable") from exc
    return report


def evaluate(
    repo: Path,
    *,
    apply: bool,
    confirm: str | None,
    expected_slug: str | None = None,
    scan_args: Sequence[str] = (),
    run: Runner = default_runner,
) -> dict:
    result: dict = {"schema": SCHEMA, "applied": False}
    try:
        slug = origin_slug(run, repo)
        result["repository"] = slug
        if expected_slug and expected_slug != slug:
            raise GateStop("blocked", "origin_differs_from_expected_repository")
        state = remote_state(run, slug)
        result["visibility_before"] = state["visibility"]
        if state["visibility"] == "PUBLIC":
            result["status"] = "already_public"
            return result
        if state["archived"]:
            raise GateStop("blocked", "repository_archived")
        head = check_remote_refs_fetched(run, repo, state["default_branch"])
        result["head"] = head
        scan = run_scan(run, repo, scan_args)
        result["scan_status"] = scan.get("status")
        if scan.get("status") == "tool_error":
            raise GateStop("tool_error", "scan_tool_error")
        if scan.get("status") != "pass":
            result["failed_checks"] = sorted(
                name
                for name, check in (scan.get("checks") or {}).items()
                if isinstance(check, dict)
                and check.get("status") in {"fail", "unknown", "tool_error"}
            )
            raise GateStop("blocked", "scan_not_pass")
        result["ci_gate"] = check_ci_gate(run, repo, head)
        if not apply:
            result["status"] = "ready"
            result["next"] = f"--apply --confirm {slug}"
            return result
        if confirm != slug:
            raise GateStop("blocked", "confirmation_mismatch")
        # 検査から切り替えまでの間に push されていないかを直前に確かめ直す
        if check_remote_refs_fetched(run, repo, state["default_branch"]) != head:
            raise GateStop("blocked", "origin_changed_during_gate")
        edited = run(
            [
                "gh",
                "repo",
                "edit",
                slug,
                "--visibility",
                "public",
                "--accept-visibility-change-consequences",
            ]
        )
        if edited.returncode != 0:
            raise GateStop("tool_error", "gh_repo_edit_failed")
        result["applied"] = True
        after = remote_state(run, slug)
        result["visibility_after"] = after["visibility"]
        if after["visibility"] != "PUBLIC":
            raise GateStop("tool_error", "visibility_not_confirmed")
        result["status"] = "published"
        return result
    except GateStop as stop:
        result["status"] = stop.status
        result["issue"] = stop.issue
        return result


EXIT_CODES = {"published": 0, "ready": 0, "already_public": 0, "blocked": 1}

MESSAGES = {
    "published": "公開に切り替え、GitHub 上で PUBLIC を確認しました。",
    "ready": "機械の検査はすべて通りました。人が公開を決めたら --apply --confirm を付けて再実行してください。",
    "already_public": "すでに公開されています。何も変更していません。",
    "blocked": "公開に切り替えていません。issue と failed_checks を直してから再実行してください。",
    "tool_error": "確認できなかったため公開に切り替えていません。この結果を判断に使わないでください。",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", type=Path, required=True, help="公開する repo の clone"
    )
    parser.add_argument(
        "--github",
        help="OWNER/NAME。指定すると origin と一致するかも確かめる",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="検査が通ったら実際に公開へ切り替える（既定は dry-run）",
    )
    parser.add_argument(
        "--confirm",
        help="--apply のとき、対象の OWNER/NAME をもう一度書く",
    )
    parser.add_argument(
        "--reviewed-secret-exceptions",
        help="readiness_scan.py へそのまま渡す（レビュー済み例外）",
    )
    return parser


def main(argv: list[str] | None = None, run: Runner = default_runner) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    if args.github and not SLUG_RE.match(args.github):
        print("error: --github must be OWNER/NAME", file=sys.stderr)
        return 2
    if args.apply and not args.confirm:
        print("error: --apply requires --confirm OWNER/NAME", file=sys.stderr)
        return 2
    scan_args = []
    if args.reviewed_secret_exceptions:
        scan_args += ["--reviewed-secret-exceptions", args.reviewed_secret_exceptions]
    result = evaluate(
        args.repo.resolve(),
        apply=args.apply,
        confirm=args.confirm,
        expected_slug=args.github,
        scan_args=scan_args,
        run=run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(MESSAGES[result["status"]], file=sys.stderr)
    return EXIT_CODES.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
