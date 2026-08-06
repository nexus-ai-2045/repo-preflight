"""Claude Code / Grok 向け skill pointer をホームへ配布する。

既定は dry-run。--apply で書き込む。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_MARKER = "<!-- repo-preflight:root -->"


def default_targets(home: Path) -> list[tuple[str, Path]]:
    targets = [
        ("claude-code", home / ".claude" / "skills" / "repo-preflight"),
        ("agents", home / ".agents" / "skills" / "repo-preflight"),
    ]
    grok_skills = home / ".grok" / "skills"
    # .grok/skills が既にある環境だけ配る（未使用ホームを増やさない）
    if grok_skills.is_dir() or (home / ".grok").is_dir():
        targets.append(("grok", grok_skills / "repo-preflight"))
    return targets


def adapter_source(repo: Path, runtime: str) -> Path:
    if runtime == "claude-code":
        return repo / "runtime" / "claude-code" / "SKILL.md"
    if runtime in {"grok", "agents"}:
        # agents home は Grok 系と共有されることが多い
        return repo / "runtime" / "grok" / "SKILL.md"
    raise ValueError(runtime)


def render_pointer(template: str, repo_root: Path) -> str:
    root = str(repo_root.resolve())
    lines = template.splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith("REPO_PREFLIGHT_ROOT="):
            out.append(f"REPO_PREFLIGHT_ROOT={root}")
        else:
            out.append(line)
    text = "\n".join(out) + "\n"
    if ROOT_MARKER in text and f"REPO_PREFLIGHT_ROOT={root}" not in text:
        text = text.replace(
            "REPO_PREFLIGHT_ROOT=\n",
            f"REPO_PREFLIGHT_ROOT={root}\n",
        )
    return text


def install_one(
    *,
    repo: Path,
    runtime: str,
    dest: Path,
    apply: bool,
) -> dict:
    source = adapter_source(repo, runtime)
    if not source.is_file():
        return {"runtime": runtime, "dest": str(dest), "status": "missing_adapter"}
    body = render_pointer(source.read_text(encoding="utf-8"), repo)
    action = {
        "runtime": runtime,
        "dest": str(dest),
        "source": str(source.relative_to(repo)),
        "status": "would_write" if not apply else "written",
    }
    if not apply:
        return action
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(body, encoding="utf-8")
    scan_path = repo.resolve() / "scripts" / "readiness_scan.py"
    # CLI への近道メモ (path の空白対策で引用符を付ける)
    (dest / "README.md").write_text(
        (
            f"# repo-preflight pointer\n\n"
            f"正本: `{repo.resolve()}`\n\n"
            f"```bash\n"
            f'python "{scan_path}" --repo "<TARGET>" --intent open_pr --human\n'
            f"# create_repo のときは --repo を付けない\n"
            f'python "{scan_path}" --intent create_repo --human\n'
            f"```\n"
        ),
        encoding="utf-8",
    )
    return action


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repo-preflight checkout",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実際にホーム skills へ書き込む (無い場合は dry-run)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.path.expanduser("~")),
        help="home directory override (tests)",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    if not (repo / "SKILL.md").is_file():
        print("error: SKILL.md not found in --repo", file=sys.stderr)
        return 2

    results = []
    for runtime, dest in default_targets(args.home.resolve()):
        results.append(
            install_one(repo=repo, runtime=runtime, dest=dest, apply=args.apply)
        )

    payload = {
        "schema": "repo-preflight.install-runtime-skills/v1",
        "apply": bool(args.apply),
        "repo": str(repo),
        "results": results,
        "next": (
            "re-run with --apply to write"
            if not args.apply
            else "run: python scripts/runtime_smoke.py --repo <repo-preflight>"
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if any(item.get("status") == "missing_adapter" for item in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
