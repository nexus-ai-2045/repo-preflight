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
IMAGE_LINK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp")
LINKED_IMAGE_RE = re.compile(r"\[!\[[^\]]*\]\(([^)]+)\)\]\(([^)]+)\)")
LINKED_INLINE_TO_REF_RE = re.compile(r"\[!\[[^\]]*\]\(([^)]+)\)\]\[([^\]]+)\]")
LINKED_REF_TO_INLINE_RE = re.compile(r"\[!\[[^\]]*\]\[([^\]]+)\]\]\(([^)]+)\)")
LINKED_REF_TO_REF_RE = re.compile(r"\[!\[[^\]]*\]\[([^\]]+)\]\]\[([^\]]+)\]")
BARE_IMAGE_RE = re.compile(r"(?<!\[)!\[[^\]]*\]\(([^)]+)\)")
BARE_REF_IMAGE_RE = re.compile(r"(?<!\[)!\[[^\]]*\]\[([^\]]+)\]")
REFERENCE_DEF_RE = re.compile(r"^\[([^\]]+)\]:\s+(\S+)", re.MULTILINE)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
PIP_INSTALL_RE = re.compile(
    r"(?:python(?:3)?[ \t]+-m[ \t]+)?pip3?[ \t]+install\b",
    re.IGNORECASE,
)
DANGER_REVIEW_REQUEST_RE = re.compile(
    r"(?:先に.{0,20})?危険レビュー.{0,24}(?:出せ|して|せよ|しろ|してください)"
    r"|(?:必ず|先に).{0,12}危険レビュー"
)
DANGER_REVIEW_NEGATION_RE = re.compile(r"危険レビュー.{0,16}不要")
GITHUB_REPO_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
)
EVIDENCE_MARKERS = (
    "docs/decisions/",
    "/decisions/",
    "adr-",
    "adr/",
    "tests/",
    "test_",
    "public_ready",
    "security.md",
    "preflight.md",
    "contract",
    "artifacts.md",
)
SAFETY_SUBJECTS = ("write", "visibility", "secret", "unknown")


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
        # block quote 内の fenced code block も Markdown 上は実際の図として描画される。
        # container prefix を外した本文で fence とラベルを判定する。
        content = re.sub(r"^\s*(?:>\s?)+", "", line)
        stripped = content.lstrip()
        match = FENCE_RE.match(content)
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
        labels.extend(_mermaid_line_labels(content, diagram_type))
    # Markdown は閉じ fence がなくても EOF までを fenced code block として扱う。
    if labels is not None:
        diagrams.append(labels)
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
        # 図の有無は review() の "Visualize" 抑止でも使う。判定規則を
        # ここ (FENCE_RE) に一本化し、文字列一致と食い違わせない (review 第 3 巡)
        "diagram_count": len(mermaid_diagrams),
        "diagram_label_count": len(diagram_labels),
    }
    if not japanese_document:
        return findings, recommendations, metrics
    # 可読性検査は当面 warning に留める (review F7)。
    # 下流 repo に opt-out なしの hard block をかけない。ratchet の順序は
    # 測る → 悪化を止める → 全部止める で、いまは第 1 段。運用で誤検知が
    # ないことを確認してから error へ上げる
    readability_severity = "warning"
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
                "severity": readability_severity,
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
                "severity": readability_severity,
                "message": (
                    "図のラベルを本文と同じ言語にしてください。"
                    "識別子のままだと図から意味が読めません。"
                ),
            }
        )
        # "Visualize" は「図が無い」の推薦語として review() で使っている。
        # ここで同じ語を出すとエージェントが作図に走るので、翻訳を促す別語にする (review F10)
        recommendations.add("Localize Diagram")
    return findings, recommendations, metrics


def _headings_outside_fences(lines: list[str]) -> list[tuple[int, str, int]]:
    result: list[tuple[int, str, int]] = []
    for number, line in _outside_fences(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            result.append((len(match.group(1)), match.group(2).strip(), number))
    return result


def _heading_section_body(lines: list[str], aliases: tuple[str, ...]) -> str | None:
    """指定 alias の見出し直後から、同じかより浅い見出しの直前までの本文。

    見出しが無ければ None。見出しはあるが本文が空なら空文字。
    fence 内の見出しは節境界に使わない。
    """
    headings = _headings_outside_fences(lines)
    start_line: int | None = None
    start_level: int | None = None
    end_line = len(lines) + 1
    for level, title, number in headings:
        folded = title.casefold()
        if start_line is None and any(alias in folded for alias in aliases):
            start_line = number
            start_level = level
            continue
        if start_line is not None and start_level is not None and level <= start_level:
            end_line = number
            break
    if start_line is None:
        return None
    return "\n".join(lines[start_line : end_line - 1])


def _is_image_target(target: str) -> bool:
    path = target.split("?", 1)[0].split("#", 1)[0].casefold()
    return path.endswith(IMAGE_LINK_EXT)


def _is_remote_target(target: str) -> bool:
    lowered = target.strip().casefold()
    return lowered.startswith("https://") or lowered.startswith("http://")


def _is_evidence_target(target: str) -> bool:
    if _is_image_target(target):
        return False
    lowered = target.casefold()
    return any(marker in lowered for marker in EVIDENCE_MARKERS)


def _visible_markdown(lines: list[str]) -> str:
    return HTML_COMMENT_RE.sub(
        "", "\n".join(line for _, line in _outside_fences(lines))
    )


def _reference_targets(text: str) -> dict[str, str]:
    return {
        match.group(1).casefold(): match.group(2)
        for match in REFERENCE_DEF_RE.finditer(text)
    }


def _resolve_reference(target: str, refs: dict[str, str]) -> str:
    return refs.get(target.casefold(), target)


def _has_danger_review_request(body: str) -> bool:
    if DANGER_REVIEW_NEGATION_RE.search(body):
        return False
    if DANGER_REVIEW_REQUEST_RE.search(body) is None:
        return False
    if "削除" not in body:
        return False
    lowered = body.casefold()
    return all(token in lowered for token in SAFETY_SUBJECTS)


def _covered_by_span(start: int, spans: list[tuple[int, int]]) -> bool:
    return any(left <= start < right for left, right in spans)


def _figure_pairs(text: str) -> tuple[list[tuple[int, int]], list[tuple[str, str]]]:
    refs = _reference_targets(text)
    linked_spans: list[tuple[int, int]] = []
    linked_pairs: list[tuple[str, str]] = []

    def add_linked(match: re.Match[str], source: str, destination: str) -> None:
        linked_spans.append(match.span())
        linked_pairs.append((source, destination))

    for match in LINKED_IMAGE_RE.finditer(text):
        add_linked(match, match.group(1), match.group(2))
    for match in LINKED_INLINE_TO_REF_RE.finditer(text):
        add_linked(
            match,
            match.group(1),
            _resolve_reference(match.group(2), refs),
        )
    for match in LINKED_REF_TO_INLINE_RE.finditer(text):
        add_linked(
            match,
            _resolve_reference(match.group(1), refs),
            match.group(2),
        )
    for match in LINKED_REF_TO_REF_RE.finditer(text):
        add_linked(
            match,
            _resolve_reference(match.group(1), refs),
            _resolve_reference(match.group(2), refs),
        )
    return linked_spans, linked_pairs


def _ai_paste_contract_findings(
    lines: list[str],
) -> tuple[list[dict[str, object]], set[str]]:
    """公開 README のクイックスタートを、AIへ貼る危険レビュー付きにする契約。

    貼り付け契約は日本語READMEだけ。図の根拠リンクは言語を問わない。
    warning に留め、下流を hard block しない。
    """
    findings: list[dict[str, object]] = []
    recommendations: set[str] = set()

    def warn(code: str, message: str, rec: str) -> None:
        findings.append({"code": code, "severity": "warning", "message": message})
        recommendations.add(rec)

    if _is_japanese_document(lines):
        body = _heading_section_body(
            lines, ("クイックスタート", "quickstart", "quick start")
        )
        if body is not None:
            visible_body = HTML_COMMENT_RE.sub("", body)
            has_paste = GITHUB_REPO_URL_RE.search(visible_body) is not None
            has_pip = PIP_INSTALL_RE.search(visible_body) is not None
            if not has_paste:
                warn(
                    "quickstart_missing_ai_paste",
                    "クイックスタートは人のコマンド手順ではなく、"
                    "AIに貼る GitHub URL を置いてください。",
                    "Paste To AI",
                )
            if has_pip:
                warn(
                    "quickstart_is_command_procedure",
                    "クイックスタートに pip install があります。"
                    "人間が叩く手順ではなく、AIへ貼る文にしてください。",
                    "Paste To AI",
                )
            if has_paste and not _has_danger_review_request(visible_body):
                warn(
                    "quickstart_missing_danger_review",
                    "貼る文に危険レビューを先に出させる指示がありません。"
                    "削除・GitHub write・visibility・secret・unknown を安全と読まないことを書いてください。",
                    "Danger Review Prompt",
                )

    visible = _visible_markdown(lines)
    refs = _reference_targets(visible)
    linked_spans, linked_pairs = _figure_pairs(visible)
    bare_found = False
    for match in BARE_IMAGE_RE.finditer(visible):
        if _covered_by_span(match.start(), linked_spans):
            continue
        if _is_remote_target(match.group(1)):
            continue
        bare_found = True
        break
    if not bare_found:
        for match in BARE_REF_IMAGE_RE.finditer(visible):
            if _covered_by_span(match.start(), linked_spans):
                continue
            source = _resolve_reference(match.group(1), refs)
            if _is_remote_target(source):
                continue
            bare_found = True
            break
    if bare_found:
        warn(
            "figure_not_linked_to_evidence",
            "図は画像ファイル自身ではなく、根拠 (ADR / テスト / 契約文書) へリンクしてください。",
            "Link Figures To Evidence",
        )
    else:
        for source, destination in linked_pairs:
            if _is_remote_target(source):
                continue
            if source == destination or not _is_evidence_target(destination):
                warn(
                    "figure_not_linked_to_evidence",
                    "図のリンク先が根拠ではありません。ADR や再現テストなどへ向けてください。",
                    "Link Figures To Evidence",
                )
                break
    return findings, recommendations


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
    paste_findings, paste_recommendations = _ai_paste_contract_findings(lines)
    findings.extend(paste_findings)
    recommendations |= paste_recommendations

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
    # 図の検出は _mermaid_diagrams と同じ規則 (~~~ fence / 空白入り info string /
    # 入れ子 fence 内の例は除外)。"```mermaid" の部分一致だと Localize Diagram と
    # Visualize が同時に出て F10 の分離が破れる
    has_mermaid = int(ja_metrics["diagram_count"]) > 0
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
