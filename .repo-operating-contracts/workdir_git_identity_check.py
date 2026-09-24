import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
Runner = Callable[[list[str], Path], tuple[int, str, str]]


def run_command(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        creationflags=NO_WINDOW,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _run_git(runner: Runner, cwd: Path, args: list[str]) -> str | None:
    code, stdout, _stderr = runner(["git", *args], cwd)
    if code != 0:
        return None
    return stdout.strip()


def _parse_ahead_behind(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    parts = text.split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def _parse_status_v2(
    status: str | None,
) -> tuple[str | None, str | None, int | None, int | None, list[str]]:
    """`git status --porcelain=v2 --branch` を読む。

    `--short` の header は color.ui / color.status=always で ANSI escape が入り、
    翻訳もされる。porcelain v2 は利用者設定に左右されない機械向け形式なので、
    branch / upstream / ahead-behind / 変更行をここからだけ取る。
    """
    branch: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    changes: list[str] = []
    for line in (status or "").splitlines():
        if line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ").strip()
            branch = None if head == "(detached)" else head or None
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ").strip() or None
        elif line.startswith("# branch.ab "):
            match = re.fullmatch(r"# branch\.ab \+(\d+) -(\d+)", line.strip())
            if match:
                ahead, behind = int(match.group(1)), int(match.group(2))
        elif line and not line.startswith("#"):
            changes.append(line)
    return branch, upstream, ahead, behind, changes


def _sync_state(upstream: str | None, ahead: int | None, behind: int | None) -> str:
    if not upstream:
        return "no_upstream"
    if ahead is None or behind is None:
        return "unknown"
    if ahead and behind:
        return "diverged"
    if ahead:
        return "ahead"
    if behind:
        return "behind"
    return "synced"


_NETWORK_SCHEMES = {"https", "http", "ssh", "git", "git+ssh", "ssh+git"}
_SCP_LIKE = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>.+)$")


def _split_remote_url(url: str) -> tuple[str, list[str]] | None:
    """remote URL を (host, path segment 列) に分ける。network URL でなければ None。"""
    url = url.strip()
    if not url:
        return None
    if "://" in url:
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            return None
        if parts.scheme.lower() not in _NETWORK_SCHEMES or not host:
            return None
        path = parts.path
    else:
        match = _SCP_LIKE.match(url)
        # "C:/work/repo" のような drive letter 付き local path は scp 形式ではない。
        if not match or len(match.group("host")) == 1:
            return None
        host, path = match.group("host"), match.group("path")
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")]
    return host.lower(), [segment for segment in path.split("/")]


def _remote_url_matches(url: str, expected_host: str, expected_owner: str, expected_repo: str) -> bool:
    """URL の host が一致し、path が丁度 owner/repo の 2 段で両方一致する時だけ True。"""
    if not expected_repo:
        return False
    split = _split_remote_url(url)
    if split is None:
        return False
    host, segments = split
    if host != expected_host.strip().lower() or len(segments) != 2:
        return False
    owner, repo = segments
    # owner 未指定は照合不能。飛ばすと fork や archive owner も一致扱いになる。
    if not expected_owner or owner.lower() != expected_owner.lower():
        return False
    return repo.lower() == expected_repo.lower()


def _config_value(runner: Runner, cwd: Path, key: str) -> str | None:
    return _run_git(runner, cwd, ["config", "--get", key]) or None


def _identity_remotes(runner: Runner, cwd: Path, branch: str | None) -> dict[str, Any]:
    """今の branch が実際に fetch / push に使う remote と URL を git の既定順で解決する。

    fetch: branch.<b>.remote、無ければ origin。
    push: branch.<b>.pushRemote、remote.pushDefault、branch.<b>.remote、origin の順。
    push URL は pushurl があればそれ全部、無ければ url。
    """
    branch_remote = _config_value(runner, cwd, f"branch.{branch}.remote") if branch else None
    push_remote = (
        (_config_value(runner, cwd, f"branch.{branch}.pushRemote") if branch else None)
        or _config_value(runner, cwd, "remote.pushDefault")
        or branch_remote
        or "origin"
    )
    fetch_remote = branch_remote or "origin"

    fetch_url = _run_git(runner, cwd, ["remote", "get-url", fetch_remote])
    push_urls_text = _run_git(runner, cwd, ["remote", "get-url", "--push", "--all", push_remote])
    push_urls = [line.strip() for line in (push_urls_text or "").splitlines() if line.strip()]
    return {
        "fetch_remote": fetch_remote,
        "fetch_url": fetch_url or None,
        "push_remote": push_remote,
        "push_urls": push_urls,
    }


def _remotes_match(remotes: dict[str, Any], expected_host: str, expected_owner: str, expected_repo: str) -> bool:
    urls = [remotes["fetch_url"], *remotes["push_urls"]]
    if not remotes["fetch_url"] or not remotes["push_urls"]:
        return False
    return all(_remote_url_matches(url, expected_host, expected_owner, expected_repo) for url in urls)


def check_workdir_identity(
    cwd: Path,
    expected_repo: str,
    expected_owner: str = "",
    expected_root_hint: str = "",
    expected_host: str = "github.com",
    current_task_goal: str = "",
    allow_dirty: bool = False,
    allow_no_upstream: bool = False,
    runner: Runner = run_command,
) -> dict[str, Any]:
    root = _run_git(runner, cwd, ["rev-parse", "--show-toplevel"])
    status = _run_git(runner, cwd, ["status", "--porcelain=v2", "--branch"])

    branch, upstream, ahead, behind, dirty_lines = _parse_status_v2(status)
    if branch is None:
        branch = _run_git(runner, cwd, ["branch", "--show-current"])
    resolved_upstream = _run_git(runner, cwd, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    # 追跡先が設定されているのに解決できなければ gone (remote で削除 / prune、remote 自体が無い)。
    # 追跡設定の無い新規 local branch だけが allow_no_upstream の対象。
    tracking_configured = bool(upstream) or bool(
        branch
        and (
            _config_value(runner, cwd, f"branch.{branch}.remote")
            or _config_value(runner, cwd, f"branch.{branch}.merge")
        )
    )
    upstream_gone = tracking_configured and resolved_upstream is None
    upstream = None if upstream_gone else (resolved_upstream or upstream)
    if upstream and (ahead is None or behind is None):
        ahead_behind_text = _run_git(runner, cwd, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
        ahead, behind = _parse_ahead_behind(ahead_behind_text)

    sync_state = "no_upstream" if upstream_gone else _sync_state(upstream, ahead, behind)

    root_text = root or ""
    # 同一性は「今の branch が実際に fetch / push する remote の URL が、host と owner/repo まで
    # 完全一致する」ことだけで決める。他の remote、remote 名、checkout path、URL の部分一致は数えない。
    remotes = _identity_remotes(runner, cwd, branch)
    repo_match = _remotes_match(remotes, expected_host, expected_owner, expected_repo)
    hint_match = not expected_root_hint or expected_root_hint.replace("\\", "/").lower() in root_text.replace("\\", "/").lower()

    if not root:
        repo_identity = "unknown"
    elif repo_match and hint_match:
        repo_identity = "matched"
    else:
        repo_identity = "mismatched"

    workdir_state = "dirty" if dirty_lines else "clean"

    if repo_identity == "mismatched":
        recommendation = "stop_for_review"
        reason = "想定 repo と実際の作業場所が一致していません。"
    elif repo_identity == "unknown":
        recommendation = "suggest_new_chat"
        reason = "Git repository の同一性を確認できません。"
    elif sync_state == "diverged" or (sync_state == "no_upstream" and (upstream_gone or not allow_no_upstream)):
        # allow_no_upstream は新規 local branch 用。消失 (gone) した upstream は許可対象外。
        recommendation = "stop_for_review"
        reason = "upstream が未設定または消失 (gone)、あるいは branch が diverged しています。"
    elif workdir_state == "dirty" and not allow_dirty:
        recommendation = "stop_for_review"
        reason = "未整理の dirty state があります。"
    else:
        recommendation = "continue_here"
        reason = "作業場所と Git 同一性は想定内です。"

    return {
        "recommendation": recommendation,
        "repo_identity": repo_identity,
        "workdir_state": workdir_state,
        "sync_state": sync_state,
        "reason": reason,
        "safe_next_action": "書き込み前に main task と changed files を確認する。",
        "skill": "side-route-chat-router",
        "facts": {
            "repo_root": root,
            "branch": branch,
            "upstream": upstream,
            "upstream_gone": upstream_gone,
            "fetch_remote": remotes["fetch_remote"],
            "fetch_url": remotes["fetch_url"],
            "push_remote": remotes["push_remote"],
            "push_urls": remotes["push_urls"],
            "ahead": ahead,
            "behind": behind,
            "current_task_goal": current_task_goal,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="作業ディレクトリと Git repository の同一性を read-only で確認します。")
    parser.add_argument("--expected-repo", required=True)
    parser.add_argument("--expected-owner", required=True)
    parser.add_argument("--expected-root-hint", default="")
    parser.add_argument(
        "--expected-host",
        default="github.com",
        help="remote URL の host。fetch / push URL の host がこれと一致しない時は matched にしません。",
    )
    parser.add_argument("--current-task-goal", default="")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--allow-no-upstream",
        action="store_true",
        help="新規local branchであることを確認済みの場合だけno_upstreamを許可します。",
    )
    parser.add_argument("--cwd", default=".")
    args = parser.parse_args()

    result = check_workdir_identity(
        cwd=Path(args.cwd).resolve(),
        expected_repo=args.expected_repo,
        expected_owner=args.expected_owner,
        expected_root_hint=args.expected_root_hint,
        expected_host=args.expected_host,
        current_task_goal=args.current_task_goal,
        allow_dirty=args.allow_dirty,
        allow_no_upstream=args.allow_no_upstream,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
