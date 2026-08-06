"""Claude Code / Grok / CLI 向けの最小保証 smoke。

依存ゼロ。exit 0 ならこのマシンで CLI 契約と skill 入口が揃っている。
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "scripts" / "readiness_scan.py"
REQUIRED_ADAPTERS = (
    ROOT / "SKILL.md",
    ROOT / "runtime" / "claude-code" / "SKILL.md",
    ROOT / "runtime" / "grok" / "SKILL.md",
    ROOT / "runtime" / "agents" / "openai.yaml",
    ROOT / "docs" / "runtime-support.md",
)
TRIGGER_TOKENS = (
    "PR",
    "push",
    "公開",
    "preflight",
    "create",
    "intent",
)


def run_scan(*args: str) -> tuple[int, dict | None, str]:
    result = subprocess.run(
        [sys.executable, str(SCAN), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
    )
    stdout = result.stdout.strip()
    report = None
    if stdout:
        try:
            report = json.loads(stdout)
        except json.JSONDecodeError:
            report = None
    return result.returncode, report, result.stderr


def check_skill_file(path: Path, *, rel: str) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"missing:{rel}"]
    text = path.read_text(encoding="utf-8")
    if path.name == "SKILL.md":
        if not re.search(r"(?m)^name:\s*repo-preflight\s*$", text):
            errors.append(f"skill_name_missing:{rel}")
        if "description:" not in text:
            errors.append(f"description_missing:{rel}")
        missing = [
            token
            for token in TRIGGER_TOKENS
            if token not in text and token.lower() not in text.lower()
        ]
        if len(missing) > 3:
            errors.append(f"trigger_coverage_low:{rel}:{','.join(missing)}")
    return errors


def make_min_repo(base: Path) -> Path:
    repo = base / "sample"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Smoke Author"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "smoke@example.invalid"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    for name in (
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "PREFLIGHT.md",
    ):
        body = f"# {name}\n"
        if name == "PREFLIGHT.md":
            body = "<!-- repo-preflight:review-record -->\n" + body
        (repo / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "smoke"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def main() -> int:
    parser = argparse.ArgumentParser(description="repo-preflight runtime smoke")
    parser.add_argument(
        "--repo",
        type=Path,
        default=ROOT,
        help="repo-preflight root (default: this checkout)",
    )
    args = parser.parse_args()
    root = args.repo.resolve()
    errors: list[str] = []
    notes: list[str] = []

    notes.append(f"platform={platform.system()} {platform.release()}")
    notes.append(f"python={sys.version.split()[0]}")
    notes.append(f"root={root}")

    if not (root / "scripts" / "readiness_scan.py").is_file():
        errors.append("readiness_scan_missing")

    for path in REQUIRED_ADAPTERS:
        rel = path.relative_to(ROOT).as_posix()
        candidate = root / path.relative_to(ROOT)
        if candidate.suffix == ".md":
            errors.extend(check_skill_file(candidate, rel=rel))
        elif not candidate.is_file():
            errors.append(f"missing:{rel}")

    # create_repo dialogue (no target repo required)
    code, report, _ = run_scan("--intent", "create_repo")
    if report is None:
        errors.append("create_repo_not_json")
    else:
        if report.get("schema") != "repo-preflight.dialogue/v3":
            errors.append("create_repo_schema")
        if report.get("intent") != "create_repo":
            errors.append("create_repo_intent")
        if "guarantees" not in report or "non_guarantees" not in report:
            errors.append("create_repo_boundaries")
        if not report.get("proposals"):
            errors.append("create_repo_proposals_empty")
        notes.append(f"create_repo_status={report.get('status')} exit={code}")

    with tempfile.TemporaryDirectory() as tmp:
        sample = make_min_repo(Path(tmp))
        code, report, _ = run_scan(
            "--repo", str(sample), "--intent", "open_pr", "--human"
        )
        if report is None:
            errors.append("open_pr_not_json")
        else:
            if report.get("schema") != "repo-preflight.dialogue/v3":
                errors.append("open_pr_schema")
            if report.get("scan") is None:
                errors.append("open_pr_missing_scan")
            notes.append(f"open_pr_status={report.get('status')} exit={code}")

        code, report, _ = run_scan("--repo", str(sample))
        if report is None or report.get("schema") != "repo-preflight.scan/v3":
            errors.append("scan_schema")
        else:
            notes.append(f"scan_status={report.get('status')} exit={code}")

    payload = {
        "schema": "repo-preflight.runtime-smoke/v1",
        "status": "pass" if not errors else "fail",
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "notes": notes,
        "errors": errors,
        "supported_runtimes": ["cli", "claude-code", "grok", "codex"],
        "guarantee": (
            "CLI dialogue/scan contracts and skill adapter files exist on this machine"
        ),
        "non_guarantee": (
            "Model will always load the skill; product auto-install; remote sandbox without git"
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
