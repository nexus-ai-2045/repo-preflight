#!/usr/bin/env python3
"""AI のシェル実行の前に呼ぶ検査（Claude Code の PreToolUse / Codex の PreToolUse 用）。

共通フックを外す・差し替えるコマンドを AI 側では拒む（品質判定 M8）。人が自分の端末で打つ分は止めない。
拒むもの：
- `--no-verify`（省略形 `--no-veri` なども）と `git commit -n`
- `core.hooksPath` に触れるもの（`git -c core.hooksPath=…`・`git config core.hooksPath …` など）
- `git commit-tree`（フックを通らない commit の作り方）
- 検査が頼る環境変数の上書き・削除：REPO_PREFLIGHT_*、PATH、PYTHON*（出力まわりの一部を除く）、
  GIT_*（GIT_PAGER など表示まわりを除く）

標準入力は PreToolUse の JSON（tool_input.command）。拒むときは exit 2 と理由（stderr）。
読めない入力も拒む（fail-closed）。base64 などで隠したものまでは見抜けないので、最後の砦は CI の必須チェック。
`python3 -I` で呼ぶ前提のため、標準ライブラリだけを使い、同じフォルダのモジュールも読まない。
"""

from __future__ import annotations

import json
import re
import shlex
import sys

ALLOWED_ENV = {
    "GIT_PAGER",
    "GIT_TERMINAL_PROMPT",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
}
_ENV_NAME = r"(?:REPO_PREFLIGHT_[A-Z0-9_]*|PATH|PYTHON[A-Z0-9_]*|GIT_[A-Z0-9_]*)"
_SEP = r"(?:^|(?<=[\s;&|(`'\"{]))"
_RULES = (
    ("--no-verify", re.compile(r"--no-veri(?:fy?)?(?![\w-])")),
    ("core.hooksPath", re.compile(r"core\.hookspath", re.IGNORECASE)),
    ("git commit-tree", re.compile(r"\bcommit-tree\b")),
)
_ASSIGN = re.compile(_SEP + r"(" + _ENV_NAME + r")\+?=")
# PowerShell の $env: は名前の大小を区別しない
_PS_ASSIGN = re.compile(r"\$env:(" + _ENV_NAME + r")\s*\+?=", re.IGNORECASE)
_UNSET = re.compile(
    r"\b(?:unset|setenv|unsetenv|env\s+-u|export\s+-n)\s+"
    r"(?:-\w+\s+)*((?:\w+\s+)*?" + _ENV_NAME + r")\b"
)
_PS_REMOVE = re.compile(r"Remove-Item\s+env:(" + _ENV_NAME + r")\b", re.IGNORECASE)
_SEGMENT = re.compile(r"&&|\|\||[;|\n&]")
_VALUE_SHORT = set("mFCct")


def _env_names(command: str) -> list[str]:
    found = []
    for match in [
        *_ASSIGN.finditer(command),
        *_PS_ASSIGN.finditer(command),
        *_PS_REMOVE.finditer(command),
    ]:
        name = match.group(1)
        if name.upper() not in ALLOWED_ENV:
            found.append(name)
    for match in _UNSET.finditer(command):
        for name in match.group(1).split():
            if re.fullmatch(_ENV_NAME, name) and name not in ALLOWED_ENV:
                found.append(name)
    return found


def _commit_short_n(command: str) -> bool:
    """`git commit -n`（短い -n は --no-verify）。-m などの値の中の -n は数えない。"""
    for segment in _SEGMENT.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if (
            "git" not in [t.rsplit("/", 1)[-1] for t in tokens]
            or "commit" not in tokens
        ):
            continue
        rest = tokens[tokens.index("commit") + 1 :]
        skip = False
        for token in rest:
            if skip:
                skip = False
                continue
            if token == "--":
                break
            if not token.startswith("-") or token.startswith("--") or token == "-":
                continue
            for index, char in enumerate(token[1:]):
                if char == "n":
                    return True
                if char in _VALUE_SHORT:
                    skip = index == len(token) - 2  # 値が次の語にある
                    break
    return False


def reasons(command: str) -> list[str]:
    text = command.replace("\\\n", " ")
    found = [label for label, rule in _RULES if rule.search(text)]
    if _commit_short_n(text):
        found.append("git commit -n")
    names = _env_names(text)
    if names:
        found.append("環境変数の上書き（" + ", ".join(sorted(set(names))) + "）")
    return found


def extract_command(data: object) -> str | None:
    if not isinstance(data, dict):
        raise ValueError("not an object")
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    return None


def main() -> int:
    try:
        command = extract_command(json.loads(sys.stdin.read()))
    except (ValueError, UnicodeDecodeError):
        sys.stderr.write("repo-preflight: 入力を読めないので実行を止めます\n")
        return 2
    if command is None:
        return 0
    found = reasons(command)
    if not found:
        return 0
    sys.stderr.write(
        "repo-preflight: 共通の検査（フック）を外す・差し替える操作なので、AI からは実行できません："
        + "、".join(found)
        + "。必要なら人が自分の端末で判断して行ってください\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
