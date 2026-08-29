"""Claude Code / Grok 向け skill pointer をホームへ配布する。

絶対 path を skill 本文に焼かない。
install 先に:
  - SKILL.md (portable 手順)
  - run_preflight.py (root 自動解決 launcher)
  - checkout/ (clone への symlink / junction)

既定は dry-run。--apply で書き込む。
--check は install 済みコピーが正本から drift していないかを read-only で検査する。
"""

from __future__ import annotations

import argparse
import hashlib
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


def project_skill_body(body: str) -> str:
    """install 先へ書く SKILL.md 本文を作る。

    絶対 path を焼かないため `REPO_PREFLIGHT_ROOT=` 行の値を落とす。
    check 側も必ずこの射影を通してから比較すること (通さないと必ず drift 誤検知する)。
    """
    cleaned_lines = []
    for line in body.splitlines():
        if (
            line.startswith("REPO_PREFLIGHT_ROOT=")
            and line.strip() != "REPO_PREFLIGHT_ROOT="
        ):
            cleaned_lines.append("REPO_PREFLIGHT_ROOT=")
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines) + "\n"


def render_readme(link_mode: str) -> str:
    """install 先 README.md 本文。個人 path / 絶対 path を書かない。"""
    return (
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
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# install が実際に作りうる link mode。detect_link_mode はこれ以外に
# "missing" / "unknown" も返すが、それは install の産物ではなく異常側
INSTALLABLE_LINK_MODES = ("symlink", "junction", "path-file")


def _read_installed(path: Path) -> str | None:
    """install 済み file を読む。読めなければ None を返す。

    ここで例外を上げると 1 file の破損で run 全体が traceback になり、
    残りの target が未検査のまま JSON も出ない。read-only の検査が
    落ちる理由にはしない (2026-08-29 review)。
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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
    # 絶対 path を焼かない adapter 本文 (check 側と同じ射影を通す)
    body = source.read_text(encoding="utf-8")
    (dest / "SKILL.md").write_text(project_skill_body(body), encoding="utf-8")

    enhance_run_preflight_for_path_file(run_src, dest)
    link_mode = link_checkout(dest / "checkout", repo)
    action["link"] = link_mode

    # README にも個人 path / 絶対 path を書かない
    (dest / "README.md").write_text(render_readme(link_mode), encoding="utf-8")
    return action


def detect_link_mode(dest_checkout: Path, repo: Path) -> tuple[str, str | None]:
    """install 済み checkout/ の方式と異常を返す。書き込みはしない。"""
    if not dest_checkout.exists() and not dest_checkout.is_symlink():
        return "missing", "checkout_missing"

    want = repo.resolve()

    if dest_checkout.is_symlink():
        if not dest_checkout.exists():
            return "symlink", "checkout_dangling"
        if dest_checkout.resolve() != want:
            return "symlink", "checkout_foreign"
        return "symlink", None

    if dest_checkout.is_dir():
        root_file = dest_checkout / "ROOT_PATH.txt"
        if root_file.is_file():
            raw = _read_installed(root_file)
            if raw is None:
                return "path-file", "checkout_foreign"
            recorded = raw.strip()
            if not recorded:
                return "path-file", "checkout_foreign"
            if Path(recorded).resolve() != want:
                return "path-file", "checkout_foreign"
            return "path-file", None
        # junction / 実ディレクトリ
        if dest_checkout.resolve() != want:
            return "junction", "checkout_foreign"
        return "junction", None

    return "unknown", "checkout_unexpected"


def check_one(*, repo: Path, runtime: str, dest: Path) -> dict:
    """install 済みコピーが repo 正本から drift していないか read-only で検査する。

    書き込み・削除は一切しない。比較は sha256。標準ライブラリのみ。
    """
    source = adapter_source(repo, runtime)
    run_src = repo / "runtime" / "shared" / "run_preflight.py"
    result: dict = {"runtime": runtime, "dest": str(dest)}

    if not source.is_file() or not run_src.is_file():
        result["status"] = "missing_adapter"
        return result

    if not dest.is_dir():
        result["status"] = "not_installed"
        return result

    findings: list[str] = []

    expected_skill = _digest(project_skill_body(source.read_text(encoding="utf-8")))
    dest_skill = dest / "SKILL.md"
    if not dest_skill.is_file():
        findings.append("skill_md_missing")
    else:
        body = _read_installed(dest_skill)
        if body is None:
            findings.append("skill_md_unreadable")
        elif _digest(body) != expected_skill:
            findings.append("skill_md_drift")

    expected_run = _digest(run_src.read_text(encoding="utf-8"))
    dest_run = dest / "run_preflight.py"
    if not dest_run.is_file():
        findings.append("run_preflight_missing")
    else:
        body = _read_installed(dest_run)
        if body is None:
            findings.append("run_preflight_unreadable")
        elif _digest(body) != expected_run:
            findings.append("run_preflight_drift")

    link_mode, link_finding = detect_link_mode(dest / "checkout", repo)
    result["link"] = link_mode
    if link_finding:
        findings.append(link_finding)

    dest_readme = dest / "README.md"
    if not dest_readme.is_file():
        findings.append("readme_missing")
    else:
        body = _read_installed(dest_readme)
        if body is None:
            findings.append("readme_unreadable")
        else:
            # README は install 時の link mode を本文に含むが、その mode は
            # どこにも記録されていない。今の checkout から検出した mode で
            # 再生成すると、checkout を壊しただけで README が drift 判定になり、
            # checkout を消すと README が一切検査されなくなる。
            # install が作りうる mode のどれかと一致すれば ok とする
            expected_readmes = {
                _digest(render_readme(mode)) for mode in INSTALLABLE_LINK_MODES
            }
            if _digest(body) not in expected_readmes:
                findings.append("readme_drift")

    result["status"] = "drift" if findings else "ok"
    result["findings"] = findings
    return result


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
        "--check",
        action="store_true",
        help="install 済みコピーが正本から drift していないか read-only で検査する",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.path.expanduser("~")),
        help="home directory override (tests)",
    )
    args = parser.parse_args()
    if args.check and args.apply:
        print("error: --check and --apply are mutually exclusive", file=sys.stderr)
        return 2
    repo = args.repo.resolve()
    if not (repo / "SKILL.md").is_file():
        print("error: SKILL.md not found in --repo", file=sys.stderr)
        return 2
    if not (repo / "runtime" / "shared" / "run_preflight.py").is_file():
        print("error: runtime/shared/run_preflight.py missing", file=sys.stderr)
        return 2

    if args.check:
        checks = [
            check_one(repo=repo, runtime=runtime, dest=dest)
            for runtime, dest in default_targets(args.home.resolve())
        ]
        drifted = [item for item in checks if item.get("status") == "drift"]
        unusable = [item for item in checks if item.get("status") == "missing_adapter"]
        # 何も検査できなかったのを "pass" と書くと JSON を読む側が fail-open する
        if unusable:
            top_status = "tool_error"
            next_action = "--repo が repo-preflight checkout を指しているか確認する"
        elif drifted:
            top_status = "drift"
            next_action = "python scripts/install_runtime_skills.py --apply で再配布する"
        else:
            top_status = "pass"
            next_action = "no action"
        payload = {
            "schema": "repo-preflight.check-runtime-skills/v1",
            "repo": str(repo),
            "read_only": True,
            "status": top_status,
            "results": checks,
            "guarantee": (
                "install 済み skill コピーの SKILL.md / run_preflight.py / README.md / "
                "checkout link を repo 正本と sha256 で突き合わせる"
            ),
            "non_guarantee": (
                "install していないマシンの状態; repo 正本そのものの正しさ; "
                "CI 上での実行 (CI に install 済みコピーは存在しない)"
            ),
            "next": next_action,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if unusable:
            return 2
        return 1 if drifted else 0

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
