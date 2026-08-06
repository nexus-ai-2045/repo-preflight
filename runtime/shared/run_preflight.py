#!/usr/bin/env python3
"""Portable launcher for readiness_scan.py.

絶対 path を skill に焼かない。解決順:
1. 環境変数 REPO_PREFLIGHT_ROOT
2. このファイル隣の checkout/ (install が作る link)
3. このファイル隣が repo root そのもの (scripts/readiness_scan.py がある)
4. カレントから上に向かって repo-preflight root を探索
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def looks_like_root(path: Path) -> bool:
    scan = path / "scripts" / "readiness_scan.py"
    skill = path / "SKILL.md"
    return scan.is_file() and skill.is_file()


def discover_root(start: Path | None = None) -> Path:
    env = os.environ.get("REPO_PREFLIGHT_ROOT", "").strip().strip('"')
    if env:
        candidate = Path(env).expanduser().resolve()
        if looks_like_root(candidate):
            return candidate
        raise SystemExit(
            f"error: REPO_PREFLIGHT_ROOT is set but is not a repo-preflight root: {candidate}"
        )

    here = (start or Path(__file__).resolve()).parent
    checkout = here / "checkout"
    if checkout.exists():
        try:
            if looks_like_root(checkout):
                return checkout.resolve()
        except OSError:
            pass
        # link が作れず path-file fallback した install
        marker = checkout / "ROOT_PATH.txt"
        if marker.is_file():
            target = Path(marker.read_text(encoding="utf-8").strip().strip('"'))
            if looks_like_root(target):
                return target.resolve()

    if looks_like_root(here):
        return here.resolve()

    # install せず clone 直下で python runtime/shared/run_preflight.py した場合
    shared_parent = here.parent
    if shared_parent.name == "runtime" and looks_like_root(shared_parent.parent):
        return shared_parent.parent.resolve()

    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        if looks_like_root(parent):
            return parent

    raise SystemExit(
        "error: repo-preflight root not found.\n"
        "Fix one of:\n"
        "  - run: python scripts/install_runtime_skills.py --repo <clone> --apply\n"
        "  - set REPO_PREFLIGHT_ROOT to your clone path\n"
        "  - cd into the clone and retry\n"
        "  - or call scripts/readiness_scan.py directly from the clone"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = discover_root()
    scan = root / "scripts" / "readiness_scan.py"
    # readiness_scan.main を同じプロセスで実行し、sys.argv を差し替える
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(scan), *args]
        runpy.run_path(str(scan), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 2
    finally:
        sys.argv = old_argv
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
