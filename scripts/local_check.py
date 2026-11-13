#!/usr/bin/env python3
"""差分と依存周辺を選択して手元・CIで同じ試験を回す。未知の対応関係は、計画とCIでは停止し、互換実行では全部の試験を回す。

identity / consistency の必須ゲートは選択せず維持する。
--plan-tests は副作用のない汎用選択、--run-selected-tests は選択済みpytestだけの入口。
未解決の全件実行は --run-selected-tests だけが行う。full / quick は既存呼出し用の互換名。
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
from pathlib import Path
from typing import NamedTuple

_SELECTOR_SOURCE = Path(__file__).resolve().read_bytes()
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

_DEPENDENCIES_PATH = Path(__file__).resolve().with_name("local_check_dependencies.py")
_DEPENDENCIES_SOURCE = _DEPENDENCIES_PATH.read_bytes()
_DEPENDENCIES_KEY = (
    "_selection_dependencies_"
    + hashlib.sha256(
        str(_DEPENDENCIES_PATH).encode("utf-8") + b"\0" + _DEPENDENCIES_SOURCE
    ).hexdigest()
)
if _DEPENDENCIES_KEY not in sys.modules:
    _dependencies_spec = importlib.util.spec_from_file_location(
        _DEPENDENCIES_KEY, _DEPENDENCIES_PATH
    )
    dependencies = importlib.util.module_from_spec(_dependencies_spec)
    sys.modules[_DEPENDENCIES_KEY] = dependencies
    exec(
        compile(_DEPENDENCIES_SOURCE, str(_DEPENDENCIES_PATH), "exec"),
        dependencies.__dict__,
    )
else:
    dependencies = sys.modules[_DEPENDENCIES_KEY]


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


def _selection_config(repo: Path, config_path: Path | None = None) -> dict:
    path = config_path or Path(".test-selection.json")
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(repo.resolve())
        except ValueError as exc:
            raise ValueError(
                "selection config must belong to target repository"
            ) from exc
    safe = repo / dependencies.safe_relative(repo, path.as_posix())
    if config_path is None and not safe.exists():
        return {}
    config = json.loads(safe.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("selection config must be an object")
    return config


def _dependents(reverse, seed):
    found = {seed}
    pending = [seed]
    while pending:
        for consumer in reverse.get(pending.pop(), set()) - found:
            found.add(consumer)
            pending.append(consumer)
    return found


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
        dependencies.safe_relative(repo, root)
    if not policy["test_patterns"] or not policy["test_roots"]:
        raise ValueError("test roots and patterns must be nonempty")
    all_tests = sorted(
        {
            dependencies.safe_relative(repo, p.relative_to(repo).as_posix())
            for root in policy["test_roots"]
            for p in dependencies.root_files(repo, root)
            if p.is_file()
            and any(fnmatch.fnmatchcase(p.name, pat) for pat in policy["test_patterns"])
        }
    )
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
        if rule.get("dependency_mode", "auto") not in ("auto", "mapped", "supplement"):
            raise ValueError("rule dependency_mode must be auto, mapped or supplement")
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
            dependencies.safe_relative(repo, pattern)
    reverse, dependency_issues = dependencies.dependency_graph(
        repo, roots, files, policy["code_suffixes"]
    )
    reasons: dict[str, list[dict]] = {}
    file_reasons: dict[str, list[str]] = {}
    unresolved: list[dict] = []
    checks: set[str] = set()
    affected: dict[str, set[str]] = {}

    def add(test: str, name: str, reason: str):
        reasons.setdefault(test, []).append({"file": name, "reason": reason})
        file_reasons[name].append(reason)

    for raw_name in sorted(set(files)):
        name = dependencies.safe_relative(repo, raw_name)
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
            if "toml_only_keys" in rule:
                unresolved.append(
                    {
                        "file": name,
                        "reason": "TOML condition requires base-bound plan evaluation",
                    }
                )
                continue
            matched = True
            mode = rule.get("dependency_mode", "auto")
            if mode != "supplement":
                dependency_modes.append(mode)
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
        in_tests = any(
            dependencies.within_root(name, root) for root in policy["test_roots"]
        )
        deleted_test = (
            in_tests
            and name not in all_tests
            and any(
                fnmatch.fnmatchcase(Path(name).name, p) for p in policy["test_patterns"]
            )
        )
        if deleted_test:
            file_reasons[name].append("deleted test; inspect remaining consumers")
        if (
            Path(name).suffix in policy["code_suffixes"]
            and not any(dependencies.within_root(name, root) for root in roots)
            and not matched
        ):
            unresolved.append(
                {
                    "file": name,
                    "reason": "code outside owner-declared roots; explicit mapping required",
                }
            )
            continue
        affected[name] = _dependents(reverse, name)
        hits = sorted(set(all_tests) & affected[name])
        for test in hits:
            add(test, name, "Python syntax file dependency")
        for issue in dependency_issues:
            if issue not in unresolved:
                unresolved.append(issue)
        if not hits and not matched and name not in all_tests and not deleted_test:
            unresolved.append(
                {
                    "file": name,
                    "reason": "no dependency or explicit mapping; owner review required",
                }
            )
    # An unresolved incoming edge cannot enlarge a consumer already reached by
    # this change. Other opaque consumers remain unknown, even with no tests.
    covered = set()
    for closure in affected.values():
        covered.update(closure)
    # Manual tests are covered only when their known downstream tail is covered
    # too (a test module can itself be imported as a helper by another test).
    available = covered | set(reasons)
    covered.update(test for test in reasons if _dependents(reverse, test) <= available)
    remaining = []
    for issue in unresolved:
        if (
            issue in dependency_issues
            and issue["file"] in covered
            and "path escape" not in issue["reason"]
        ):
            for name in file_reasons:
                if issue["file"] in affected.get(name, set()) or any(
                    item["file"] == name for item in reasons.get(issue["file"], [])
                ):
                    evidence = (
                        "dependency uncertainty already covered: " + issue["file"]
                    )
                    if evidence not in file_reasons[name]:
                        file_reasons[name].append(evidence)
        else:
            remaining.append(issue)
    unresolved = remaining
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
        **({"policy_tests": all_tests} if unresolved else {}),
    }


def _toml_scoped_config(repo: Path, files: list[str], config: dict, base_oid: str):
    """Prove allowed TOML fields against Git base, without trusting policy flags."""
    effective = {**config, "rules": []}
    evidence = []
    rules = config.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("rules require objects")
        if "toml_only_keys" not in rule:
            effective["rules"].append(rule)
            continue
        keys = rule["toml_only_keys"]
        if (
            not isinstance(keys, list)
            or not keys
            or not all(
                isinstance(key, str)
                and all(part.isidentifier() for part in key.split("."))
                for key in keys
            )
        ):
            raise ValueError("toml_only_keys must contain dotted TOML keys")
        patterns = rule.get("changes")
        if (
            not isinstance(patterns, list)
            or not patterns
            or not all(isinstance(pattern, str) for pattern in patterns)
        ):
            raise ValueError("conditional rule changes must be nonempty strings")
        for pattern in patterns:
            dependencies.safe_relative(repo, pattern)
        matches = [
            name
            for name in files
            if any(
                fnmatch.fnmatchcase(name, pattern)
                for pattern in rule.get("changes", [])
            )
        ]
        verified = []
        for name in matches:
            name = dependencies.safe_relative(repo, name)
            record = {
                "file": name,
                "keys": keys,
                "base_oid": base_oid,
                "base_sha256": None,
                "current_sha256": None,
                "matched": False,
            }
            try:
                try:
                    import tomllib
                except ImportError:
                    import tomli as tomllib
                current_path = repo / name
                if current_path.is_symlink() or not current_path.is_file():
                    raise ValueError("conditional TOML requires a regular file")
                tree_entry = subprocess.run(
                    ["git", "ls-tree", base_oid, "--", name],
                    cwd=repo,
                    capture_output=True,
                    check=True,
                ).stdout
                base_mode = tree_entry.split(b" ", 1)[0]
                current_mode = (
                    b"100755" if current_path.stat().st_mode & 0o111 else b"100644"
                )
                if base_mode not in (b"100644", b"100755") or base_mode != current_mode:
                    raise ValueError("conditional TOML mode changed")
                before_bytes = subprocess.run(
                    ["git", "show", base_oid + ":" + name],
                    cwd=repo,
                    capture_output=True,
                    check=True,
                ).stdout
                after_bytes = (repo / name).read_bytes()
                record.update(
                    base_sha256=hashlib.sha256(before_bytes).hexdigest(),
                    current_sha256=hashlib.sha256(after_bytes).hexdigest(),
                )
                before = tomllib.loads(before_bytes.decode("utf-8"))
                after = tomllib.loads(after_bytes.decode("utf-8"))
                for document in (before, after):
                    for key in keys:
                        parts = key.split(".")
                        parent = document
                        for part in parts[:-1]:
                            parent = (
                                parent.get(part) if isinstance(parent, dict) else None
                            )
                        if isinstance(parent, dict):
                            parent.pop(parts[-1], None)

                def same(left, right):
                    if type(left) is not type(right):
                        return False
                    if isinstance(left, dict):
                        return left.keys() == right.keys() and all(
                            same(left[key], right[key]) for key in left
                        )
                    if isinstance(left, list):
                        return len(left) == len(right) and all(
                            same(a, b) for a, b in zip(left, right)
                        )
                    return left == right

                if same(before, after):
                    verified.append(name)
                    record["matched"] = True
            except (ImportError, ValueError, OSError, subprocess.CalledProcessError):
                pass
            evidence.append(record)
        if verified:
            effective["rules"].append(
                {
                    key: value
                    for key, value in {**rule, "changes": verified}.items()
                    if key != "toml_only_keys"
                }
            )
    return effective, evidence


def _selector_source_sha256() -> str:
    digest = hashlib.sha256()
    for name, source in (
        ("local_check.py", _SELECTOR_SOURCE),
        ("local_check_dependencies.py", _DEPENDENCIES_SOURCE),
    ):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(source).digest())
    return digest.hexdigest()


def plan_tests(repo: Path, base: str, config_path: Path | None = None) -> dict:
    config = _selection_config(repo, config_path)
    head_oid = git(repo, "rev-parse", "HEAD").strip()
    base_oid = git(repo, "merge-base", base, head_oid).strip()
    files = changed_files(repo, base_oid)
    effective_config, toml_evidence = _toml_scoped_config(repo, files, config, base_oid)
    plan = select_tests(repo, files, effective_config)
    if toml_evidence:
        for record in toml_evidence:
            if record["current_sha256"] is not None:
                current = repo / dependencies.safe_relative(repo, record["file"])
                if (
                    hashlib.sha256(current.read_bytes()).hexdigest()
                    != record["current_sha256"]
                ):
                    raise ValueError("TOML input changed during selection; retry")
        plan["toml_conditions"] = toml_evidence
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
        selector_source_sha256=_selector_source_sha256(),
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


class _Blocked(NamedTuple):
    reason: str
    selected_count: int
    unresolved_count: int
    check_count: int


class _Run(NamedTuple):
    tests: tuple[str, ...]
    workers: str | None
    fallback: bool


def _execution_decision(
    plan,
    *,
    ci=False,
    private=False,
    workers=None,
    allow_fallback=True,
    pytest_only=True,
) -> _Blocked | _Run:
    """Pure boundary: blocked selections never carry executable test paths."""
    counts = (
        len(set(plan["selected_tests"])),
        len(plan["unresolved"]),
        len(plan["checks"]),
    )
    if private:
        return _Blocked("private_selection", *counts)
    if pytest_only and plan["checks"]:
        return _Blocked("consumer_checks_required", *counts)
    unresolved = bool(plan["unresolved"]) or plan.get("status") == "unresolved"
    if unresolved and (ci or not allow_fallback):
        return _Blocked("unresolved_selection", *counts)
    tests = set(plan["selected_tests"])
    if unresolved:
        tests.update(plan.get("policy_tests", []))
        if not tests:
            return _Blocked("unresolved_selection", *counts)
    changed = set(plan.get("changed_files", []))
    return _Run(
        tuple(sorted(tests & changed) + sorted(tests - changed)),
        workers or ("auto" if ci or unresolved else None),
        unresolved,
    )


def _pytest_command(decision: _Run):
    if not isinstance(decision, _Run):
        raise TypeError("pytest requires a runnable selection")
    if not decision.tests:
        return []
    parallel = ["-n", decision.workers] if decision.workers else []
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-x",
        *parallel,
        "--",
        *decision.tests,
    ]


def _ci_report(plan, decision):
    """Only fixed vocabulary, counts and validated hashes may leave CI planning."""
    blocked = isinstance(decision, _Blocked)
    report = {
        "schema": "test-selection-ci/v1",
        "selection_status": (
            "unresolved" if blocked else ("selected" if decision.tests else "no_tests")
        ),
        "decision": "blocked" if blocked else ("run" if decision.tests else "skip"),
        "selected_count": len(set(plan["selected_tests"])),
        "unresolved_count": len(plan["unresolved"]),
        "check_count": len(plan["checks"]),
        "fallback": False,
    }
    if blocked:
        report["reason"] = decision.reason
    for key, width in (
        ("head_oid", 40),
        ("base_oid", 40),
        ("policy_sha256", 64),
        ("selector_source_sha256", 64),
    ):
        value = plan.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % width, value):
            report[key] = value
    return report


def _write_ci_summary(report):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as summary:
            summary.write("### Test selection\n\n| Field | Value |\n| --- | --- |\n")
            for key, value in report.items():
                rendered = str(value).lower() if isinstance(value, bool) else str(value)
                summary.write(f"| {key} | {rendered} |\n")
            summary.write("\n")


def _emit_ci_report(report, *, json_output):
    written = True
    try:
        _write_ci_summary(report)
    except OSError:
        report = {**report, "decision": "blocked", "reason": "summary_write_failed"}
        written = False
    print(json.dumps(report) if json_output else str(report), flush=True)
    return written


def _pytest_workers(value):
    if value == "auto" or (
        re.fullmatch(r"[1-9][0-9]{0,2}", value) and int(value) <= 256
    ):
        return value
    raise argparse.ArgumentTypeError("expected auto or a worker count from 1 to 256")


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
    plan = plan_tests(repo, base)
    files = plan["changed_files"]
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
    decision = _execution_decision(plan, allow_fallback=False, pytest_only=False)
    if isinstance(decision, _Blocked):
        raise ValueError("unresolved test selection")
    if decision.tests:
        steps.append(
            (
                f"pytest（関係 {len(decision.tests)} ファイル）",
                _pytest_command(decision),
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
    ap.add_argument(
        "--ci", action="store_true", help="CI: 未解決なら停止し、既定で自動並列実行"
    )
    ap.add_argument("--plan-tests", action="store_true", help="選択planだけを出す")
    ap.add_argument(
        "--run-selected-tests", action="store_true", help="選択したpytestだけを実行"
    )
    ap.add_argument(
        "--pytest-workers",
        type=_pytest_workers,
        help="pytest-xdist worker count (auto or 1-256)",
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
    if ns.ci:
        if ns.plan_tests or ns.dry_run or ns.identity_worktree:
            ap.error(
                "--ci conflicts with --plan-tests, --dry-run and --identity-worktree"
            )
        ns.run_selected_tests = True
    if ns.pytest_workers and not ns.run_selected_tests:
        ap.error("--pytest-workers requires --run-selected-tests")
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
            private_plan = bool(
                local_identity_guard.find_hits(serialized, identities)
                or redact(serialized, repo) != serialized
            )
            decision = _execution_decision(
                plan,
                ci=ns.ci,
                private=private_plan,
                workers=ns.pytest_workers,
                allow_fallback=ns.run_selected_tests,
            )
            if ns.ci:
                report = _ci_report(plan, decision)
                if not _emit_ci_report(report, json_output=ns.json):
                    return 2
            else:
                if private_plan:
                    # Redacted paths must never become executable selection inputs.
                    plan = {
                        "schema": "test-selection/v1",
                        "status": "unresolved",
                        "selected_tests": [],
                        "checks": [],
                        "unresolved": [
                            {
                                "reason": "private identity in plan; owner review required"
                            }
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
            if isinstance(decision, _Blocked):
                if (
                    not ns.ci
                    and decision.reason == "consumer_checks_required"
                    and ns.run_selected_tests
                ):
                    print(
                        "停止: check IDs require the consumer runner", file=sys.stderr
                    )
                # A plan-only consumer owns check IDs; they do not block reading its plan.
                if ns.run_selected_tests or private_plan or plan["unresolved"]:
                    return 2
                return 0
            if ns.run_selected_tests:
                command = _pytest_command(decision)
                if decision.fallback:
                    print(
                        "対応が分からない変更があるので、全部の試験を回します",
                        file=sys.stderr,
                    )
                if command:
                    return subprocess.run(
                        command,
                        cwd=repo,
                        stdout=sys.stderr if ns.json or ns.ci else None,
                    ).returncode
            return 0
        if ns.selection_config:
            ap.error("--selection-config requires --plan-tests or --run-selected-tests")
        steps = build_steps(repo, ns.mode, ns.base_ref)
    except (ValueError, OSError) as exc:
        if ns.ci:
            _emit_ci_report(
                {
                    "schema": "test-selection-ci/v1",
                    "decision": "blocked",
                    "reason": "selection_failed",
                    "fallback": False,
                },
                json_output=ns.json,
            )
            return 2
        safe_error = local_identity_guard.redact(redact(str(exc), repo), identities)
        print(
            json.dumps({"status": "unresolved", "error": safe_error})
            if ns.json
            else f"停止: {safe_error}"
        )
        return 2
    except subprocess.CalledProcessError:
        if ns.ci:
            _emit_ci_report(
                {
                    "schema": "test-selection-ci/v1",
                    "decision": "blocked",
                    "reason": "comparison_failed",
                    "fallback": False,
                },
                json_output=ns.json,
            )
            return 2
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
    print("CI の結果は別に確認してください（PR 本文・全 ref の走査を含む）")
    print(f"全文のログ: {rel}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
