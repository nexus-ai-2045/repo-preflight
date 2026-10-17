"""publish_guard_hook の判定。Claude Code が渡す PreToolUse の JSON を模して与える。"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_guard_hook as h


def run(event):
    out = io.StringIO()
    assert h.main(io.StringIO(json.dumps(event)), out) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def denied(result):
    return (
        result is not None
        and result["hookSpecificOutput"]["permissionDecision"] == "deny"
    )


@pytest.mark.parametrize(
    "command",
    [
        "gh repo edit owner/name --visibility public --accept-visibility-change-consequences",
        "gh repo edit --visibility=PUBLIC",
        "cd x && gh repo edit owner/name \\\n  --visibility public",
        "gh repo create owner/new --public --source .",
        "gh repo create owner/new --visibility public",
        "gh api -X PATCH repos/owner/name -F private=false",
        "gh api -X PATCH repos/owner/name -f visibility=public",
        "curl -X PATCH https://api.github.com/repos/o/n -d '{\"private\": false}'",
    ],
)
def test_直接の公開操作は止める(command):
    assert denied(run(bash(command)))


@pytest.mark.parametrize(
    "command",
    [
        "python scripts/publish_gate.py --repo . --apply --confirm owner/name",
        "gh repo create owner/new --private",
        "gh repo edit owner/name --visibility private",
        "gh repo edit owner/name --description public",
        "gh api repos/owner/name",
        "git push -u origin main",
    ],
)
def test_公開しない操作は通す(command):
    assert run(bash(command)) is None


def test_MCPでprivate指定の無いrepository作成は止める():
    event = {"tool_name": "mcp__github__create_repository", "tool_input": {"name": "x"}}
    assert denied(run(event))


def test_MCPでprivateのrepository作成は通す():
    event = {
        "tool_name": "mcp__github__create_repository",
        "tool_input": {"name": "x", "private": True},
    }
    assert run(event) is None


def test_GitHub以外のMCPは見ない():
    event = {"tool_name": "mcp__other__create_repository", "tool_input": {}}
    assert run(event) is None


def test_読めない入力では止めない():
    out = io.StringIO()
    assert h.main(io.StringIO("not json"), out) == 0
    assert out.getvalue() == ""


def test_理由に公開コマンドを案内する():
    reason = run(bash("gh repo create o/n --public"))["hookSpecificOutput"][
        "permissionDecisionReason"
    ]
    assert "publish_gate.py" in reason


def test_UTF8のバイト列入力を読む():
    event = bash("echo 公開 && gh repo create o/n --public")
    out = io.StringIO()
    assert (
        h.main(io.BytesIO(json.dumps(event, ensure_ascii=False).encode("utf-8")), out)
        == 0
    )
    assert denied(json.loads(out.getvalue()))


def test_PowerShellのコマンドも見る():
    event = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "gh repo edit o/n --visibility public"},
    }
    assert denied(run(event))


def test_実プロセスとしてstdinから読む():
    import subprocess

    script = Path(h.__file__)
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(
            bash("gh repo create o/n --public"), ensure_ascii=False
        ).encode("utf-8"),
        capture_output=True,
        check=True,
    )
    result = json.loads(proc.stdout.decode("utf-8"))
    assert denied(result)


def test_installはユーザー設定へ一度だけ足し既存の設定を残す(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"model": "x", "hooks": {"PreToolUse": [{"matcher": "Edit"}]}}),
        encoding="utf-8",
    )
    assert h.install(settings) == "installed"
    assert h.install(settings) == "already_installed"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "x"
    entries = data["hooks"]["PreToolUse"]
    assert len(entries) == 2 and entries[0] == {"matcher": "Edit"}
    assert "publish_guard_hook.py" in entries[1]["hooks"][0]["command"]
    assert (tmp_path / "settings.json.bak").exists()


def test_installは設定ファイルが無ければ作る(tmp_path):
    settings = tmp_path / "nested" / "settings.json"
    assert h.install(settings) == "installed"
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]


def test_installしたコマンドで実際にフックが動く(tmp_path):
    import shlex
    import subprocess

    settings = tmp_path / "settings.json"
    h.install(settings)
    command = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][
        0
    ]["hooks"][0]["command"]
    args = shlex.split(command, posix=(sys.platform != "win32"))
    args = [a.strip('"') for a in args]
    proc = subprocess.run(
        args,
        input=json.dumps(bash("gh repo create o/n --public")).encode("utf-8"),
        capture_output=True,
        check=True,
    )
    assert denied(json.loads(proc.stdout.decode("utf-8")))
