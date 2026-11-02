#!/usr/bin/env python3
"""差分と依存周辺を選択して手元・CIで同じ試験を回す。未知の対応関係は停止する。

identity / consistency の必須ゲートは選択せず維持する。
--plan-tests は副作用のない汎用選択、--run-selected-tests は選択済みpytestだけの入口。
全件試験への暗黙のfallbackはない。full / quick は既存呼出し用の互換名。
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

_IDENTITY_PATH = Path(__file__).resolve().with_name("local_identity_guard.py")
_IDENTITY_KEY = (
    "_selection_identity_" + hashlib.sha256(_IDENTITY_PATH.read_bytes()).hexdigest()
)
if _IDENTITY_KEY not in sys.modules:
    _identity_spec = importlib.util.spec_from_file_location(
        _IDENTITY_KEY, _IDENTITY_PATH
    )
    local_identity_guard = importlib.util.module_from_spec(_identity_spec)
    sys.modules[_IDENTITY_KEY] = local_identity_guard
    _identity_spec.loader.exec_module(local_identity_guard)
else:
    local_identity_guard = sys.modules[_IDENTITY_KEY]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION = {
    "source_roots": ["scripts"],
    "test_roots": ["tests"],
    "test_patterns": ["test_*.py"],
    "code_suffixes": [".py"],
    "rules": [],
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def changed_files(repo: Path, base: str) -> list[str]:
    """比較元との分岐点から作業ツリーまで（コミット済み・ステージ・未ステージ・未追跡）。"""
    merge_base = git(repo, "merge-base", base, "HEAD").strip()
    names = set(
        git(repo, "diff", "--no-renames", "--name-only", "-z", merge_base).split("\0")
    )
    names |= set(
        git(repo, "ls-files", "-z", "--others", "--exclude-standard").split("\0")
    )
    names.discard("")
    return sorted(names)


def _safe_relative(repo: Path, name: str) -> str:
    """Policy and changed paths never escape the target repository (including symlinks)."""
    path = Path(name)
    if (
        not name
        or path.is_absolute()
        or PureWindowsPath(name).drive
        or ".." in path.parts
        or "\\" in name
    ):
        raise ValueError("selection paths must be repository-relative")
    try:
        (repo / path).resolve().relative_to(repo.resolve())
    except ValueError as exc:
        raise ValueError("selection path escapes repository") from exc
    return path.as_posix()


def _selection_config(repo: Path, config_path: Path | None = None) -> dict:
    path = config_path or Path(".test-selection.json")
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(repo.resolve())
        except ValueError as exc:
            raise ValueError(
                "selection config must belong to target repository"
            ) from exc
    safe = repo / _safe_relative(repo, path.as_posix())
    if config_path is None and not safe.exists():
        return {}
    config = json.loads(safe.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("selection config must be an object")
    return config


def _root_files(repo: Path, root: str):
    omitted = {
        ".git",
        ".local",
        ".worktrees",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
    }
    for current, directories, files in os.walk(repo / root):
        directories[:] = sorted(name for name in directories if name not in omitted)
        for name in sorted(files):
            yield Path(current) / name


def _within_root(name: str, root: str) -> bool:
    prefix = Path(root).as_posix().rstrip("/")
    return prefix == "." or name.startswith(prefix + "/")


def _module_texts(
    repo: Path, roots: list[str], suffixes: list[str]
) -> list[tuple[str, str]]:
    return [
        (p.stem, p.read_text(encoding="utf-8", errors="replace"))
        for root in roots
        for p in _root_files(repo, root)
        if p.is_file()
        and p.suffix in suffixes
        and _safe_relative(repo, p.relative_to(repo).as_posix())
    ]


def consumers_pattern(
    module: str,
    repo: Path,
    source_roots: list[str] | None = None,
    code_suffixes: list[str] | None = None,
    module_texts: list[tuple[str, str]] | None = None,
) -> re.Pattern[str]:
    """Existing name-reference reverse dependency closure, configurable by the owner."""
    sources = (
        module_texts
        if module_texts is not None
        else _module_texts(repo, source_roots or ["scripts"], code_suffixes or [".py"])
    )
    names = {module}
    while True:
        pattern = re.compile(
            r"\b(?:" + "|".join(map(re.escape, sorted(names))) + r")\b"
        )
        grown = names | {stem for stem, text in sources if pattern.search(text)}
        if grown == names:
            return pattern
        names = grown


def select_tests(repo: Path, files: list[str], config: dict) -> dict:
    """Return paths, per-file evidence and unresolved mappings; never execute policy text."""
    if "schema" in config and config["schema"] != "test-selection-policy/v1":
        raise ValueError("unknown selection policy schema")
    policy = {**DEFAULT_SELECTION, **config}
    for key in ("source_roots", "test_roots", "test_patterns", "code_suffixes"):
        if not isinstance(policy[key], list) or not all(
            isinstance(x, str) for x in policy[key]
        ):
            raise ValueError(f"{key} must be a list of strings")
    roots = policy["source_roots"] + policy["test_roots"]
    for root in roots:
        _safe_relative(repo, root)
    if not policy["test_patterns"] or not policy["test_roots"]:
        raise ValueError("test roots and patterns must be nonempty")
    all_tests = sorted(
        {
            _safe_relative(repo, p.relative_to(repo).as_posix())
            for root in policy["test_roots"]
            for p in _root_files(repo, root)
            if p.is_file()
            and any(fnmatch.fnmatchcase(p.name, pat) for pat in policy["test_patterns"])
        }
    )
    texts = {
        t: (repo / t).read_text(encoding="utf-8", errors="replace") for t in all_tests
    }
    rules = policy["rules"]
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("changes"):
            raise ValueError("rules require changes")
        for key in ("changes", "tests", "checks"):
            values = rule.get(key, [])
            if not isinstance(values, list) or not all(
                isinstance(x, str) for x in values
            ):
                raise ValueError(f"rule {key} must be a list of strings")
        if rule.get("dependency_mode", "auto") not in ("auto", "mapped"):
            raise ValueError("rule dependency_mode must be auto or mapped")
        if "no_tests" in rule and not isinstance(rule["no_tests"], bool):
            raise ValueError("rule no_tests must be a boolean")
        if any(
            not value.strip() or value != value.strip()
            for value in rule.get("checks", [])
        ):
            raise ValueError(
                "rule check IDs must be nonempty without boundary whitespace"
            )
        for pattern in rule["changes"] + rule.get("tests", []):
            _safe_relative(repo, pattern)
    modules = _module_texts(repo, roots, policy["code_suffixes"])
    reasons: dict[str, list[dict]] = {}
    file_reasons: dict[str, list[str]] = {}
    unresolved: list[dict] = []
    checks: set[str] = set()

    def add(test: str, name: str, reason: str):
        reasons.setdefault(test, []).append({"file": name, "reason": reason})
        file_reasons[name].append(reason)

    for raw_name in sorted(set(files)):
        name = _safe_relative(repo, raw_name)
        file_reasons[name] = []
        if Path(name).name == "conftest.py":
            unresolved.append(
                {
                    "file": name,
                    "reason": "fixture-impact: implicit pytest consumers require owner scope review",
                }
            )
            continue
        matched = False
        dependency_modes: list[str] = []
        unresolved_before_rules = len(unresolved)
        for index, rule in enumerate(rules):
            if not any(
                fnmatch.fnmatchcase(name, pattern) for pattern in rule["changes"]
            ):
                continue
            matched = True
            dependency_modes.append(rule.get("dependency_mode", "auto"))
            checks.update(rule.get("checks", []))
            hits = [
                t
                for t in all_tests
                if any(
                    fnmatch.fnmatchcase(t, pattern) for pattern in rule.get("tests", [])
                )
            ]
            for test in hits:
                add(test, name, f"policy rule {index}")
            missing_patterns = [
                pat
                for pat in rule.get("tests", [])
                if not any(fnmatch.fnmatchcase(t, pat) for t in all_tests)
            ]
            if missing_patterns:
                unresolved.append(
                    {
                        "file": name,
                        "reason": f"policy rule {index} matched no tests for: "
                        + ", ".join(missing_patterns),
                    }
                )
            elif not hits and not rule.get("checks") and not rule.get("no_tests"):
                unresolved.append(
                    {"file": name, "reason": f"policy rule {index} has no disposition"}
                )
            else:
                file_reasons[name].append(f"policy rule {index}")
        if name in all_tests:
            add(name, name, "changed test")
        if (
            dependency_modes
            and all(mode == "mapped" for mode in dependency_modes)
            and len(unresolved) == unresolved_before_rules
        ):
            file_reasons[name].append(
                "owner mapped dependency scope; automatic closure omitted"
            )
            continue
        in_tests = any(_within_root(name, root) for root in policy["test_roots"])
        deleted_test = (
            in_tests
            and name not in all_tests
            and any(
                fnmatch.fnmatchcase(Path(name).name, p) for p in policy["test_patterns"]
            )
        )
        if deleted_test:
            file_reasons[name].append("deleted test; inspect remaining consumers")
        code = Path(name).suffix in policy["code_suffixes"]
        in_sources = any(_within_root(name, root) for root in policy["source_roots"])
        if code and (in_sources or in_tests):
            pattern = consumers_pattern(
                Path(name).stem, repo, roots, policy["code_suffixes"], modules
            )
            hits = [t for t, body in texts.items() if pattern.search(body)]
            for test in hits:
                add(test, name, "reverse dependency or name reference")
        else:
            direct = {t for t, body in texts.items() if Path(name).name in body}
            seeds = {stem for stem, body in modules if Path(name).name in body}
            hits = set(direct)
            for seed in sorted(seeds):
                pattern = consumers_pattern(
                    seed, repo, roots, policy["code_suffixes"], modules
                )
                hits.update(t for t, body in texts.items() if pattern.search(body))
            for test in sorted(hits):
                reason = (
                    "file name reference"
                    if test in direct
                    else "file name reference through reverse dependency"
                )
                add(test, name, reason)
        if not hits and not matched and name not in all_tests and not deleted_test:
            unresolved.append(
                {
                    "file": name,
                    "reason": "no dependency or explicit mapping; owner review required",
                }
            )
    return {
        "schema": "test-selection/v1",
        "status": (
            "unresolved" if unresolved else ("selected" if reasons else "no_tests")
        ),
        "changed_files": sorted(file_reasons),
        "selected_tests": sorted(reasons),
        "reasons": reasons,
        "file_reasons": file_reasons,
        "checks": sorted(checks),
        "unresolved": unresolved,
    }


def plan_tests(repo: Path, base: str, config_path: Path | None = None) -> dict:
    config = _selection_config(repo, config_path)
    head_oid = git(repo, "rev-parse", "HEAD").strip()
    base_oid = git(repo, "merge-base", base, head_oid).strip()
    files = changed_files(repo, base_oid)
    plan = select_tests(repo, files, config)
    if git(repo, "rev-parse", "HEAD").strip() != head_oid:
        raise ValueError("HEAD changed during selection; retry")
    policy_bytes = json.dumps(
        {**DEFAULT_SELECTION, **config}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    plan.update(
        head_oid=head_oid,
        base_oid=base_oid,
        base_kind="merge-base",
        policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        selector_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    return plan


def related_tests(repo: Path, files: list[str]) -> list[str]:
    """Compatibility API: unknown mappings fail closed instead of returning full-suite sentinel."""
    plan = select_tests(repo, files, _selection_config(repo))
    if plan["unresolved"]:
        raise ValueError(
            "unresolved test selection: "
            + ", ".join(x["file"] for x in plan["unresolved"])
        )
    return plan["selected_tests"]


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


def worktree_dirty(repo: Path) -> bool:
    """まだステージしていない変更か、未追跡のファイルがあるか。"""
    unstaged = git(repo, "diff", "--name-only", "-z")
    untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard")
    return bool(unstaged.strip("\0") or untracked.strip("\0"))


@contextmanager
def worktree_index_env(repo: Path):
    """作業ツリーの全部（未ステージ・未追跡を含む）を載せた一時の索引を作り、それを指す環境変数を返す。

    本物の索引（ステージの状態）は変えない。identity_gate の --staged は GIT_INDEX_FILE を
    そのまま使うので、この索引を渡すと「HEAD から作業ツリーまで」の足した行とファイル名を見る。
    """
    real = Path(git(repo, "rev-parse", "--git-path", "index").strip())
    if not real.is_absolute():
        real = repo / real
    with tempfile.TemporaryDirectory(prefix="local-check-") as tmp:
        index = Path(tmp) / "index"
        if real.exists():
            # copy2 で時刻も写す。copyfile だと索引の時刻が「今」になり、git の racy 判定
            # （索引と同じ時刻に書き換えたファイルは中身を読み直す）が効かず、同じ大きさで
            # 書き換えたファイルを「変更なし」と見落とす
            shutil.copy2(real, index)
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        subprocess.run(
            ["git", "add", "-A"], cwd=repo, env=env, check=True, capture_output=True
        )
        yield env


def identity_worktree(repo: Path) -> int:
    """作業ツリーの全部を identity_gate --public --staged で見る（--identity-worktree の中身）。"""
    with worktree_index_env(repo) as env:
        return subprocess.run(
            [
                sys.executable,
                "scripts/identity_gate.py",
                "--public",
                "--repo",
                ".",
                "--staged",
            ],
            cwd=repo,
            env=env,
        ).returncode


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
    if worktree_dirty(repo):
        # 未ステージと未追跡も素通りさせない（一時の索引に載せて見る。本物の索引は変えない）
        steps.append(
            (
                "identity_gate（作業ツリー）",
                [
                    py,
                    "scripts/local_check.py",
                    "--repo",
                    ".",
                    "--identity-worktree",
                ],
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
    if mode != "quick":
        steps += [
            ("ai_ratchet_gate", [py, "-m", "ai_ratchet_gate", "--repo", "."]),
            ("runtime_smoke", [py, "scripts/runtime_smoke.py", "--repo", "."]),
            ("readme_release_gate", readme),
        ]
    files = changed_files(repo, base)
    steps.append(("consistency_gate", consistency))  # 0.1 秒程度で、差分だけを見る
    if mode == "quick" and any(
        f == "README.md" or f == "scripts/readme_release_gate.py" for f in files
    ):
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
    if tests:
        steps.append(
            (
                f"pytest（関係 {len(tests)} ファイル）",
                [py, "-m", "pytest", "-q", "--", *tests],
            )
        )
    if mode == "quick" and any(
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
        choices=("selected", "full", "quick"),
        default="selected",
        help="selected（既定）。full / quick は互換名で、試験はすべて同じ差分選択を使う",
    )
    ap.add_argument(
        "--base-ref", default="origin/main", help="比較元（既定 origin/main）"
    )
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--plan-tests", action="store_true", help="選択planだけを出す")
    ap.add_argument(
        "--run-selected-tests", action="store_true", help="選択したpytestだけを実行"
    )
    ap.add_argument("--selection-config", type=Path, help="対象repo内の選択policy")
    ap.add_argument("--json", action="store_true", help="planをJSONで出す")
    ap.add_argument("--dry-run", action="store_true", help="回す項目だけを出す")
    ap.add_argument(
        "--identity-worktree",
        action="store_true",
        help="内部用: 未ステージ・未追跡を含む作業ツリーの全部を identity_gate で見る",
    )
    ns = ap.parse_args(argv)
    repo = ns.repo.resolve()
    identities = local_identity_guard.collect_identities(
        repo=repo, public_names={"nexus-ai-2045", "nexus_ai"}
    )
    if ns.identity_worktree:
        return identity_worktree(repo)
    try:
        if ns.plan_tests or ns.run_selected_tests:
            plan = plan_tests(repo, ns.base_ref, ns.selection_config)
            serialized = json.dumps(plan, ensure_ascii=False, indent=2)
            if (
                local_identity_guard.find_hits(serialized, identities)
                or redact(serialized, repo) != serialized
            ):
                # Never hand a consumer redacted paths as executable selection.
                plan = {
                    "schema": "test-selection/v1",
                    "status": "unresolved",
                    "selected_tests": [],
                    "checks": [],
                    "unresolved": [
                        {"reason": "private identity in plan; owner review required"}
                    ],
                }
            print(
                (
                    json.dumps(plan, ensure_ascii=False, indent=2)
                    if ns.json
                    else str(plan)
                ),
                flush=True,
            )
            if plan["unresolved"]:
                return 2
            if ns.run_selected_tests and plan["checks"]:
                print("停止: check IDs require the consumer runner", file=sys.stderr)
                return 2
            if ns.run_selected_tests and plan["selected_tests"]:
                return subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "--",
                        *plan["selected_tests"],
                    ],
                    cwd=repo,
                    stdout=sys.stderr if ns.json else None,
                ).returncode
            return 0
        if ns.selection_config:
            ap.error("--selection-config requires --plan-tests or --run-selected-tests")
        steps = build_steps(repo, ns.mode, ns.base_ref)
    except (ValueError, OSError) as exc:
        safe_error = local_identity_guard.redact(redact(str(exc), repo), identities)
        print(
            json.dumps({"status": "unresolved", "error": safe_error})
            if ns.json
            else f"停止: {safe_error}"
        )
        return 2
    except subprocess.CalledProcessError:
        message = (
            "比較元が読めないので停止します（git fetch と比較元を確認してください）"
        )
        print(
            json.dumps({"status": "unresolved", "error": message})
            if ns.json
            else message
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
            log.write(
                local_identity_guard.redact(redact(done.stdout, repo), identities)
            )
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
    print("CI の結果は別に確認してください（PR 本文・base_guard・全 ref の走査を含む）")
    print(f"全文のログ: {rel}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
