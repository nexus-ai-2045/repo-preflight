"""repo-preflight を repo-preflight 自身へ全 intent で実行する保証テスト。

codex review (PR #16) 指摘の再発防止:
merge / publish / release などの文書化済みコマンドが、対象 repo としての
repo-preflight 自身に使えなくなる退行 (change_sensitive_scope_unavailable や
空 diff での history inventory 失敗) を検知する。

検査するのは scan 層が成立するかどうかだけ。blocked (人間確認待ち) は
設計どおりの正常系として扱い、tool_error だけを退行とみなす。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "readiness_scan.py"

DOCUMENTED_INTENTS = [
    ("push", ["--base-ref", "origin/main"]),
    ("open_pr", ["--base-ref", "origin/main"]),
    ("merge", ["--base-ref", "origin/main"]),
    ("publish", ["--audience", "public", "--consistency-base-ref", "origin/main"]),
    ("release", ["--consistency-base-ref", "origin/main"]),
]


def _origin_main_is_usable_base() -> bool:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
        cwd=REPO,
        capture_output=True,
    )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "refs/remotes/origin/main", "HEAD"],
        cwd=REPO,
        capture_output=True,
    )
    return probe.returncode == 0 and ancestor.returncode == 0


def _run_scan(intent: str, extra: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(REPO), "--intent", intent, *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(("intent", "extra"), DOCUMENTED_INTENTS)
def test_documented_intents_stay_usable_against_this_repository(intent, extra):
    if not _origin_main_is_usable_base():
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(
                "refs/remotes/origin/main が CI checkout に無い。"
                "fetch-depth: 0 が外れると本保証テストは実行できない"
            )
        pytest.skip("origin/main が無い、または HEAD の祖先ではない環境")
    report = _run_scan(intent, extra)
    scan = report["scan"]
    assert scan["status"] != "tool_error", scan.get("issues", scan)
    consistency = scan["checks"]["repository_consistency"]
    assert consistency["status"] == "pass", consistency
