#!/usr/bin/env python3
"""pre-push 用：送る commit の基準値を、送り先にある基準値（比較元）と比べる。

手元で基準値ファイルを書き換えて件数を上げても、送り先の基準値より緩めていれば push を止める。
検査（readiness_scan.py）は回さないので速い。全履歴の判定は CI の preflight-gate が行う。

標準入力は git の pre-push と同じ「local_ref local_sha remote_ref remote_sha」の行。
比較元は、送り先の commit（手元にあれば）→ 送り先を追う手元の ref（同じ枝・HEAD・main・master）
→ どれも無ければ全件 0（いちばん厳しい）。枝の削除とタグは見ない。
件数だけを扱い、指摘の中身は出さない。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 9):  # noqa: UP036 - Mac 標準より古い版では理由を出して止める
    sys.stderr.write("Python 3.9 以上が要ります。push を止めます\n")
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preflight_ratchet import (  # noqa: E402
    DEFAULT_BASELINE,
    RATCHET_CHECKS,
    RatchetError,
    read_baseline_at_ref,
)


def _is_zero(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def _has_commit(repo: Path, ref: str) -> bool:
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{ref}^{{commit}}",
            ],
            capture_output=True,
        ).returncode
        == 0
    )


def base_ref_for(
    repo: Path, remote: str, remote_ref: str, remote_sha: str
) -> str | None:
    """比較元の ref。見つからなければ None（全件 0 と比べる）。"""
    if not _is_zero(remote_sha) and _has_commit(repo, remote_sha):
        return remote_sha
    branch = remote_ref[len("refs/heads/") :]
    for ref in (
        f"refs/remotes/{remote}/{branch}",
        f"refs/remotes/{remote}/HEAD",
        f"refs/remotes/{remote}/main",
        f"refs/remotes/{remote}/master",
    ):
        if _has_commit(repo, ref):
            return ref
    return None


def check(repo: Path, remote: str, lines: list[str], rel: str) -> list[str]:
    """止める理由の一覧（空なら通す）。"""
    problems: list[str] = []
    zeros = {name: 0 for name in RATCHET_CHECKS}
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, remote_ref, remote_sha = parts
        if _is_zero(local_sha) or not remote_ref.startswith("refs/heads/"):
            continue
        pushed = read_baseline_at_ref(repo, local_sha, rel)
        if pushed is None:
            problems.append(
                f"基準値ファイル（{rel}）が無いので push を止めます（{remote_ref}）"
            )
            continue
        base_ref = base_ref_for(repo, remote, remote_ref, remote_sha)
        base = (
            read_baseline_at_ref(repo, base_ref, rel) if base_ref else None
        ) or zeros
        loosened = [
            f"{name}: {base[name]} → {pushed[name]}"
            for name in RATCHET_CHECKS
            if pushed[name] > base[name]
        ]
        if loosened:
            problems.append(
                "基準値を送り先より緩めているので push を止めます（"
                + remote_ref
                + "）："
                + "、".join(loosened)
                + "。基準値は締める方向（--update-baseline）だけ動かせます"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--remote", required=True)
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)
    try:
        problems = check(
            args.repo.resolve(),
            args.remote,
            sys.stdin.read().splitlines(),
            args.baseline,
        )
    except RatchetError as exc:
        problems = [f"基準値を読めないので push を止めます（{exc}）"]
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
