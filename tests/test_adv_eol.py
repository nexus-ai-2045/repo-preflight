"""品質判定 M3 のカナリア：Windows（core.autocrlf=true）で取り出してもフックが CRLF にならない。

.gitattributes の eol=lf を写した一時リポを autocrlf=true で clone し、フック・スクリプトに
CR が入らないことを見る。値はすべて仮のもの。GitHub には何も送らない。
"""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LF_FILES = (
    "templates/hooks/pre-commit",
    "templates/hooks/commit-msg",
    "templates/hooks/pre-push",
    "templates/canary/local-canary.sh",
    "templates/ai/claude-settings.json",
    "templates/ai/codex-config.toml",
    "scripts/identity_gate.py",
    "scripts/push_baseline_guard.py",
    "scripts/ai_command_guard.py",
    ".github/workflows/preflight-gate.yml",
)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def test_フックとスクリプトはeol_lfの属性を持つ():
    proc = _git(ROOT, "check-attr", "eol", "--", *LF_FILES)
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert len(lines) == len(LF_FILES)
    for line in lines:
        assert line.endswith(": eol: lf"), line


def test_作業ツリーのフックにCRが無い():
    for name in ("pre-commit", "commit-msg", "pre-push"):
        assert b"\r" not in (ROOT / "templates/hooks" / name).read_bytes()


def test_autocrlfで取り出してもフックはLFのまま(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    shutil.copy2(ROOT / ".gitattributes", src / ".gitattributes")
    for rel in LF_FILES:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, src / rel)
    _git(src, "add", "-A")
    done = _git(
        src,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "eol",
    )
    assert done.returncode == 0, done.stderr
    dst = tmp_path / "dst"
    cloned = subprocess.run(
        ["git", "-c", "core.autocrlf=true", "clone", "-q", str(src), str(dst)],
        capture_output=True,
        text=True,
    )
    assert cloned.returncode == 0, cloned.stderr
    for rel in LF_FILES:
        assert b"\r" not in (dst / rel).read_bytes(), rel
