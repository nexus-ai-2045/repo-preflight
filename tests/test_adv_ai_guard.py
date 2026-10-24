"""品質判定 M8 のカナリア：検査を外す操作を AI 側（PreToolUse）で拒む。

scripts/ai_command_guard.py に PreToolUse と同じ JSON を渡し、外す形は exit 2、ふつうの操作は exit 0 を見る。
Claude Code と Codex のひな形に書いたコマンドそのものでも同じことを見る（実際の AI 設定は触らない）。
フックが PYTHONPATH の差し替えを読まないこと（-E）も、本物の git commit で見る。値はすべて仮のもの。
"""

import json
import os
import shutil
import subprocess
import sys

try:
    import tomllib
except ImportError:  # Python 3.9/3.10 は tomli（test 依存）
    import tomli as tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts/ai_command_guard.py"
PLACEHOLDER = "/ABSOLUTE/PATH/TO/repo-preflight"

DENY = [
    "git commit --no-verify -m x",
    "git push --no-verify origin main",
    "git push --no-veri origin main",
    "git commit -nm x",
    "git commit -m x -n",
    "git -c core.hooksPath=/dev/null commit -m x",
    "git config core.hookspath /tmp/none",
    "git config --unset core.hooksPath",
    "REPO_PREFLIGHT_HOME=/tmp/fake git push",
    "env PATH=/tmp/fake git commit -m x",
    "export PYTHONPATH=/tmp/fake",
    "PYTHONHOME=/tmp/fake git commit -m x",
    "unset REPO_PREFLIGHT_HOME",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath git commit",
    "GIT_CONFIG_PARAMETERS=x git commit -m x",
    "$env:Path = 'C:\\fake'",
    "git commit-tree HEAD^{tree} -m x",
    "cd w && \\\n  git commit --no-verify -m x",
]
ALLOW = [
    "git commit -m x",
    'git commit -m "fix -n bug"',
    "git commit -F msg.txt",
    "git push origin main",
    "GIT_PAGER=cat git log -1",
    "PYTHONUNBUFFERED=1 python3 x.py",
    "path=a; echo $path $PATH",
    "ls -la",
]


def _run(command_line, payload, shell=False):
    return subprocess.run(
        command_line,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=shell,
    )


def _bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


@pytest.mark.parametrize("command", DENY)
def test_検査を外す操作は拒む(command):
    result = _run([sys.executable, "-I", str(GUARD)], _bash(command))
    assert result.returncode == 2, command
    assert "AI からは実行できません" in result.stderr


@pytest.mark.parametrize("command", ALLOW)
def test_ふつうの操作は通す(command):
    result = _run([sys.executable, "-I", str(GUARD)], _bash(command))
    assert result.returncode == 0, (command, result.stderr)


def test_読めない入力は拒み_シェル以外は通す():
    bad = subprocess.run(
        [sys.executable, "-I", str(GUARD)], input="xx", capture_output=True, text=True
    )
    assert bad.returncode == 2
    other = _run(
        [sys.executable, "-I", str(GUARD)],
        {"tool_name": "Read", "tool_input": {"file_path": "a"}},
    )
    assert other.returncode == 0
    listed = _run(
        [sys.executable, "-I", str(GUARD)],
        {
            "tool_name": "Bash",
            "tool_input": {"command": ["git", "commit", "--no-verify"]},
        },
    )
    assert listed.returncode == 2


def _template_commands():
    claude = json.loads((ROOT / "templates/ai/claude-settings.json").read_text("utf-8"))
    (entry,) = claude["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    codex = tomllib.loads((ROOT / "templates/ai/codex-config.toml").read_text("utf-8"))
    (centry,) = codex["hooks"]["PreToolUse"]
    assert centry["matcher"] == "^Bash$"
    (chook,) = centry["hooks"]
    assert chook["type"] == "command"
    return {"claude": hook["command"], "codex": chook["command"]}


@pytest.mark.skipif(os.name == "nt", reason="ひな形のコマンドは sh で走らせる")
@pytest.mark.parametrize("which", ["claude", "codex"])
def test_ひな形のコマンドそのものが拒む(which):
    command = _template_commands()[which]
    assert command.startswith("python3 -I ") and PLACEHOLDER in command
    real = command.replace(PLACEHOLDER, str(ROOT))
    denied = _run(real, _bash("git commit --no-verify -m x"), shell=True)
    assert denied.returncode == 2, denied.stderr
    allowed = _run(real, _bash("git status"), shell=True)
    assert allowed.returncode == 0, allowed.stderr


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("gitleaks") is None,
    reason="ひな形は sh・gitleaks で動く",
)
def test_フックはPYTHONPATHの差し替えを読まない(tmp_path, monkeypatch):
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("REPO_PREFLIGHT_HOME", str(ROOT))
    # 読まれたら検査を何もせず「通った」で終わらせる差し替え
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "sitecustomize.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(evil))
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(ROOT / "templates/hooks/pre-commit", hooks / "pre-commit")
    (hooks / "pre-commit").chmod(0o755)
    work = tmp_path / "w"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (
        ("user.name", "someone"),
        ("user.email", "someone@example.com"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "a.txt"], check=True)
    result = subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "対照"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0  # 許可名義でない作者は、差し替えがあっても止まる
