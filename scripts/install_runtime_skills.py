"""Claude Code / Grok 向け skill pointer をホームへ配布する。

絶対 path を skill 本文に焼かない。
install 先に:
  - SKILL.md (portable 手順)
  - run_preflight.py (root 自動解決 launcher)
  - checkout/ (clone への symlink / junction)

既定は dry-run。--apply で書き込む。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def default_targets(home: Path) -> list[tuple[str, Path]]:
    targets = [
        ("claude-code", home / ".claude" / "skills" / "repo-preflight"),
        ("agents", home / ".agents" / "skills" / "repo-preflight"),
    ]
    grok_skills = home / ".grok" / "skills"
    if grok_skills.is_dir() or (home / ".grok").is_dir():
        targets.append(("grok", grok_skills / "repo-preflight"))
    return targets


def adapter_source(repo: Path, runtime: str) -> Path:
    if runtime == "claude-code":
        return repo / "runtime" / "claude-code" / "SKILL.md"
    if runtime in {"grok", "agents"}:
        return repo / "runtime" / "grok" / "SKILL.md"
    raise ValueError(runtime)


def link_checkout(dest_checkout: Path, repo: Path) -> str:
    """checkout を repo へリンク。成功した方式名を返す。"""
    if dest_checkout.exists() or dest_checkout.is_symlink():
        is_junc = bool(getattr(dest_checkout, "is_junction", lambda: False)())
        if dest_checkout.is_symlink() or is_junc:
            dest_checkout.unlink()
        elif dest_checkout.is_dir():
            # 古い実ディレクトリ / path-file fallback を消す
            shutil.rmtree(dest_checkout)
        else:
            dest_checkout.unlink()

    target = str(repo.resolve())
    link = str(dest_checkout)

    # 1) symlink
    try:
        dest_checkout.symlink_to(repo.resolve(), target_is_directory=True)
        return "symlink"
    except OSError:
        pass

    # 2) Windows junction (管理者不要なことが多い)
    if os.name == "nt":
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and dest_checkout.exists():
            return "junction"

    # 3) fallback: path ファイル
    dest_checkout.mkdir(parents=True, exist_ok=True)
    (dest_checkout / "ROOT_PATH.txt").write_text(target + "\n", encoding="utf-8")
    # resolve 側が checkout/scripts を見るので、最低限のリダイレクト用 run は skill 直下
    return "path-file"


def enhance_run_preflight_for_path_file(run_src: Path, dest: Path) -> None:
    """path-file fallback でも動くよう、run_preflight をそのままコピー。

    discover は checkout/ に readiness が無い場合 ROOT_PATH.txt を読むよう本体を拡張済みにする。
    """
    text = run_src.read_text(encoding="utf-8")
    (dest / "run_preflight.py").write_text(text, encoding="utf-8")


def install_one(
    *,
    repo: Path,
    runtime: str,
    dest: Path,
    apply: bool,
) -> dict:
    source = adapter_source(repo, runtime)
    run_src = repo / "runtime" / "shared" / "run_preflight.py"
    if not source.is_file():
        return {"runtime": runtime, "dest": str(dest), "status": "missing_adapter"}
    if not run_src.is_file():
        return {"runtime": runtime, "dest": str(dest), "status": "missing_adapter"}

    action = {
        "runtime": runtime,
        "dest": str(dest),
        "source": str(source.relative_to(repo)),
        "status": "would_write" if not apply else "written",
        "link": "pending",
    }
    if not apply:
        action["link"] = "would_link_checkout"
        return action

    dest.mkdir(parents=True, exist_ok=True)
    # 絶対 path を焼かない adapter 本文
    body = source.read_text(encoding="utf-8")
    # 旧 install の絶対 path 行を掃除
    cleaned_lines = []
    for line in body.splitlines():
        if (
            line.startswith("REPO_PREFLIGHT_ROOT=")
            and line.strip() != "REPO_PREFLIGHT_ROOT="
        ):
            cleaned_lines.append("REPO_PREFLIGHT_ROOT=")
        else:
            cleaned_lines.append(line)
    (dest / "SKILL.md").write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")

    enhance_run_preflight_for_path_file(run_src, dest)
    link_mode = link_checkout(dest / "checkout", repo)
    action["link"] = link_mode

    # README にも個人 path / 絶対 path を書かない
    (dest / "README.md").write_text(
        (
            "# repo-preflight skill (portable)\n\n"
            "絶対 path 固定ではありません。個人のホーム path も記録しません。\n\n"
            "解決順:\n"
            "1. 環境変数 `REPO_PREFLIGHT_ROOT`\n"
            "2. この skill 隣の `checkout/` (install が作る link)\n"
            "3. カレントから repo-preflight root を探索\n\n"
            "```bash\n"
            "python run_preflight.py --repo \"<TARGET>\" --intent open_pr --human\n"
            "python run_preflight.py --intent create_repo --human\n"
            "```\n"
            "\n"
            f"link mode: `{link_mode}`\n"
            "（clone の実 path は `checkout/` link を辿ること。本文には書かない）\n"
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
    if not (repo / "runtime" / "shared" / "run_preflight.py").is_file():
        print("error: runtime/shared/run_preflight.py missing", file=sys.stderr)
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
        "portable": True,
        "results": results,
        "next": (
            "re-run with --apply to write"
            if not args.apply
            else 'run: python "<skill>/run_preflight.py" --intent create_repo --human'
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if any(item.get("status") == "missing_adapter" for item in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
