#!/usr/bin/env python3
"""プリリポを通したときだけ GitHub repository を公開に切り替える専用コマンド。

GitHub には「公開に切り替える操作そのもの」を止める仕組みが無い。公開はこのコマンド経由に限ると
運用で決め、ここで次をすべて満たしたときだけ `gh repo edit --visibility public` を実行する。

1. 手元の clone が origin の default branch と同じ commit で、origin の全 branch / tag を取得済み
   （検査した履歴と公開される履歴を一致させる）
2. readiness_scan.py の全体検査（削除済みを含む全履歴）が `pass`。ラチェットの基準値は使わない
3. 公開後も検査が走り続けるよう、HEAD に共通 CI ゲート（`preflight-gate.yml` を呼ぶ workflow）と
   ラチェットの基準値ファイルがある
4. `--apply` と、対象 repository 名をもう一度打った `--confirm OWNER/NAME` と dry-run の --reviewed-snapshot がある

既定は dry-run で、何も変更しない。結果は `repo-preflight.publish/v1` の JSON で返す。
「pass」は機械で調べた範囲の結果で、公開してよいという承認ではない。承認は人が出す。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Sequence

from readiness_scan import PrivateIdentityScan

SCHEMA = "repo-preflight.publish/v1"
SCAN_SCRIPT = Path(__file__).resolve().parent / "readiness_scan.py"
GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except OSError as exc:
        tool = cmd[0] if cmd[0] in {"git", "gh"} else "scanner"
        raise GateStop("tool_error", f"{tool}_launch_failed") from exc


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
            f"github.com/{slug}",
            "--json",
            "id,nameWithOwner,visibility,isArchived,defaultBranchRef",
        ]
    )
    if proc.returncode != 0:
        raise GateStop("tool_error", "gh_repo_view_failed")
    try:
        data = json.loads(proc.stdout)
        if (
            not isinstance(data["id"], str)
            or not data["id"]
            or not isinstance(data["nameWithOwner"], str)
            or not SLUG_RE.fullmatch(data["nameWithOwner"])
            or data["visibility"] not in {"PRIVATE", "PUBLIC", "INTERNAL"}
            or not isinstance(data["isArchived"], bool)
        ):
            raise GateStop("tool_error", "gh_repo_view_unreadable")
        return {
            "id": data["id"],
            "name_with_owner": data["nameWithOwner"],
            "visibility": data["visibility"],
            "archived": data["isArchived"],
            "default_branch": data["defaultBranchRef"]["name"],
        }
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateStop("tool_error", "gh_repo_view_unreadable") from exc


class CheckedRefs(NamedTuple):
    head: str
    remote: dict[str, str]
    closure_digest: str


def checked_remote_refs(run: Runner, repo: Path, default_branch: str) -> CheckedRefs:
    """全履歴検査がたどれる origin の branch と tag を OID に固定する。"""
    listed = git(run, repo, "ls-remote", "--refs", "--heads", "--tags", "origin")
    if listed.returncode != 0:
        raise GateStop("tool_error", "ls_remote_failed")
    refs = {}
    for line in listed.stdout.rstrip("\n").split("\n") if listed.stdout else ():
        parts = line.split("\t")
        if len(parts) != 2:
            raise GateStop("tool_error", "ls_remote_unreadable")
        sha, ref = parts
        if ref in refs or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
            raise GateStop("tool_error", "ls_remote_unreadable")
        refs[ref] = sha
    default_sha = refs.get(f"refs/heads/{default_branch}")
    if default_sha is None:
        raise GateStop("blocked", "default_branch_missing_on_origin")
    head = git(run, repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise GateStop("tool_error", "head_unavailable")
    head_sha = head.stdout.strip()
    if head_sha != default_sha:
        raise GateStop("blocked", "head_differs_from_origin_default_branch")
    commit_tips = git(run, repo, "rev-list", "--no-walk=unsorted", "--all", "HEAD")
    if commit_tips.returncode != 0:
        raise GateStop("tool_error", "local_refs_unreadable")
    tips = git(run, repo, "rev-parse", "--revs-only", "--all", "HEAD")
    if tips.returncode != 0:
        raise GateStop("tool_error", "local_refs_unreadable")
    roots = dict.fromkeys(tips.stdout.splitlines() + commit_tips.stdout.splitlines())
    if set(refs.values()).difference(roots):
        raise GateStop("blocked", "remote_refs_not_fetched")
    closure = hashlib.sha256("\n".join(roots).encode("ascii")).hexdigest()
    return CheckedRefs(head_sha, refs, closure)


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
    proc = run(
        [
            sys.executable,
            str(SCAN_SCRIPT),
            "--repo",
            str(repo),
            "--require-private-identity",
            *scan_args,
        ]
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GateStop("tool_error", "scan_output_unreadable") from exc
    if not isinstance(report, dict) or (
        report.get("status") == "pass" and proc.returncode != 0
    ):
        raise GateStop("tool_error", "scan_output_unreadable")
    return report


def check_public_names(checker: PrivateIdentityScan, names: Iterable[str]) -> None:
    for name in names:
        try:
            raw_name = name.encode("utf-8")
        except UnicodeEncodeError:
            raise GateStop("tool_error", "public_name_encoding_unverified") from None
        checker.check(raw_name, repository_name=True)
        if checker.report["status"] != "pass" or checker.skipped:
            raise GateStop("tool_error", "public_name_check_unverified")
        if checker.findings:
            raise GateStop("blocked", "private_identity_in_public_name")


def evaluate(
    repo: Path,
    *,
    apply: bool,
    confirm: str | None,
    reviewed_snapshot: str | None = None,
    expected_slug: str | None = None,
    scan_args: Sequence[str] = (),
    run: Runner = default_runner,
) -> dict:
    result: dict = {"schema": SCHEMA, "applied": False, "edit_attempted": False}
    try:
        slug = origin_slug(run, repo)
        names = PrivateIdentityScan(repo.resolve(), required=True)
        check_public_names(names, [slug])
        result["repository"] = slug
        if expected_slug and expected_slug.lower() != slug.lower():
            raise GateStop("blocked", "origin_differs_from_expected_repository")
        state = remote_state(run, slug)
        checked_name = state["name_with_owner"]
        check_public_names(names, [checked_name])
        if checked_name.lower() != slug.lower():
            raise GateStop("blocked", "github_repository_name_differs_from_origin")
        result["repository"] = checked_name
        result["visibility_before"] = state["visibility"]
        if state["visibility"] == "PUBLIC":
            result["status"] = "already_public"
            return result
        if state["archived"]:
            raise GateStop("blocked", "repository_archived")
        refs = checked_remote_refs(run, repo, state["default_branch"])
        if any(
            not ref.startswith(("refs/heads/", "refs/tags/")) for ref in refs.remote
        ):
            raise GateStop("tool_error", "ls_remote_unreadable")
        check_public_names(names, (ref.split("/", 2)[2] for ref in refs.remote))
        head = refs.head
        result["head"] = head
        snapshot = hashlib.sha256(
            json.dumps([slug, state, refs.remote], sort_keys=True).encode("utf-8")
        ).hexdigest()
        if apply and (confirm is None or confirm.lower() != checked_name.lower()):
            raise GateStop("blocked", "confirmation_mismatch")
        if apply and reviewed_snapshot != snapshot:
            raise GateStop("blocked", "reviewed_snapshot_mismatch")
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
        identity = (scan.get("checks") or {}).get("private_identity_scan") or {}
        if (
            scan.get("head") != head
            or identity.get("status") != "pass"
            or identity.get("closure_revisions_sha256") != refs.closure_digest
        ):
            raise GateStop("blocked", "scan_history_differs_from_review")
        result["ci_gate"] = check_ci_gate(run, repo, head)
        if not apply:
            result["status"] = "ready"
            result["reviewed_snapshot"] = snapshot
            result["next"] = (
                f"--apply --confirm {checked_name} --reviewed-snapshot {snapshot}"
            )
            return result
        # 検査から切り替えまでの間に push されていないかを直前に確かめ直す
        if (
            origin_slug(run, repo) != slug
            or remote_state(run, slug) != state
            or checked_remote_refs(run, repo, state["default_branch"]) != refs
        ):
            raise GateStop("blocked", "origin_changed_during_gate")
        result["edit_attempted"] = True
        edited = run(
            [
                "gh",
                "repo",
                "edit",
                f"github.com/{checked_name}",
                "--visibility",
                "public",
                "--accept-visibility-change-consequences",
            ]
        )
        if edited.returncode != 0:
            raise GateStop("tool_error", "gh_repo_edit_failed")
        result["applied"] = True
        after = remote_state(run, checked_name)
        result["visibility_after"] = after["visibility"]
        if (
            after["visibility"] != "PUBLIC"
            or after["id"] != state["id"]
            or after["name_with_owner"] != checked_name
        ):
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
    "ready": "機械の検査はすべて通りました。人が対象と履歴を確認したら next の引数で再実行してください。",
    "already_public": "すでに公開されています。何も変更していません。",
    "blocked": "公開に切り替えていません。issue と failed_checks を直してから再実行してください。",
    "tool_error": "確認できなかった項目があります。issue と GitHub 上の公開状態を確認してください。",
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
        "--reviewed-snapshot", help="人が確認したdry-runのreviewed_snapshot"
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
        reviewed_snapshot=args.reviewed_snapshot,
        scan_args=scan_args,
        run=run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(MESSAGES[result["status"]], file=sys.stderr)
    return EXIT_CODES.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
