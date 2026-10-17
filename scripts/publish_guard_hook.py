#!/usr/bin/env python3
"""AI エージェントが repository を直接公開するのを止める Claude Code の PreToolUse フック。

GitHub には公開への切り替えそのものを条件付きで止める設定が無い。公開は `publish_gate.py`
経由に限る運用を、エージェントの操作の手前で機械的に守らせる。

止める操作:
- Bash で `gh repo edit ... --visibility public`、`gh repo create ... --public`
- Bash で `gh api` / `curl` から GitHub API の `private=false` や `visibility=public` を送る
- GitHub MCP の create_repository で `private` が true でないもの（既定は公開になる）

`publish_gate.py` は内部で gh を呼ぶため、このフックからは見えず、そのまま通る。
入力を読めないときは止めない（フックの故障でエージェントの作業全体を止めない）。
人が GitHub の画面や自分の端末で行う公開は止められない。

`--install` を付けて実行すると、Claude Code のユーザー設定（`~/.claude/settings.json`）へ
このフックを足す。macOS・Linux・Windows で同じコマンドが使える。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REASON = (
    "repository を公開に切り替える操作は repo-preflight の公開コマンドからだけ行います。"
    "`python <repo-preflight>/scripts/publish_gate.py --repo <clone>` で判定し、"
    "人が公開を決めたら `--apply --confirm OWNER/NAME` を付けてください。"
    "新しい repository は private で作ってください。"
)

_SEP = r"(?:\s+|\s*=\s*)"
BASH_PATTERNS = (
    re.compile(r"\bgh\s+repo\s+edit\b.*--visibility" + _SEP + r"['\"]?public\b", re.S),
    re.compile(
        r"\bgh\s+repo\s+create\b.*(?:--public\b|--visibility"
        + _SEP
        + r"['\"]?public\b)",
        re.S,
    ),
)
API_CALL = re.compile(r"\bgh\s+api\b|api\.github\.com|\bgh\s+repo\s+edit\b")
API_PUBLIC_FIELDS = (
    re.compile(r"\bprivate['\"]?\s*[:=]\s*['\"]?false\b"),
    re.compile(r"\bvisibility['\"]?\s*[:=]\s*['\"]?public\b"),
)


# Windows では PowerShell のツールで gh を呼ぶこともあるため、シェル系はまとめて見る。
SHELL_TOOLS = ("Bash", "PowerShell")


def bash_command_publishes(command: str) -> bool:
    text = command.lower()
    if any(p.search(text) for p in BASH_PATTERNS):
        return True
    return bool(API_CALL.search(text)) and any(
        p.search(text) for p in API_PUBLIC_FIELDS
    )


def mcp_call_publishes(tool_name: str, tool_input: dict[str, Any]) -> bool:
    name = tool_name.lower()
    if "github" not in name:
        return False
    if name.endswith("create_repository"):
        return tool_input.get("private") is not True
    visibility = str(tool_input.get("visibility", "")).lower()
    return visibility == "public" or tool_input.get("private") is False


def decide(event: dict[str, Any]) -> str | None:
    """止めるなら理由を、通すなら None を返す。"""
    tool_name = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    if tool_name in SHELL_TOOLS:
        if bash_command_publishes(str(tool_input.get("command", ""))):
            return REASON
        return None
    if tool_name.startswith("mcp__") and mcp_call_publishes(tool_name, tool_input):
        return REASON
    return None


def main(stdin=None, stdout=None) -> int:
    # Windows の既定コードページに左右されないよう、入出力は UTF-8 のバイト列で扱う。
    raw = stdin.read() if stdin is not None else sys.stdin.buffer.read()
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        event = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    if not isinstance(event, dict):
        return 0
    reason = decide(event)
    if reason:
        body = json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
        if stdout is not None:
            stdout.write(body)
        else:
            sys.stdout.buffer.write(body.encode("utf-8"))
            sys.stdout.buffer.flush()
    return 0


MATCHER = "Bash|PowerShell|mcp__.*github.*"


def hook_command() -> str:
    """この Python とこのスクリプトの絶対パスで組んだ hook のコマンド（OS を問わない）。"""
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def install(settings_path: Path) -> str:
    """ユーザー設定へフックを足す。既にあれば何もしない。元のファイルは .bak に残す。"""
    settings: dict[str, Any] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("settings.json is not a JSON object")
    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault("PreToolUse", [])
    script = Path(__file__).name
    for entry in entries:
        for hook in entry.get("hooks", []):
            if script in str(hook.get("command", "")):
                return "already_installed"
    entries.append(
        {"matcher": MATCHER, "hooks": [{"type": "command", "command": hook_command()}]}
    )
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        shutil.copy2(
            settings_path, settings_path.with_name(settings_path.name + ".bak")
        )
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return "installed"


def cli(argv: list[str]) -> int:
    if not argv:
        return main()
    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="引数なしで PreToolUse フックとして動く。--install でユーザー設定へ足す。"
    )
    parser.add_argument(
        "--install", action="store_true", help="Claude Code のユーザー設定へ足す"
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
        help="足す先の settings.json（既定はユーザー設定）",
    )
    args = parser.parse_args(argv)
    if not args.install:
        parser.error("--install を指定してください")
    print(install(args.settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
