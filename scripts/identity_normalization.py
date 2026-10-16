"""公開repo-preflightの汎用identity正規化・照合の正本。

個人値・account・hook・例外・ファイル配置は持たない。policyはcallerが非公開入力から渡す。
検査対象文字列を実行せず、静的Python literalだけを安全に正規化する。
"""

from __future__ import annotations

from bisect import bisect_right
import re

# 相対path成分を除外し、URI・CLI option直後の絶対pathを維持する。
# CLI optionは一般形で確認し、検出値/hashは絶対path部分だけへ束縛する。
_UNIX_PATH_START = (
    r"(?:(?<![\w./\\-])(?:-{1,2}[A-Za-z][A-Za-z0-9_-]*)?" r"|(?<=(?i:file://)))"
)
# 引用したusernameは非emptyかつ閉じquote直後のseparatorまで検査する。
# rootだけを引用した検査用文字列と、引用を含む実pathを区別する。
_QUOTED_WINDOWS_USER = r"(?:\"[^/\\\"\r\n]+\"(?=[/\\])|'[^/\\'\r\n]+'(?=[/\\]))"
_QUOTED_UNIX_USER = r"(?:\"[^/\"\r\n]+\"(?=/)|'[^/'\r\n]+'(?=/))"
PATH_PATTERNS = (
    re.compile(
        r"[A-Za-z]:[/\\]+Us"
        + r"ers[/\\]+(?:[^/\\\s'\"]+|"
        + _QUOTED_WINDOWS_USER
        + r")"
    ),
    re.compile(
        _UNIX_PATH_START
        + r"(?P<personal_path>/Us"
        + r"ers/(?:[^/\s'\"]+|"
        + _QUOTED_UNIX_USER
        + r"))"
    ),
    re.compile(
        _UNIX_PATH_START
        + r"(?P<personal_path>/ho"
        + r"me/(?:[^/\s'\"]+|"
        + _QUOTED_UNIX_USER
        + r"))"
    ),
)
PATH_RULE_IDS = ("windows_user_path", "macos_user_path", "linux_home_path")


# 互換search APIも同じ文法から派生させ、別の検出文法を持たない。
LOCAL_ABSOLUTE_PATH_PATTERN = re.compile(
    "|".join(
        "(?:" + pattern.pattern.replace("(?P<personal_path>", "(?:") + ")"
        for pattern in PATH_PATTERNS
    )
)


def parse_denylist_text(text: str) -> list[str]:
    """読み取った同一snapshotからprivate照合語を得る。生値は返却以外へ出さない。"""
    patterns = list(
        dict.fromkeys(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    )
    if not patterns:
        raise ValueError("private_identity_policy_empty")
    return patterns


USERNAME_FORM_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
# 明示されたencoded Windows home prefixは、末尾delimiter以降もpathが続く。
# 通常aliasとは型を分け、username例外spanでもそのliteralを免除しない。
ENCODED_HOME_PREFIX_PATTERN = re.compile(
    r"[A-Za-z]-Users-[A-Za-z0-9._-]+-", re.IGNORECASE
)


def hit_patterns(
    text: str,
    patterns: list[str],
    *,
    ignored_username_spans: tuple[tuple[int, int], ...] = (),
) -> list[str]:
    low = text.lower()
    if ignored_username_spans and not text.isascii():
        endpoints = {offset for span in ignored_username_spans for offset in span}
        translated = {0: 0}
        max_endpoint = max(endpoints)
        offset = 0
        for position, char in enumerate(text, 1):
            offset += len(char.lower())
            if position in endpoints:
                translated[position] = offset
            if position == max_endpoint:
                break
        ignored_username_spans = tuple(
            (translated[start], translated[end])
            for start, end in ignored_username_spans
        )
    ignored_username_spans = tuple(sorted(ignored_username_spans))
    starts = tuple(start for start, _ in ignored_username_spans)
    hits = []
    for p in patterns:
        needle = p.lower()
        if USERNAME_FORM_PATTERN.fullmatch(
            p
        ) and not ENCODED_HOME_PREFIX_PATTERN.fullmatch(p):
            matches = re.finditer(
                r"(?<![a-z0-9._-])" + re.escape(needle) + r"(?![a-z0-9._-])", low
            )
            for match in matches:
                index = bisect_right(starts, match.start()) - 1
                if index < 0 or match.end() > ignored_username_spans[index][1]:
                    hits.append(p)
                    break
        elif needle in low:
            hits.append(p)
    return hits


def hit_local_absolute_path(text: str) -> bool:

    return LOCAL_ABSOLUTE_PATH_PATTERN.search(text) is not None


# 既存literal候補抽出と共通の字句。改行はescapeされた場合だけliteral内へ含める。
_QUOTED_LITERAL = r'"(?:[^"\\\r\n]|\\[\s\S])*"|\'(?:[^\'\\\r\n]|\\[\s\S])*\''
_STATIC_LITERAL = r"(?<![\w])(?:[rRuUbBfF]{1,2})?(?:" + _QUOTED_LITERAL + r")"
_RECOVERY_TOKEN = re.compile(
    r"(?P<literal>" + _STATIC_LITERAL + r")"
    r"|(?P<comment>/\*[\s\S]*?\*/|//[^\r\n]*|\#[^\r\n]*)"
    r"|(?P<unterminated_comment>/\*[\s\S]*\Z)"
    r"|(?P<plus>\+)|(?P<space>\s+)|(?P<other>[\s\S])"
)


def _literal_plus_chains(text: str) -> list[tuple[str, str]]:
    """literalを先に字句化し、式間commentだけを評価用token列から外す。"""
    chains = []
    parts = []
    first = last = 0

    def finish():
        complete = parts[:-1] if parts and parts[-1] == "+" else parts
        if len(complete) >= 3:
            chains.append((" ".join(complete), text[first:last]))

    for token in _RECOVERY_TOKEN.finditer(text):
        kind = token.lastgroup
        if kind in {"comment", "space"}:
            continue
        if kind == "unterminated_comment" and len(parts) > 1:
            raise ValueError("private_identity_normalization_incomplete")
        if kind == "literal":
            if not parts or parts[-1] != "+":
                finish()
                parts = []
                first = token.start()
            parts.append(token.group())
            last = token.end()
        elif kind == "plus" and parts and parts[-1] != "+":
            parts.append("+")
        else:
            finish()
            parts = []
    finish()
    return chains


def static_string_views(text: str) -> list[tuple[str, str]]:
    """静的に解釈できる最大のstring式と原文だけを返す。対象コードは実行しない。"""
    import ast
    import copy
    import io
    import textwrap

    fragments = [text]
    fence = None
    block = []
    for line in text.splitlines():
        if fence is None:
            opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$", line)
            if opening:
                fence = opening.group(1)
                block = []
        elif re.fullmatch(
            r"[ \t]{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*",
            line,
        ):
            fragments.append("\n".join(block))
            fence = None
        else:
            block.append(line)
    if fence is not None:
        fragments.append("\n".join(block))

    def string_list(node):
        if isinstance(node, (ast.List, ast.Tuple)) and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in node.elts
        ):
            return [item.value for item in node.elts]
        return None

    def static_string(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = static_string(node.left), static_string(node.right)
            if left is not None and right is not None:
                return left + right
        if (
            isinstance(node, ast.Call)
            and not node.keywords
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and len(node.args) == 1
        ):
            # Python string.join(list) / JS list.join(string) の静的literalだけ。
            separator, values = static_string(node.func.value), string_list(
                node.args[0]
            )
            if separator is not None and values is not None:
                return separator.join(values)
            values, separator = string_list(node.func.value), static_string(
                node.args[0]
            )
            if values is not None and separator is not None:
                return separator.join(values)
        return None

    views = []
    for fragment in fragments:
        fragment = textwrap.dedent(fragment)
        try:
            tree = ast.parse(fragment)
        except (SyntaxError, ValueError):
            # JS/TSや別行の構文エラーで全fileのliteral連結を捨てない。
            # 最大の確定quoted + quoted式だけを既存AST評価へ渡す。
            for expression_source, original in _literal_plus_chains(fragment):
                try:
                    expression = ast.parse("(" + expression_source + "\n)", mode="eval")
                    value = static_string(expression.body)
                except (SyntaxError, ValueError, RecursionError):
                    raise ValueError(
                        "private_identity_normalization_incomplete"
                    ) from None
                if value is not None:
                    views.append((value, original))
            continue
        except RecursionError:
            raise ValueError("private_identity_normalization_incomplete") from None

        # 全sourceをliteralごとに再分割せず、標準APIへ対象の物理行だけ渡す。
        # newline="" はCR/CRLFを保持し、form feed等を行区切りへ変えない。
        source_lines = tuple(io.StringIO(fragment, newline=""))

        def visit(node):
            value = static_string(node)
            if value is not None:
                first = node.lineno - 1
                last = node.end_lineno
                if not 0 <= first < last <= len(source_lines):
                    raise ValueError("private_identity_normalization_incomplete")
                segment_node = copy.copy(node)
                segment_node.lineno = 1
                segment_node.end_lineno -= first
                original = ast.get_source_segment(
                    "".join(source_lines[first:last]), segment_node
                )
                if original is None:
                    raise ValueError("private_identity_normalization_incomplete")
                views.append((value, original))
            else:
                for child in ast.iter_child_nodes(node):
                    visit(child)

        try:
            visit(tree)
        except RecursionError:
            raise ValueError("private_identity_normalization_incomplete") from None
    return views


def _reconstruct_literal_candidates(
    text: str, views: list[tuple[str, str]]
) -> list[str]:
    """静的string-arrayと一文字ずつ区切ったliteralの照合候補。実行結果とは断定しない。"""
    import ast

    literal = _QUOTED_LITERAL
    arrays = re.compile(
        r"\[\s*(?:" + literal + r")(?:\s*,\s*(?:" + literal + r"))*\s*,?\s*\]"
    )
    candidates = []
    values = [value for value, _ in views]
    for match in arrays.finditer(text):
        try:
            pieces = ast.literal_eval(match.group())
        except (SyntaxError, ValueError):
            continue
        if (
            isinstance(pieces, list)
            and pieces
            and all(isinstance(piece, str) for piece in pieces)
        ):
            candidates.append("".join(pieces))
    # JSなどPython全体としてparseできない場合もstring literal単体だけを安全に読む。
    for match in re.finditer(literal, text):
        try:
            value = ast.literal_eval(match.group())
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, str):
            values.append(value)
    for value in values:
        pieces = value.split()
        if len(pieces) >= 4 and all(len(piece) == 1 for piece in pieces):
            candidates.append("".join(pieces))
    return candidates


def scan_private_text(text: str, patterns: list[str]) -> dict:
    """既存identity規則で公開bytesを検査し、値を含まない結果を返す。"""
    if not patterns or not all(
        isinstance(pattern, str) and pattern.strip() for pattern in patterns
    ):
        raise ValueError("private_identity_policy_empty")
    views = static_string_views(text)
    # bare arrayの連結・空白除去は既知policy照合だけに使い、任意pathの意味まで推測しない。
    variants = [
        text,
        *(value for value, _ in views),
        *_reconstruct_literal_candidates(text, views),
    ]
    hits = {pattern for value in variants for pattern in hit_patterns(value, patterns)}
    path_match = hit_local_absolute_path(text) or any(
        hit_local_absolute_path(value) for value, _ in views
    )
    reconstructed_path = any(
        hit_local_absolute_path(value) and value not in original
        for value, original in views
    )
    return {
        "status": "fail" if hits or path_match else "pass",
        "literal_match_count": len(hits),
        "local_path_match": path_match,
        "reconstructed_path_match": reconstructed_path,
    }
