#!/usr/bin/env python3
"""README の図区画を mermaid で埋める（リリース準備用）。

印 ``<!-- diagram:start -->`` と ``<!-- diagram:end -->`` の間だけを書き換える。
印が無い README は触らない。図の文字列は identity_gate の形検査（実ホームパス・
個人メール）を通してから書く。公開 / 非公開の visibility で挙動を変えない。
既定は read-only。書き込みは ``--apply``。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "repo-preflight.readme-diagram/v1"
START_MARKER = "<!-- diagram:start -->"
END_MARKER = "<!-- diagram:end -->"
VISIBILITIES = frozenset({"public", "private"})

# リリース時の構成・流れ。ラベルは日本語（readme_release_gate の可読性契約に合わせる）。
CANONICAL_DIAGRAM = (
    "```mermaid\n"
    "flowchart TD\n"
    "    A[版と文書をそろえる] --> B[README を更新する]\n"
    "    B --> C[構成と流れの図を置く]\n"
    "    C --> D[検査を回す]\n"
    "    D --> E[人が判断する]\n"
    "```\n"
)


def _load_identity_gate():
    script = Path(__file__).resolve().with_name("identity_gate.py")
    existing = sys.modules.get("identity_gate")
    if existing is not None and getattr(existing, "shape_findings", None):
        return existing
    spec = importlib.util.spec_from_file_location("identity_gate", script)
    if spec is None or spec.loader is None:
        raise ImportError("identity_gate_unavailable")
    module = importlib.util.module_from_spec(spec)
    # dataclass 解決のため、exec 前に sys.modules へ登録する
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def diagram_body() -> str:
    """印の間へ書く本文。末尾改行 1 個へそろえる。"""
    return normalize_text(CANONICAL_DIAGRAM).rstrip("\n") + "\n"


def check_diagram_identity(text: str) -> list[str]:
    """図の文字列へ identity_gate の形検査を掛ける。値は出さず種類だけ返す。"""
    gate = _load_identity_gate()
    return list(gate.shape_findings(text))


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _fence_open(content: str) -> tuple[str, int] | None:
    """コードブロックの開きなら (文字, 長さ)。CommonMark の fenced code に合わせる。"""
    match = _FENCE.match(content)
    if match is None:
        return None
    fence, info = match.group(1), match.group(2)
    if fence[0] == "`" and "`" in info:
        return None
    return fence[0], len(fence)


def _fence_closes(content: str, char: str, length: int) -> bool:
    match = _FENCE.match(content)
    if match is None:
        return False
    fence, rest = match.group(1), match.group(2)
    return fence[0] == char and len(fence) >= length and rest.strip() == ""


def _marker_lines(text: str) -> tuple[list[int], list[int]]:
    """行全体が印のときだけ数える（本文中の `印` の言及は数えない）。

    ``` や ~~~ のコードブロックの中にある印（書き方の例）も数えない。
    閉じていないコードブロックは文書の終わりまで続くものとして扱う。
    返すのは start 印の行の次の位置と、end 印の行の先頭の位置。
    """
    starts: list[int] = []
    ends: list[int] = []
    offset = 0
    fence: tuple[str, int] | None = None
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if fence is not None:
            if _fence_closes(content, *fence):
                fence = None
            offset += len(line)
            continue
        opened = _fence_open(content)
        if opened is not None:
            fence = opened
            offset += len(line)
            continue
        if stripped == START_MARKER:
            starts.append(offset + len(line))
        elif stripped == END_MARKER:
            ends.append(offset)
        offset += len(line)
    return starts, ends


def locate_markers(text: str) -> dict[str, Any]:
    """印の有無と位置。曖昧なら書き込まない。"""
    starts, ends = _marker_lines(text)
    if not starts and not ends:
        return {"state": "absent"}
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return {
            "state": "ambiguous",
            "start_count": len(starts),
            "end_count": len(ends),
        }
    return {"state": "ready", "inner_start": starts[0], "inner_end": ends[0]}


def _newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def render_with_diagram(text: str, body: str) -> str | None:
    """印の間だけを body で差し替えた全文。印の外は元のまま（改行の形も）。"""
    located = locate_markers(text)
    if located["state"] != "ready":
        return None
    inner_start = int(located["inner_start"])
    inner_end = int(located["inner_end"])
    prefix = text[:inner_start]
    suffix = text[inner_end:]
    newline = _newline_of(text)
    if not prefix.endswith("\n"):
        # 印の行が改行なしで終わる（start 行がファイル末尾）ことは ready では起きない
        prefix += newline
    body = normalize_text(body).rstrip("\n") + "\n"
    if newline != "\n":
        body = body.replace("\n", newline)
    return prefix + body + suffix


def plan_readme_diagram(
    text: str,
    *,
    visibility: str = "public",
    diagram: str | None = None,
) -> dict[str, Any]:
    """書き換え計画を返す。visibility は受け取るが判定を分けない。"""
    if visibility not in VISIBILITIES:
        return {
            "schema": SCHEMA,
            "status": "tool_error",
            "reason": "visibility_invalid",
            "findings": ["visibility_invalid"],
            "changed": False,
            "visibility": visibility,
        }
    located = locate_markers(text)
    if located["state"] == "absent":
        return {
            "schema": SCHEMA,
            "status": "pass",
            "reason": "markers_absent",
            "findings": [],
            "changed": False,
            "visibility": visibility,
            "rendered": text,
        }
    if located["state"] != "ready":
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": "markers_ambiguous",
            "findings": ["markers_ambiguous"],
            "changed": False,
            "visibility": visibility,
        }
    body = (
        diagram_body()
        if diagram is None
        else normalize_text(diagram).rstrip("\n") + "\n"
    )
    identity_findings = check_diagram_identity(body)
    if identity_findings:
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": "diagram_identity_blocked",
            "findings": identity_findings,
            "changed": False,
            "visibility": visibility,
        }
    rendered = render_with_diagram(text, body)
    if rendered is None:
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": "markers_ambiguous",
            "findings": ["markers_ambiguous"],
            "changed": False,
            "visibility": visibility,
        }
    current = text
    return {
        "schema": SCHEMA,
        "status": "pass",
        "reason": "diagram_ready" if rendered != current else "already_current",
        "findings": [],
        "changed": rendered != current,
        "visibility": visibility,
        "rendered": rendered,
    }


def sync_readme_diagram(
    readme: Path,
    *,
    visibility: str = "public",
    apply: bool = False,
    diagram: str | None = None,
) -> dict[str, Any]:
    """README 1 ファイルを計画し、必要なら印の間だけ書き込む。"""
    if not readme.is_file() or readme.is_symlink():
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": "readme_missing",
            "findings": ["readme_missing"],
            "changed": False,
            "visibility": visibility,
        }
    try:
        with readme.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return {
            "schema": SCHEMA,
            "status": "tool_error",
            "reason": "readme_unreadable",
            "findings": ["readme_unreadable"],
            "changed": False,
            "visibility": visibility,
        }
    plan = plan_readme_diagram(text, visibility=visibility, diagram=diagram)
    if plan["status"] != "pass" or not apply or not plan.get("changed"):
        # apply 前の計画結果を返す（書き込みなし）。冪等で changed=False のときも同じ
        report = {k: v for k, v in plan.items() if k != "rendered"}
        report["applied"] = False
        return report
    rendered = str(plan["rendered"])
    try:
        with readme.open("w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
    except OSError:
        return {
            "schema": SCHEMA,
            "status": "tool_error",
            "reason": "readme_write_failed",
            "findings": ["readme_write_failed"],
            "changed": False,
            "applied": False,
            "visibility": visibility,
        }
    return {
        "schema": SCHEMA,
        "status": "pass",
        "reason": "diagram_applied",
        "findings": [],
        "changed": True,
        "applied": True,
        "visibility": visibility,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--visibility",
        choices=sorted(VISIBILITIES),
        default="public",
        help="公開 / 非公開の区別用。挙動は同じ（試験用）。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="印の間へ図を書き込む。省略時は計画だけ。",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    readme = args.repo.resolve() / "README.md"
    report = sync_readme_diagram(readme, visibility=args.visibility, apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["status"])
    if report["status"] == "pass":
        return 0
    if report["status"] == "blocked":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
