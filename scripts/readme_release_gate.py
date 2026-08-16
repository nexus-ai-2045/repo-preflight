from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SECTION_ALIASES = {
    "purpose": ("目的", "why", "overview", "概要"),
    "capabilities": ("できること", "what", "features", "機能"),
    "quickstart": ("クイックスタート", "quickstart", "quick start", "使い方", "usage"),
    "limits": ("制約", "限界", "limitations", "limits", "安全境界", "security", "注意"),
}
MAX_LINES = 300
MAX_SUMMARY_CHARS = 240
# 日本語READMEの可読性。閾値は既存repoの実測 (2026-08-15, 14種) に合わせる。
# 表セルはコードを含むものだけを見る。Markdownリンクはfile名を2度書くため
# 長くなりやすく、読みにくさとは別問題なので対象にしない。
MAX_TABLE_COMMAND_CELL_CHARS = 100
JAPANESE_MIN_CHARS = 20
JAPANESE_MIN_RATIO = 0.1
JAPANESE_RE = re.compile(r"[ぁ-んァ-ヶ一-龠]")
CODE_SPAN_RE = re.compile(r"`[^`]+`")
LINK_DESTINATION_RE = re.compile(r"(?<=\])\([^)]*\)")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})([^`]*)$")
# mermaid のラベル抽出。行を先に「図の種類」と「行の種類」で分類してから
# 抽出する (2026-08-16 review F4/F5/F8: regex 継ぎ足しで境界が壊れていた)。
# node ラベルは | を含んでよい ({Yes|No})。edge ラベルは矢印の直後の |..| だけ。
MERMAID_NODE_LABEL_RE = re.compile(r"[\[(\"{]([^\[\]()\"{}]{2,}?)[\])\"}]")
MERMAID_ASYMMETRIC_NODE_RE = re.compile(r"\b\w+>([^]\n]{2,})\]")
MERMAID_PIPE_EDGE_LABEL_RE = re.compile(r"(?:-->|---|-\.->|==>)\s*\|([^|]{2,})\|")
MERMAID_TEXT_EDGE_LABEL_RE = re.compile(r"--\s+(.+?)\s+-->")
MERMAID_MESSAGE_LABEL_RE = re.compile(r"(?:-{1,2}>>?|--?[x)])[^:]*:\s*(.+)$")
# 図の中身ではなく装飾・操作・構造を書く行。ラベルとして数えない
MERMAID_DIRECTIVE_PREFIXES = (
    "style ",
    "classDef ",
    "class ",
    "linkStyle ",
    "click ",
    "subgraph",
    "end",
    "direction ",
    "%%",
)
# message 抽出を当ててよい図。flowchart 系に当てると「A[読込: x]」の : でゴミが出る
MERMAID_MESSAGE_DIAGRAMS = ("sequencediagram",)


def _headings(lines: list[str]) -> list[tuple[int, str, int]]:
    result = []
    for number, line in enumerate(lines, start=1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            result.append((len(match.group(1)), match.group(2).strip(), number))
    return result


def _first_summary(lines: list[str]) -> str:
    # fence の判定は _outside_fences に一本化する。ここだけ別規則だと
    # インデントされた fence の中身が要約に混ざる (2026-08-16 review F6)
    after_title = False
    paragraph: list[str] = []
    for _, line in _outside_fences(lines):
        if not after_title:
            if line.startswith("# "):
                after_title = True
            continue
        if line.startswith("## "):
            break
        if line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            break
    return " ".join(paragraph)


def _outside_fences(lines: list[str]) -> list[tuple[int, str]]:
    """コードブロックの外にある行だけを (行番号, 本文) で返す。"""
    result: list[tuple[int, str]] = []
    fence_marker: str | None = None
    for number, line in enumerate(lines, start=1):
        match = FENCE_RE.match(line)
        if match and fence_marker is None:
            fence_marker = match.group(1)
            continue
        if fence_marker and line.lstrip().startswith(
            fence_marker[0] * len(fence_marker)
        ):
            fence_marker = None
            continue
        if fence_marker is None:
            result.append((number, line))
    return result


def _is_japanese_document(lines: list[str]) -> bool:
    """日本語で書かれたREADMEか。英語READMEへ日本語向けの基準を当てないための判定。"""
    body = "".join(line for _, line in _outside_fences(lines))
    body = CODE_SPAN_RE.sub("", body)
    body = LINK_DESTINATION_RE.sub("", body)
    japanese = len(JAPANESE_RE.findall(body))
    letters = len("".join(body.split()))
    if japanese < JAPANESE_MIN_CHARS or not letters:
        return False
    return japanese / letters >= JAPANESE_MIN_RATIO


def _split_markdown_row(line: str) -> list[str]:
    """Markdown表の区切りだけを分割し、escape・code span内のpipeは保持する。"""
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for char in line.strip():
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == "`":
            current.append(char)
            in_code = not in_code
        elif char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


def _is_table_divider(cells: list[str]) -> bool:
    return len(cells) >= 2 and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _code_span_width(cell: str) -> int:
    """セル内の code span だけを足した幅。散文は折り返せるので幅の実因にしない
    (2026-08-16 review F2)。"""
    return sum(len(span) for span in CODE_SPAN_RE.findall(cell))


def _table_command_cells(lines: list[str]) -> list[tuple[int, str]]:
    """表のセルのうちコードを含むものを (行番号, セル) で返す。"""
    cells: list[tuple[int, str]] = []
    outside = _outside_fences(lines)
    divider_indexes = {
        index
        for index, (_, line) in enumerate(outside)
        if _is_table_divider(_split_markdown_row(line))
    }
    table_indexes: set[int] = set()
    for divider in divider_indexes:
        if divider > 0 and len(_split_markdown_row(outside[divider - 1][1])) >= 2:
            table_indexes.add(divider - 1)
        cursor = divider + 1
        while cursor < len(outside):
            row = _split_markdown_row(outside[cursor][1])
            if len(row) < 2:
                break
            table_indexes.add(cursor)
            cursor += 1
    for index in sorted(table_indexes):
        number, line = outside[index]
        for text in _split_markdown_row(line):
            if CODE_SPAN_RE.search(text):
                cells.append((number, text))
    return cells


def _mermaid_line_labels(line: str, diagram_type: str) -> list[str]:
    """mermaid の 1 行からラベルを取り出す。行の種類と図の種類で抽出器を選ぶ。"""
    stripped = line.strip()
    if not stripped or stripped.startswith(MERMAID_DIRECTIVE_PREFIXES):
        return []
    labels: list[str] = []
    if diagram_type in MERMAID_MESSAGE_DIAGRAMS:
        # sequenceDiagram: 「A->>B: message」の message が本文
        message = MERMAID_MESSAGE_LABEL_RE.search(stripped)
        if message:
            labels.append(message.group(1))
        return [label.strip() for label in labels if label.strip()]
    # flowchart / graph / stateDiagram / erDiagram 等: 括弧内と edge ラベル
    labels.extend(MERMAID_NODE_LABEL_RE.findall(stripped))
    labels.extend(MERMAID_ASYMMETRIC_NODE_RE.findall(stripped))
    labels.extend(MERMAID_PIPE_EDGE_LABEL_RE.findall(stripped))
    labels.extend(MERMAID_TEXT_EDGE_LABEL_RE.findall(stripped))
    if ":" in stripped and re.search(r"(?:\|\||o[|{]|[}|]o)", stripped):
        # erDiagram の「A ||--|| B : 所有」の関係ラベル
        labels.append(stripped.rsplit(":", 1)[1])
    return [label.strip() for label in labels if label.strip()]


def _mermaid_diagrams(lines: list[str]) -> list[list[str]]:
    """mermaid図ごとにノード、矢印、message のラベルを返す。"""
    diagrams: list[list[str]] = []
    labels: list[str] | None = None
    diagram_type = ""
    fence_marker: str | None = None
    for line in lines:
        stripped = line.lstrip()
        match = FENCE_RE.match(line)
        if match and fence_marker is None:
            fence_marker = match.group(1)
            labels = [] if match.group(2).strip().casefold() == "mermaid" else None
            diagram_type = ""
            continue
        if fence_marker and stripped.startswith(fence_marker[0] * len(fence_marker)):
            if labels is not None:
                diagrams.append(labels)
            fence_marker = None
            labels = None
            continue
        if labels is None:
            continue
        if not diagram_type and stripped.strip():
            # 図の 1 行目が種類 (flowchart TD / sequenceDiagram / erDiagram ...)
            diagram_type = stripped.split()[0].casefold()
            continue
        labels.extend(_mermaid_line_labels(line, diagram_type))
    return diagrams


def _japanese_readability_findings(
    lines: list[str],
) -> tuple[list[dict[str, object]], set[str], dict[str, object]]:
    """日本語READMEの可読性検査。(findings, recommendations, metrics) を返す。

    英語READMEには適用しない。metrics は言語に関係なく常に埋める。
    """
    findings: list[dict[str, object]] = []
    recommendations: set[str] = set()
    japanese_document = _is_japanese_document(lines)
    command_cells = _table_command_cells(lines)
    widest_command_cell = max(
        (_code_span_width(cell) for _, cell in command_cells), default=0
    )
    mermaid_diagrams = _mermaid_diagrams(lines)
    diagram_labels = [label for diagram in mermaid_diagrams for label in diagram]
    metrics: dict[str, object] = {
        "japanese_document": japanese_document,
        "widest_table_command_cell": widest_command_cell,
        "diagram_label_count": len(diagram_labels),
    }
    if not japanese_document:
        return findings, recommendations, metrics
    # 表の右列に長いコマンドが入ると、狭い画面で横スクロールが出て読めなくなる。
    # 違反は全行を返す。1 件目で止めると修正→再実行が違反数だけ往復する (review F9)
    wide_lines = sorted(
        {
            line_number
            for line_number, cell in command_cells
            if _code_span_width(cell) > MAX_TABLE_COMMAND_CELL_CHARS
        }
    )
    for line_number in wide_lines:
        findings.append(
            {
                "code": "table_command_cell_too_wide",
                "severity": "error",
                "message": (
                    "表のセルのコマンドが長すぎます。共通部分を表の外へ出すか、"
                    f"{MAX_TABLE_COMMAND_CELL_CHARS}文字以内へ分割してください。"
                ),
                "line": line_number,
            }
        )
    if wide_lines:
        recommendations.add("Template Creator")
    # 本文が日本語なのに図のラベルだけ英語の識別子だと、図から意味が取れない
    if any(
        diagram and not any(JAPANESE_RE.search(label) for label in diagram)
        for diagram in mermaid_diagrams
    ):
        findings.append(
            {
                "code": "diagram_labels_not_localized",
                "severity": "error",
                "message": (
                    "図のラベルを本文と同じ言語にしてください。"
                    "識別子のままだと図から意味が読めません。"
                ),
            }
        )
        recommendations.add("Visualize")
    return findings, recommendations, metrics


def review(readme: Path) -> dict[str, object]:
    text = readme.read_text(encoding="utf-8")
    lines = text.splitlines()
    headings = _headings(lines)
    findings: list[dict[str, object]] = []
    recommendations: set[str] = set()

    def add(
        code: str, message: str, *, severity: str = "error", line: int | None = None
    ) -> None:
        finding: dict[str, object] = {
            "code": code,
            "severity": severity,
            "message": message,
        }
        if line is not None:
            finding["line"] = line
        findings.append(finding)

    if not headings or headings[0][0] != 1:
        add("missing_h1", "先頭に製品名を示すH1が必要です。")
        recommendations.add("Template Creator")

    summary = _first_summary(lines)
    if not summary:
        add(
            "missing_lead_summary", "H1直後に対象読者と価値が分かる短い要約が必要です。"
        )
        recommendations.add("Product Design")
    elif len(summary) > MAX_SUMMARY_CHARS:
        add(
            "lead_summary_too_long",
            f"H1直後の要約を{MAX_SUMMARY_CHARS}文字以内に圧縮してください。",
        )

    normalized_headings = [title.casefold() for _, title, _ in headings]
    missing_sections = []
    for section, aliases in SECTION_ALIASES.items():
        if not any(
            any(alias in title for alias in aliases) for title in normalized_headings
        ):
            missing_sections.append(section)
            add(f"missing_{section}_section", f"READMEに{section}の節が必要です。")
    if missing_sections:
        recommendations.add("Template Creator")
    if "purpose" in missing_sections or "capabilities" in missing_sections:
        recommendations.add("Product Design")

    previous_level = 0
    for level, _, line_number in headings:
        if previous_level and level > previous_level + 1:
            add(
                "heading_level_jump",
                "見出しレベルを飛ばさないでください。",
                line=line_number,
            )
        previous_level = level

    if len(lines) > MAX_LINES:
        add(
            "readme_too_long",
            f"READMEは{MAX_LINES}行以内を目安にし、詳細をdocsへ分離してください。",
        )

    ja_findings, ja_recommendations, ja_metrics = _japanese_readability_findings(lines)
    findings.extend(ja_findings)
    recommendations |= ja_recommendations

    emoji_heading = re.compile(r"^#{1,6}\s+[^\w\s`#]", re.UNICODE)
    if any(emoji_heading.match(line) for line in lines):
        add(
            "decorative_heading_emoji",
            "見出し先頭の装飾絵文字は機能的な意味がない限り外してください。",
            severity="warning",
        )
        recommendations.add("ai-slop-check")

    generic_phrases = (
        "next-generation",
        "revolutionary",
        "cutting-edge",
        "comprehensive solution",
    )
    if any(phrase in text.casefold() for phrase in generic_phrases):
        add(
            "generic_marketing_copy",
            "抽象的な宣伝文句を具体的な利用価値へ置き換えてください。",
            severity="warning",
        )

    ordered_steps = len(re.findall(r"(?m)^\s*\d+[.)]\s+", text))
    has_mermaid = "```mermaid" in text.casefold()
    if ordered_steps >= 4 and not has_mermaid:
        recommendations.add("Visualize")

    has_image = bool(re.search(r"!\[[^]]*]\([^)]+\)", text))
    ui_pattern = re.compile(
        r"(?i)(?:\bui\b|画面|screenshots?|\binterface\b|\bdashboard\b|\bfrontend\b|\bweb app\b)"
    )
    ui_surface_declared = any(ui_pattern.search(title) for _, title, _ in headings)
    if ui_surface_declared and not has_image:
        recommendations.add("Figma or frontend-design")

    blocked = any(item["severity"] == "error" for item in findings)
    return {
        "schema": "repo-preflight.readme-release-gate/v1",
        "status": "blocked" if blocked else "pass",
        "release_gate": "blocked_readme_design" if blocked else "passed_readme_design",
        "metrics": {
            "line_count": len(lines),
            "heading_count": len(headings),
            "lead_summary_chars": len(summary),
            **ja_metrics,
        },
        "findings": findings,
        "recommended_capabilities": sorted(recommendations),
        "human_visual_review_required": True,
        "external_actions_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="release前のREADME情報設計をread-only検査する"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    readme = args.repo.resolve() / "README.md"
    if not readme.is_file():
        result = {
            "schema": "repo-preflight.readme-release-gate/v1",
            "status": "blocked",
            "release_gate": "blocked_readme_missing",
            "findings": [
                {
                    "code": "readme_missing",
                    "severity": "error",
                    "message": "README.mdがありません。",
                }
            ],
            "recommended_capabilities": ["Template Creator"],
            "human_visual_review_required": True,
            "external_actions_performed": False,
        }
    else:
        result = review(readme)
    print(
        json.dumps(result, ensure_ascii=False, indent=2)
        if args.json
        else result["status"]
    )
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
