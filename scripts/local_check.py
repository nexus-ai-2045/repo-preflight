#!/usr/bin/env python3
"""手元で回す CI 相当の検査の入口。途中の作業は差分に絞り、PR 前とマージ前は全部回す。

モード:
- full（既定）: CI（ci.yml・documentation-contract.yml）と同じ項目を全部回す。PR を出す前とマージ前に使う。
- quick: 途中の作業（commit・push の前など）用。変えたファイルに関係する検査だけを回す。

どちらのモードでも、名義と個人情報の検査（identity_gate --public）は比較元から HEAD までの
全差分と、ステージした差分の全部を見る。ここは絞らない。CI の必須チェックの中身は変えない。

画面には要約（合否・件数・失敗した項目名）だけを出し、全文のログは .git/local-check/ に落とす。
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 変えたら全部の試験に戻すファイル（試験の土台・依存・設定）
FULL_TRIGGERS = (
    "pyproject.toml",
    "requirements-tools.txt",
    "requirements-guard.txt",
    "tests/conftest.py",
    "tests/synthetic_paths.py",
    "tests/identity_provider_fixture.py",
    "tests/path_input_fixture.py",
    "tests/github_settings_fixtures.py",
    ".repo-preflight-consistency.json",
)
FULL_TRIGGER_PREFIXES = (".github/", "references/", "schemas/", "templates/", "vendor/")
NARRATIVE_TEST = "tests/test_public_narrative_contract.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def changed_files(repo: Path, base: str) -> list[str]:
    """比較元との分岐点から作業ツリーまで（コミット済み・ステージ・未ステージ・未追跡）。"""
    merge_base = git(repo, "merge-base", base, "HEAD").strip()
    names = set(git(repo, "diff", "--name-only", merge_base).split())
    names |= set(git(repo, "ls-files", "--others", "--exclude-standard").split())
    return sorted(names)


def consumers_pattern(module: str, repo: Path) -> re.Pattern[str]:
    """変えたモジュールと、それを（間接に）使う scripts/ のモジュール名すべてに当たる正規表現。"""
    sources = {
        p.stem: p.read_text(encoding="utf-8") for p in (repo / "scripts").rglob("*.py")
    }
    names = {module}
    while True:
        pattern = re.compile(
            r"\b(?:" + "|".join(map(re.escape, sorted(names))) + r")\b"
        )
        grown = names | {stem for stem, text in sources.items() if pattern.search(text)}
        if grown == names:
            return pattern
        names = grown


def related_tests(repo: Path, files: list[str]) -> list[str] | None:
    """変えたファイルに関係する試験。None は「全部の試験に戻す」。"""
    tests: set[str] = set()
    all_tests = sorted(
        p.relative_to(repo).as_posix() for p in (repo / "tests").glob("test_*.py")
    )
    for name in files:
        if name in FULL_TRIGGERS or name.startswith(FULL_TRIGGER_PREFIXES):
            return None
        if name.startswith("tests/") and name.endswith(".py"):
            if Path(name).name.startswith("test_"):
                if (repo / name).exists():
                    tests.add(name)
                continue
            return None  # 試験の共有部品は、どの試験が使うか分からないので全部に戻す
        if name.startswith("scripts/") and name.endswith(".py"):
            pattern = consumers_pattern(Path(name).stem, repo)
            hits = [
                t
                for t in all_tests
                if pattern.search((repo / t).read_text(encoding="utf-8"))
            ]
            if not hits:
                return None  # 関係する試験が見つからない変更は、安全側で全部に戻す
            tests.update(hits)
        else:
            # 文書・データ：そのファイル名を見ている試験（digest や配布写しの一致など）も回す
            base_name = Path(name).name
            hits = [
                t
                for t in all_tests
                if base_name in (repo / t).read_text(encoding="utf-8")
            ]
            tests.update(hits)
            if (
                name.endswith(".md")
                or name.startswith(("runtime/", "assets/"))
                or name == "SKILL.md"
            ):
                tests.add(NARRATIVE_TEST)
            elif not hits:
                return None  # 何が見ているか決まらないファイルは、安全側で全部に戻す
    return sorted(tests)


def redact(text: str, repo: Path) -> str:
    """ログに実ホームパス・アカウント名・作業場所の絶対パスを残さない。"""
    pairs = [
        (sys.executable, "<python>"),
        (sys.prefix, "<python-prefix>"),
        (sys.base_prefix, "<python-base>"),
        (str(repo), "<repo>"),
        (str(Path.home()), "~"),
    ]
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - 取れない環境では置き換えない
        user = ""
    for old, new in pairs:
        if old and old not in ("/", "."):
            text = text.replace(old, new)
    if len(user) >= 3:
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(user)}(?![A-Za-z0-9_])", "<user>", text
        )
    return text


def build_steps(repo: Path, mode: str, base: str) -> list[tuple[str, list[str]]]:
    py = sys.executable
    merge_base = git(repo, "merge-base", base, "HEAD").strip()
    # 個人情報ゲート：どのモードでも全差分。絞らない
    identity = [
        py,
        "scripts/identity_gate.py",
        "--public",
        "--repo",
        ".",
        "--range",
        f"{merge_base}..HEAD",
    ]
    staged = git(repo, "diff", "--cached", "--name-only").strip()
    steps: list[tuple[str, list[str]]] = [("identity_gate（全差分）", identity)]
    if staged:
        steps.append(
            (
                "identity_gate（ステージ）",
                [py, "scripts/identity_gate.py", "--public", "--repo", ".", "--staged"],
            )
        )
    consistency = [
        py,
        "scripts/consistency_gate.py",
        "--repo",
        ".",
        "--base-ref",
        merge_base,
        "--require-config",
        "--require-mode",
        "enforce",
        "--json",
    ]
    readme = [py, "scripts/readme_release_gate.py", "--repo", ".", "--json"]
    if mode == "full":
        steps += [
            ("ai_ratchet_gate", [py, "-m", "ai_ratchet_gate", "--repo", "."]),
            ("black", [py, "-m", "black", "--check", "scripts/", "tests/"]),
            ("pytest（全部）", [py, "-m", "pytest", "-q", "-n", "auto"]),
            ("runtime_smoke", [py, "scripts/runtime_smoke.py", "--repo", "."]),
            ("readme_release_gate", readme),
            ("consistency_gate", consistency),
        ]
        return steps
    files = changed_files(repo, base)
    steps.append(("consistency_gate", consistency))  # 0.1 秒程度で、差分だけを見る
    if any(f == "README.md" or f == "scripts/readme_release_gate.py" for f in files):
        steps.append(("readme_release_gate", readme))
    py_files = [
        f
        for f in files
        if f.endswith(".py")
        and f.startswith(("scripts/", "tests/"))
        and (repo / f).exists()
    ]
    if py_files:
        steps.append(
            ("black（変えたファイル）", [py, "-m", "black", "--check", *py_files])
        )
    tests = related_tests(repo, files)
    if tests is None:
        steps.append(
            ("pytest（全部・安全側）", [py, "-m", "pytest", "-q", "-n", "auto"])
        )
    elif tests:
        steps.append(
            (
                f"pytest（関係 {len(tests)} ファイル）",
                [py, "-m", "pytest", "-q", "-n", "auto", *tests],
            )
        )
    if any(
        f.startswith(("runtime/", "assets/"))
        or f
        in ("SKILL.md", "scripts/runtime_smoke.py", "scripts/install_runtime_skills.py")
        for f in files
    ):
        steps.append(("runtime_smoke", [py, "scripts/runtime_smoke.py", "--repo", "."]))
    return steps


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--mode",
        choices=("full", "quick"),
        default="full",
        help="full（既定・PR 前とマージ前）／quick（途中の作業）",
    )
    ap.add_argument(
        "--base-ref", default="origin/main", help="比較元（既定 origin/main）"
    )
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--dry-run", action="store_true", help="回す項目だけを出す")
    ns = ap.parse_args(argv)
    repo = ns.repo.resolve()
    try:
        steps = build_steps(repo, ns.mode, ns.base_ref)
    except subprocess.CalledProcessError:
        print(
            f"失敗: 比較元 {ns.base_ref} が読めないので止めます（git fetch してください）"
        )
        return 2
    if ns.dry_run:
        for name, _ in steps:
            print(name)
        return 0
    log_dir = Path(git(repo, "rev-parse", "--git-dir").strip())
    if not log_dir.is_absolute():
        log_dir = repo / log_dir
    log_dir = log_dir / "local-check"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{ns.mode}.log"
    failed: list[str] = []
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        for name, cmd in steps:
            t0 = time.monotonic()
            # 実行したコマンドの絶対パス（Python の場所など）は書かず、出力も伏せてから残す
            done = subprocess.run(
                cmd,
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
            )
            rc = done.returncode
            log.write(f"===== {name}\n")
            log.write(redact(done.stdout, repo))
            mark = "緑" if rc == 0 else "赤"
            print(f"{mark} {name} {time.monotonic() - t0:.1f}秒")
            if rc != 0:
                failed.append(name)
    total = time.monotonic() - started
    # linked worktree では git-dir が repo の外にあるので相対パスで示す（実ホームパスを出さない）
    rel = Path(os.path.relpath(log_path, repo)).as_posix()
    status = "合格" if not failed else "不合格"
    print(
        f"{status}（{ns.mode}）: {len(steps) - len(failed)}/{len(steps)} 項目 {total:.1f}秒"
    )
    if failed:
        print("失敗した項目: " + "、".join(failed))
    print(f"全文のログ: {rel}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
