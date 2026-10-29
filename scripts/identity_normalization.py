"""公開repo-preflightの汎用identity正規化・照合の正本。

個人値・account・hook・例外・ファイル配置は持たない。policyはcallerが非公開入力から渡す。
検査対象文字列を実行せず、静的Python literalだけを安全に正規化する。
"""

from __future__ import annotations

from bisect import bisect_right
import re
from urllib.parse import unquote

# 相対path成分を除外し、URI・CLI option直後の絶対pathを維持する。
# CLI optionは一般形で確認し、検出値/hashは絶対path部分だけへ束縛する。
_UNIX_PATH_START = (
    r"(?:(?<![\w./\\-])(?:-{1,2}[A-Za-z][A-Za-z0-9_-]*)?" r"|(?<=(?i:file://)))"
)
# 引用したusernameは非emptyかつ閉じquote直後のseparatorまで検査する。
# rootだけを引用した検査用文字列と、引用を含む実pathを区別する。
_QUOTED_WINDOWS_USER = r"(?:\"[^/\\\"\r\n]+\"(?=[/\\])|'[^/\\'\r\n]+'(?=[/\\]))"
_QUOTED_UNIX_USER = r"(?:\"[^/\"\r\n]+\"(?=/)|'[^/'\r\n]+'(?=/))"
from pathlib import PurePosixPath

MACOS_SHARED_DIRECTORY = PurePosixPath("/", "Users", "Shared").as_posix()
_MACOS_SHARED_COMPONENT = MACOS_SHARED_DIRECTORY.rsplit("/", 1)[-1]

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
        + r"ers/"
        + rf"(?!{re.escape(_MACOS_SHARED_COMPONENT)}(?:/|$))"
        + r"(?:[^/\s'\"]+|"
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


_SHARED_REFERENCE_PATTERN = re.compile(
    _UNIX_PATH_START
    + "(?P<personal_path>"
    + re.escape(MACOS_SHARED_DIRECTORY)
    + ")(?=/|$)"
)
_PARENT_COMPONENT_PATTERN = re.compile(r"[/\\]\.\.(?=[/\\]|$)")


def macos_shared_parent_matches(value, *, entire_path=False):
    """同じ参照の正規化結果だけでSharedの外へ戻るか判定する。"""
    import posixpath

    binary = isinstance(value, bytes)
    shared = (
        re.compile(_SHARED_REFERENCE_PATTERN.pattern.encode("ascii"))
        if binary
        else _SHARED_REFERENCE_PATTERN
    )
    text = value.decode("latin-1") if binary else value
    results = []
    consumed = 0
    for match in shared.finditer(value):
        start = match.start("personal_path")
        if start < consumed:
            continue
        whole = entire_path and (start == 0 or text[:start] in ("file:", "file://"))
        end = start
        quote_char = text[start - 1] if start and text[start - 1] in "\"'`" else None
        if whole:
            end = len(text)
        elif quote_char:
            end = start
            while end < len(text):
                if text[end] == quote_char and (end == 0 or text[end - 1] != "\\"):
                    break
                end += 1
        else:
            while (
                end < len(text)
                and not text[end].isspace()
                and text[end] not in "\"'`<>"
            ):
                end += 1
        consumed = max(end, match.end())
        reference = text[start:end]
        normalized = posixpath.normpath(reference)
        if normalized != MACOS_SHARED_DIRECTORY and not normalized.startswith(
            MACOS_SHARED_DIRECTORY + "/"
        ):
            results.append(match)
    return results


def iter_private_path_matches(value):
    for rule, pattern in zip(PATH_RULE_IDS, PATH_PATTERNS):
        if isinstance(value, bytes):
            pattern = re.compile(pattern.pattern.encode("ascii"))
        for match in pattern.finditer(value):
            yield rule, match
    for match in macos_shared_parent_matches(value):
        yield "macos_user_path", match


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
        if needle not in low:
            continue
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

    return LOCAL_ABSOLUTE_PATH_PATTERN.search(text) is not None or bool(
        macos_shared_parent_matches(text)
    )


# 既存literal候補抽出と共通の字句。改行はescapeされた場合だけliteral内へ含める。
_QUOTED_LITERAL = r'"(?:[^"\\\r\n]|\\[\s\S])*"|\'(?:[^\'\\\r\n]|\\[\s\S])*\''
_STATIC_LITERAL = r"(?<![\w])(?:[rRuUbBfF]{1,2})?(?:" + _QUOTED_LITERAL + r")"
_RECOVERY_NON_LITERAL_SOURCE = (
    r"(?P<comment>/\*[\s\S]*?\*/|//[^\r\n]*|\#[^\r\n]*)"
    r"|(?P<unterminated_comment>/\*[\s\S]*\Z)"
    r"|(?P<plus>\+)|(?P<open>\()|(?P<close>\))"
    r"|(?P<identifier>[\w$]+)|(?P<space>\s+)|(?P<other>[\s\S])"
)
_RECOVERY_NON_LITERAL = re.compile(_RECOVERY_NON_LITERAL_SOURCE)
_RECOVERY_TOKEN = re.compile(
    r"(?P<literal>" + _STATIC_LITERAL + r")|" + _RECOVERY_NON_LITERAL_SOURCE
)


def _closed_quote_starts(text: str) -> set[int]:
    """既存literal文法で終端quoteへ到達できる開始位置を一度だけ計算する。"""
    starts = set()
    # next/after は各quote文法で、次位置/その次位置から最初に止まる位置。
    # backslashは改行を含め次の1文字を消費する。quote/未escape改行で止まる。
    following = [-1, -1]
    after = [-1, -1]
    for index in range(len(text) - 1, -1, -1):
        char = text[index]
        for kind, quote in enumerate(('"', "'")):
            next_stop = following[kind]
            if char == quote and next_stop >= 0 and text[next_stop] == quote:
                starts.add(index)
            if char == "\\":
                stop = after[kind]
            elif char == quote or char in "\r\n":
                stop = index
            else:
                stop = next_stop
            after[kind], following[kind] = next_stop, stop
    return starts


def _recovery_tokens(text: str):
    """同じtoken文法を、閉じない引用の終端探索を繰り返さずに読む。"""
    closed = _closed_quote_starts(text)
    position = 0
    while position < len(text):
        # prefixは既存文法と同じ0〜2文字。無効なprefixやword境界はregex側が判定する。
        has_literal_end = position in closed
        for offset in (1, 2):
            if text[position : position + offset].strip("rRuUbBfF"):
                break
            has_literal_end |= position + offset in closed
        token = (_RECOVERY_TOKEN if has_literal_end else _RECOVERY_NON_LITERAL).match(
            text, position
        )
        yield token
        position = token.end()


def _quoted_literals(text: str):
    """quote-only文法も、元々閉じない候補だけを省いて同じspanを返す。"""
    closed = _closed_quote_starts(text)
    literal = re.compile(_QUOTED_LITERAL)
    position = 0
    while position < len(text):
        if position not in closed:
            position += 1
            continue
        match = literal.match(text, position)
        yield match
        position = match.end()


def _needs_foreign_literal_recovery(fragment: str) -> bool:
    """回復本体と同じtoken列でliteralの直後のplusだけを探す。"""
    if "+" not in fragment or ("'" not in fragment and '"' not in fragment):
        return False
    after_literal = False
    for token in _recovery_tokens(fragment):
        kind = token.lastgroup
        if kind == "literal":
            after_literal = True
        elif kind not in {"space", "comment", "close"}:
            if kind == "plus" and after_literal:
                return True
            after_literal = False
    return False


def _literal_plus_chains(
    text: str, *, token_source=None, source_spans=None, issues=None
) -> list[tuple[str, str]]:
    """完全staticな括弧だけを連結へ戻し、callや動的operandの境界を越えない。"""
    tokens = [
        token
        for token in (_recovery_tokens(text) if token_source is None else token_source)
        if token.lastgroup not in {"comment", "space"}
    ]
    ranges = []

    def frame(opening=None, bubble=True):
        return {
            "opening": opening,
            "bubble": bubble,
            "static": True,
            "first": None,
            "last": None,
            "literals": 0,
            "pluses": 0,
            "expects_term": True,
        }

    def finish(current):
        if current["literals"] >= 2 and current["pluses"]:
            ranges.append((current["first"], current["last"]))
        current.update(first=None, last=None, literals=0, pluses=0, expects_term=True)

    def term(current, first, last, literals, pluses):
        if not current["expects_term"]:
            finish(current)
            current["static"] = False
        if current["first"] is None:
            current["first"] = first
        current["last"] = last
        current["literals"] += literals
        current["pluses"] += pluses
        current["expects_term"] = False

    stack = [frame()]
    previous = None
    for index, token in enumerate(tokens):
        current = stack[-1]
        kind = token.lastgroup
        if (
            kind == "unterminated_literal"
            and current["pluses"]
            and current["expects_term"]
        ):
            if issues is None:
                raise ValueError("private_identity_normalization_incomplete")
            issues.append(
                {
                    "kind": kind,
                    "start": tokens[current["first"]].start(),
                    "end": token.end(),
                }
            )
            # 未完了の範囲を越えて連結しない。既に完了したrangesは保持する。
            for pending in stack:
                pending.update(
                    first=None,
                    last=None,
                    literals=0,
                    pluses=0,
                    expects_term=True,
                    static=False,
                    bubble=False,
                )
            previous = token
            continue
        if kind == "unterminated_comment" and any(item["pluses"] for item in stack):
            raise ValueError("private_identity_normalization_incomplete")
        if kind == "literal":
            term(current, index, index, 1, 0)
        elif kind == "plus" and not current["expects_term"]:
            current["pluses"] += 1
            current["expects_term"] = True
        elif kind == "open":
            # f("a" + "b") + "c" や !("a" + "b") + "c" をabcにしない。
            blocked_prefix = previous is not None and (
                previous.lastgroup in {"literal", "close"}
                or (
                    previous.lastgroup == "identifier"
                    and (
                        previous.group() not in {"return", "throw", "case"}
                        or (index > 1 and tokens[index - 2].group() == ".")
                    )
                )
                or (
                    previous.lastgroup == "other"
                    and previous.group()
                    in {".", "]", "}", "!", "~", "-", "*", "/", "%"}
                )
                or (previous.lastgroup == "plus" and current["first"] is None)
            )
            bubble = current["expects_term"] and not blocked_prefix
            if not bubble:
                finish(current)
                current["static"] = False
            stack.append(frame(index, bubble))
        elif kind == "close" and len(stack) > 1:
            group = stack.pop()
            parent = stack[-1]
            if group["bubble"] and group["static"] and not group["expects_term"]:
                term(
                    parent,
                    group["opening"],
                    index,
                    group["literals"],
                    group["pluses"],
                )
            else:
                finish(group)
                finish(parent)
                parent["static"] = False
        else:
            if (
                kind == "other"
                and token.group() in {";", "}"}
                and current["opening"] is not None
                and current["bubble"]
                and current["static"]
                and current["pluses"]
            ):
                raise ValueError("private_identity_normalization_incomplete")
            finish(current)
            current["static"] = False
        previous = token
    if any(item["bubble"] and item["static"] and item["pluses"] for item in stack[1:]):
        raise ValueError("private_identity_normalization_incomplete")
    for current in reversed(stack):
        finish(current)
    if source_spans is not None:
        source_spans.extend(
            (tokens[first].start(), tokens[last].end()) for first, last in ranges
        )
    return [
        (
            " ".join(token.group() for token in tokens[first : last + 1]),
            text[tokens[first].start() : tokens[last].end()],
        )
        for first, last in ranges
    ]


_POWERSHELL_SINGLE_LITERAL = r"'(?:[^']|'')*'"
_POWERSHELL_RECOVERY_TOKEN = re.compile(
    r"(?P<literal>" + _POWERSHELL_SINGLE_LITERAL + r'|"(?:[^"\\\r\n]|\\[\s\S])*")'
    r"|(?P<unterminated_literal>')|" + _RECOVERY_NON_LITERAL_SOURCE
)
_POWERSHELL_RECOVERY_NON_LITERAL = re.compile(
    r"(?P<unterminated_literal>')|" + _RECOVERY_NON_LITERAL_SOURCE
)


def _powershell_recovery_tokens(text: str):
    """単一引用符ではbackslashを消費しない。閉じないquoteの終端探索は一度だけ。"""
    closed = _closed_quote_starts(text)
    last_quote = text.rfind("'")
    position = 0
    while position < len(text):
        cannot_close = (text[position] == "'" and position == last_quote) or (
            text[position] == '"' and position not in closed
        )
        token = (
            _POWERSHELL_RECOVERY_NON_LITERAL
            if cannot_close
            else _POWERSHELL_RECOVERY_TOKEN
        ).match(text, position)
        yield token
        position = token.end()


def _powershell_single_chain_value(expression: str) -> str | None:
    """完全staticな単一引用符の連結だけを読む。評価・補間は行わない。"""
    pieces = []
    depth = 0
    expects_term = True
    for token in _powershell_recovery_tokens(expression):
        kind = token.lastgroup
        if kind in {"space", "comment"}:
            continue
        if kind == "open" and expects_term:
            depth += 1
        elif kind == "close" and not expects_term and depth:
            depth -= 1
        elif kind == "plus" and not expects_term:
            expects_term = True
        elif kind == "literal" and token.group().startswith("'") and expects_term:
            pieces.append(token.group()[1:-1].replace("''", "'"))
            expects_term = False
        else:
            return None
    return (
        "".join(pieces) if len(pieces) >= 2 and not expects_term and not depth else None
    )


def _powershell_single_string_views(
    text: str, *, token_source=None, issues=None
) -> list[tuple[str, str]]:
    if "+" not in text or "'" not in text:
        return []
    views = []
    for expression, original in _literal_plus_chains(
        text,
        token_source=(
            _powershell_recovery_tokens(text) if token_source is None else token_source
        ),
        issues=issues,
    ):
        value = _powershell_single_chain_value(expression)
        if value is not None:
            views.append((value, original))
    return views


def _source_line_utf8(line: str) -> bytes:
    """静的文字列の列範囲を切るための1行UTF-8。呼び出しの外には残さない。"""
    return line.encode()


def _standard_regex_arguments(tree) -> set[int]:
    """Recognize syntax roles, never import or execute the inspected source."""
    import ast

    if not any(
        isinstance(node, ast.Import)
        and any(alias.name == "re" and alias.asname is None for alias in node.names)
        for node in tree.body
    ):
        return set()
    parents = {
        id(child): node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "re":
            parent = parents.get(id(node))
            if not isinstance(node.ctx, ast.Load) or not isinstance(
                parent, ast.Attribute
            ):
                return set()
        if isinstance(node, ast.arg) and node.arg == "re":
            return set()
        if isinstance(node, (ast.Global, ast.Nonlocal)) and "re" in node.names:
            return set()
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == "re"
        ):
            return set()
        if isinstance(node, ast.ExceptHandler) and node.name == "re":
            return set()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*" or (
                    isinstance(node, ast.Import)
                    and alias.name == "re"
                    and alias.asname is not None
                ):
                    return set()
                bound = alias.asname or alias.name.split(".")[0]
                if bound == "re" and not (
                    isinstance(node, ast.Import)
                    and alias.name == "re"
                    and alias.asname is None
                ):
                    return set()
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            root = node
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if (
                isinstance(root, ast.Name)
                and root.id == "re"
                and (
                    not isinstance(node.ctx, ast.Load)
                    or isinstance(node, ast.Attribute)
                    and node.attr.startswith("__")
                )
            ):
                return set()
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"exec", "eval", "globals", "locals", "vars"}
        ):
            return set()
        # Pattern captures are bindings too; these node classes do not exist on 3.9.
        if type(node).__name__ in {"MatchAs", "MatchStar", "MatchMapping"} and (
            getattr(node, "name", None) == "re" or getattr(node, "rest", None) == "re"
        ):
            return set()
    return {
        id(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
        and node.func.attr == "compile"
        and len(node.args) == 1
        and not node.keywords
    }


_REGEX_HOME_ROOT = re.compile(r"(?:[A-Za-z]:[/\\]+Users[/\\]+|/(?:Users|home)/)")
_REGEX_USERNAME_CLASSES = (r"[^/\s]+", r"[^\\\s]+", r"[A-Za-z0-9._-]+")
_REGEX_USERNAME_LOOKAHEAD = r"(?!USER\b|<)"


def _generic_regex_path_spans(
    value: str, *, source_token: bool = False
) -> list[tuple[int, int]]:
    """Only supported generic username atoms, never a concrete username prefix."""
    spans = []
    forms = (False, True) if source_token else (False,)
    for match in LOCAL_ABSOLUTE_PATH_PATTERN.finditer(value):
        root = _REGEX_HOME_ROOT.match(value, match.start())
        if root is None:
            continue
        for escaped in forms:

            def spelling(token):
                return token.replace("\\", "\\\\") if escaped else token

            start = root.end()
            lookahead = spelling(_REGEX_USERNAME_LOOKAHEAD)
            if value.startswith(lookahead, start):
                start += len(lookahead)
            for generic in _REGEX_USERNAME_CLASSES:
                token = spelling(generic)
                if not value.startswith(token, start):
                    continue
                end = start + len(token)
                tail = value[end:]
                boundary = not tail or tail[0] in "/\\|)$" or tail.startswith(r"\Z")
                if source_token and tail[:1] in {"'", '"'}:
                    boundary = True
                if boundary:
                    spans.append((match.start(), end))
    return spans


def _mask_path_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    # Preserve boundaries: deleting a template must not join unrelated path pieces.
    chars = list(text)
    for first, last in spans:
        chars[first:last] = " " * (last - first)
    return "".join(chars)


def _regex_source_token_spans(
    original: str, source_start: int
) -> list[tuple[int, int]]:
    """Mask template syntax inside string tokens, preserving intervening comments."""
    import ast
    import io
    import tokenize

    lines = tuple(io.StringIO(original, newline=""))
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    spans = []
    try:
        for token in tokenize.generate_tokens(
            io.StringIO(original, newline="").readline
        ):
            if token.type != tokenize.STRING:
                continue
            decoded = ast.literal_eval(token.string)
            if not isinstance(decoded, str) or not _generic_regex_path_spans(decoded):
                continue
            offset = source_start + offsets[token.start[0] - 1] + token.start[1]
            spans.extend(
                (offset + first, offset + last)
                for first, last in _generic_regex_path_spans(
                    token.string, source_token=True
                )
            )
    except (SyntaxError, ValueError, tokenize.TokenError, IndentationError):
        return []
    return spans


def _static_string_views(
    text: str, regex_roles: list[dict] | None = None, *, parsed_tree=None
) -> list[tuple[str, str]]:
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

    def string_list(node, *, regex_expression=False):
        if isinstance(node, (ast.List, ast.Tuple)):
            if regex_expression:
                values = [
                    static_string(item, regex_expression=True) for item in node.elts
                ]
                return values if all(value is not None for value in values) else None
            if all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in node.elts
            ):
                return [item.value for item in node.elts]
        return None

    def static_string(node, *, regex_expression=False):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = static_string(
                node.left, regex_expression=regex_expression
            ), static_string(node.right, regex_expression=regex_expression)
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
            separator, values = static_string(
                node.func.value, regex_expression=regex_expression
            ), string_list(node.args[0], regex_expression=regex_expression)
            if separator is not None and values is not None:
                return separator.join(values)
            values, separator = string_list(
                node.func.value, regex_expression=regex_expression
            ), static_string(node.args[0], regex_expression=regex_expression)
            if values is not None and separator is not None:
                return separator.join(values)
        return None

    views = []
    seen_views = set()
    seen_view_count = 0
    for fragment_index, fragment in enumerate(fragments):
        fragment = textwrap.dedent(fragment)

        def append_powershell_views():
            nonlocal seen_view_count
            if not powershell_views:
                return
            seen_views.update(views[seen_view_count:])
            for value, original in powershell_views:
                if (value, original) in seen_views:
                    continue
                if regex_roles is not None and any(
                    "pattern" in role
                    and role["pattern_spans"]
                    and original in role["source"]
                    for role in regex_roles
                ):
                    regex_roles.append(
                        {
                            "index": len(views),
                            "value_spans": _generic_regex_path_spans(
                                value, source_token=True
                            ),
                            "source_spans": [],
                        }
                    )
                views.append((value, original))
                seen_views.add((value, original))
            seen_view_count = len(views)

        try:
            tree = (
                parsed_tree
                if parsed_tree is not None and fragment_index == 0 and fragment == text
                else ast.parse(fragment)
            )
        except (SyntaxError, ValueError):
            # JS/TSや別行の構文エラーで、引用を連結した式だけを既存AST評価へ渡す。
            # 連結のない文章は文字単位の回復解析をしない。
            recovered_spans = []
            incompatible_spans = []
            if _needs_foreign_literal_recovery(fragment):
                source_spans = []
                chains = _literal_plus_chains(fragment, source_spans=source_spans)
                for (expression_source, original), span in zip(chains, source_spans):
                    try:
                        expression_text = "(" + expression_source + "\n)"
                        expression = ast.parse(expression_text, mode="eval")
                        value = static_string(expression.body)
                    except (SyntaxError, ValueError, RecursionError):
                        value = _powershell_single_chain_value(expression_source)
                        if value is None:
                            raise ValueError(
                                "private_identity_normalization_incomplete"
                            ) from None
                        views.append((value, original))
                    else:
                        if value is not None:
                            # 証明済みのPython式ごとに同じAST検査を使い、原文へ束縛する。
                            views.extend(
                                (candidate, original)
                                for candidate, _ in _static_string_views(
                                    expression_text, parsed_tree=expression
                                )
                            )
                            recovered_spans.append(span)
                            try:
                                _powershell_single_string_views(expression_text)
                            except ValueError as error:
                                if (
                                    str(error)
                                    != "private_identity_normalization_incomplete"
                                ):
                                    raise
                                incompatible_spans.append(span)
            # 証明済みの式は上の再帰で別解釈まで検査済み。同じ長さの空白へ
            # 置換し、互換性のない引用符が後続の別式を飲み込むことを防ぐ。
            remaining_fragment = _mask_path_spans(fragment, incompatible_spans)
            powershell_tokens = (
                list(_powershell_recovery_tokens(remaining_fragment))
                if "+" in remaining_fragment and "'" in remaining_fragment
                else []
            )
            issues = []
            powershell_views = _powershell_single_string_views(
                fragment, token_source=powershell_tokens, issues=issues
            )
            for issue in issues:
                covered = any(
                    start <= issue["start"]
                    and issue["end"] <= end
                    and not any(
                        token.lastgroup
                        in {"literal", "comment", "unterminated_comment"}
                        and (
                            token.start() < start < token.end()
                            or token.start() < end < token.end()
                        )
                        for token in powershell_tokens
                    )
                    for start, end in recovered_spans
                )
                if not covered:
                    raise ValueError("private_identity_normalization_incomplete")
            append_powershell_views()
            continue
        except RecursionError:
            raise ValueError("private_identity_normalization_incomplete") from None

        regex_arguments = (
            _standard_regex_arguments(tree)
            if regex_roles is not None and fragment_index == 0 and fragment == text
            else set()
        )

        regex_nodes = set()
        for argument in ast.walk(tree) if regex_arguments else ():
            if id(argument) not in regex_arguments:
                continue
            try:
                pattern = static_string(argument, regex_expression=True)
            except RecursionError:
                continue
            if pattern is None or len(pattern) > 65536:
                continue
            try:
                re.compile(pattern)
            except (re.error, ValueError, RecursionError, OverflowError):
                continue
            # The role validator may reconstruct across join items that the public
            # tuple API intentionally keeps separate. Scan that complete value too.
            for normalized in dict.fromkeys((pattern, unquote(pattern))):
                try:
                    re.compile(normalized)
                except (re.error, ValueError, RecursionError, OverflowError):
                    spans = []
                else:
                    spans = _generic_regex_path_spans(normalized)
                regex_roles.append(
                    {
                        "pattern": normalized,
                        "pattern_spans": spans,
                        "source_spans": [],
                        "source": ast.get_source_segment(fragment, argument)
                        or fragment,
                    }
                )
            regex_nodes.update(id(node) for node in ast.walk(argument))

        # 単一行はUTF-8を行ごとに一度だけ作り、stdlibと同じbyte offsetで切る。
        # 複数行・位置が欠けたnodeは従来どおり対象行だけを標準APIへ渡す。
        # newline="" はCR/CRLFを保持し、form feed等を行区切りへ変えない。
        source_lines = tuple(io.StringIO(fragment, newline=""))
        line_utf8_cache: list[bytes | None] = [None] * len(source_lines)
        source_offsets = [0]
        if regex_nodes:
            for line in source_lines:
                source_offsets.append(source_offsets[-1] + len(line))

        def encoded_line(index: int) -> bytes:
            cached = line_utf8_cache[index]
            if cached is None:
                cached = _source_line_utf8(source_lines[index])
                line_utf8_cache[index] = cached
            return cached

        node_sources = {}

        def source_for(node):
            if id(node) in node_sources:
                return node_sources[id(node)]
            single_line = (
                node.end_lineno == node.lineno
                and node.col_offset is not None
                and node.end_col_offset is not None
                and isinstance(node.lineno, int)
                and 1 <= node.lineno <= len(source_lines)
            )
            if single_line:
                original = encoded_line(node.lineno - 1)[
                    node.col_offset : node.end_col_offset
                ].decode()
            else:
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
            node_sources[id(node)] = original
            return original

        def alternate_roots(node):
            # 一度のpostorderで適用可能なAddを決め、重ならない最大範囲だけを読む。
            # staticなCallの内部も調べるが、親子のAdd全てを再解析しない。
            order = list(ast.walk(node))
            eligible = {}
            parents = {}
            for item in reversed(order):
                for child in ast.iter_child_nodes(item):
                    parents[id(child)] = item
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    eligible[id(item)] = bool(
                        re.fullmatch(_POWERSHELL_SINGLE_LITERAL, source_for(item))
                    )
                elif isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add):
                    eligible[id(item)] = eligible.get(
                        id(item.left), False
                    ) and eligible.get(id(item.right), False)
            return (
                item
                for item in order
                if isinstance(item, ast.BinOp)
                and eligible.get(id(item), False)
                and not eligible.get(id(parents.get(id(item))), False)
            )

        def visit(node):
            value = static_string(node)
            if value is not None:
                original = source_for(node)
                if id(node) in regex_nodes:
                    path_spans = _generic_regex_path_spans(value)
                    if path_spans:
                        source_start = source_offsets[node.lineno - 1]
                        source_start += len(
                            encoded_line(node.lineno - 1)[: node.col_offset].decode()
                        )
                        regex_roles.append(
                            {
                                "index": len(views),
                                "value_spans": path_spans,
                                "source_spans": _regex_source_token_spans(
                                    original, source_start
                                ),
                            }
                        )
                views.append((value, original))
                # 有効なPythonではASTが確定した式の範囲だけを別解釈する。
                # 無関係な式の引用符が、この式の検査候補を消すことはない。
                for root in (
                    alternate_roots(node) if "+" in original and "'" in original else ()
                ):
                    alternate_source = source_for(root)
                    alternate = _powershell_single_chain_value(alternate_source)
                    if alternate is not None and alternate != value:
                        if id(root) in regex_nodes:
                            path_spans = _generic_regex_path_spans(
                                alternate, source_token=True
                            )
                            if path_spans:
                                regex_roles.append(
                                    {
                                        "index": len(views),
                                        "value_spans": path_spans,
                                        "source_spans": [],
                                    }
                                )
                        views.append((alternate, alternate_source))
            else:
                for child in ast.iter_child_nodes(node):
                    visit(child)

        try:
            visit(tree)
        except RecursionError:
            raise ValueError("private_identity_normalization_incomplete") from None
    return views


def static_string_views(text: str) -> list[tuple[str, str]]:
    """Keep the public value/source tuple API; syntax-role metadata stays internal."""
    return _static_string_views(text)


# 一文字区切りの引用の必要条件。後段は literal_eval の値を split() し、1 文字の欠片が
# 4 つ以上なら候補にする。欠片の間の区切りは、元の文字では空白かエスケープ (\\) として
# 必ず現れるので、空白か \\ が 3 つ以上ある引用だけを復元すれば取りこぼさない。
# 後段の書き方を正規表現で真似ると、エスケープ・Unicode の空白・反対側の引用符などで
# 前段と後段がずれ、符号化した非公開の識別子が素通りする (PR #15 のレビュー指摘)。
_SPACED_SEPARATOR_HINT = re.compile(r"[\s\\]")
_STRING_ARRAY_START = re.compile(r"""\[\s*['"]""")


def _reconstruct_literal_candidates(
    text: str,
    views: list[tuple[str, str]],
    *,
    provenance: list[tuple[str, str]] | None = None,
) -> list[str]:
    """静的string-arrayと一文字ずつ区切ったliteralの照合候補。実行結果とは断定しない。"""
    import ast

    literal = _QUOTED_LITERAL
    candidates = []
    values = list(views)
    # 配列は開き括弧の直後に引用がある時だけ探す。引用は区切りの必要条件で絞ってから復元する。
    if _STRING_ARRAY_START.search(text):
        arrays = re.compile(
            r"\[\s*(?:" + literal + r")(?:\s*,\s*(?:" + literal + r"))*\s*,?\s*\]"
        )
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
                candidate = "".join(pieces)
                candidates.append(candidate)
                if provenance is not None:
                    provenance.append((candidate, match.group()))
    # 閉じないquoteの終端を別のgateで再探索せず、同じ有界iteratorで候補を読む。
    for match in _quoted_literals(text):
        source = match.group()
        if len(_SPACED_SEPARATOR_HINT.findall(source)) < 3:
            continue
        try:
            value = ast.literal_eval(source)
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, str):
            values.append((value, source))
    for value, original in values:
        pieces = value.split()
        if len(pieces) >= 4 and all(len(piece) == 1 for piece in pieces):
            candidate = "".join(pieces)
            candidates.append(candidate)
            if provenance is not None:
                provenance.append((candidate, original))
    return candidates


def _private_text_analysis(text: str) -> dict:
    regex_roles: list[dict] = []
    views = _static_string_views(text, regex_roles)
    reconstructed: list[tuple[str, str]] = []
    _reconstruct_literal_candidates(text, views, provenance=reconstructed)
    literals = [
        *views,
        *reconstructed,
        *(
            (role["pattern"], role["source"])
            for role in regex_roles
            if "pattern" in role
        ),
    ]
    source_spans = [span for role in regex_roles for span in role["source_spans"]]
    value_spans = {
        role["index"]: role["value_spans"] for role in regex_roles if "index" in role
    }
    paths = [
        (
            _mask_path_spans(value, value_spans.get(index, [])),
            original,
            value not in original,
        )
        for index, (value, original) in enumerate(views)
    ]
    paths.extend(
        (
            _mask_path_spans(role["pattern"], role["pattern_spans"]),
            role["source"],
            role["pattern"] not in text,
        )
        for role in regex_roles
        if "pattern" in role
    )
    return {
        "literal_values": literals,
        "path_values": paths,
        "path_source": _mask_path_spans(text, source_spans),
    }


def _validate_private_patterns(patterns):
    if not patterns or not all(
        isinstance(pattern, str) and pattern.strip() for pattern in patterns
    ):
        raise ValueError("private_identity_policy_empty")


def _private_text_result(literals, paths, patterns):
    _validate_private_patterns(patterns)
    hits = {pattern for value in literals for pattern in hit_patterns(value, patterns)}
    path_match = any(hit_local_absolute_path(value) for value, _ in paths)
    reconstructed = any(
        flag and hit_local_absolute_path(value) for value, flag in paths
    )
    return {
        "status": "fail" if hits or path_match else "pass",
        "literal_match_count": len(hits),
        "local_path_match": path_match,
        "reconstructed_path_match": reconstructed,
    }


def scan_private_text(text: str, patterns: list[str]) -> dict:
    """既存identity規則で公開bytesを検査し、値を含まない結果を返す。"""
    _validate_private_patterns(patterns)
    analysis = _private_text_analysis(text)
    return _private_text_result(
        [text, *(value for value, _ in analysis["literal_values"])],
        [
            (analysis["path_source"], False),
            *(
                (value, reconstructed)
                for value, _, reconstructed in analysis["path_values"]
            ),
        ],
        patterns,
    )


def private_text_delta_views(previous: str, text: str) -> dict:
    import difflib

    old_lines, new_lines = previous.splitlines(keepends=True), text.splitlines(
        keepends=True
    )
    prefix = 0
    common_limit = min(len(old_lines), len(new_lines))
    while prefix < common_limit and old_lines[prefix] == new_lines[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < common_limit - prefix
        and old_lines[len(old_lines) - suffix - 1]
        == new_lines[len(new_lines) - suffix - 1]
    ):
        suffix += 1
    old_end, new_end = len(old_lines) - suffix, len(new_lines) - suffix
    old_middle, new_middle = old_lines[prefix:old_end], new_lines[prefix:new_end]
    if (
        len(previous) + len(text) > 2_000_000
        or len(old_middle) * len(new_middle) > 16_000_000
    ):
        after = _private_text_analysis(text)
        return {
            "literal_values": [text, *(value for value, _ in after["literal_values"])],
            "path_values": [
                (after["path_source"], False),
                *((value, flag) for value, _, flag in after["path_values"]),
            ],
            "full_fallback": True,
        }
    before, after = _private_text_analysis(previous), _private_text_analysis(text)
    offsets = [0]
    for line in new_lines:
        offsets.append(offsets[-1] + len(line))
    operations = [
        (operation, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix)
        for operation, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=old_middle, b=new_middle, autojunk=False
        ).get_opcodes()
    ]
    if prefix:
        operations.insert(0, ("equal", 0, prefix, 0, prefix))
    if suffix:
        operations.append(("equal", old_end, len(old_lines), new_end, len(new_lines)))
    equal_ranges = [
        (offsets[j1], offsets[j2]) for op, _, _, j1, j2 in operations if op == "equal"
    ]
    changed_ranges = [
        (offsets[j1], offsets[j2])
        for op, _, _, j1, j2 in operations
        if op in {"insert", "replace"}
    ]

    source_unchanged_cache: dict[str, bool] = {}

    def unchanged(candidate, old_candidates):
        value, source = candidate[:2]
        if (value, source) not in old_candidates or not source:
            return False
        if source in source_unchanged_cache:
            return source_unchanged_cache[source]
        found, offset = False, 0
        while True:
            position = text.find(source, offset)
            if position < 0:
                source_unchanged_cache[source] = found
                return found
            found = True
            end = position + len(source)
            if not any(
                first <= position and end <= last for first, last in equal_ranges
            ):
                source_unchanged_cache[source] = False
                return False
            offset = position + 1

    old_literals = set(before["literal_values"])
    old_paths = {(value, source) for value, source, _ in before["path_values"]}
    return {
        "literal_values": [
            *(text[first:last] for first, last in changed_ranges),
            *(
                value
                for value, source in after["literal_values"]
                if not unchanged((value, source), old_literals)
            ),
        ],
        "path_values": [
            *(
                (after["path_source"][first:last], False)
                for first, last in changed_ranges
            ),
            *(
                (value, reconstructed)
                for value, source, reconstructed in after["path_values"]
                if not unchanged((value, source), old_paths)
            ),
        ],
    }


def scan_private_text_delta(previous: str, text: str, patterns: list[str]) -> dict:
    _validate_private_patterns(patterns)
    views = private_text_delta_views(previous, text)
    result = _private_text_result(
        views["literal_values"], views["path_values"], patterns
    )
    if views.get("full_fallback"):
        result["delta_full_fallback"] = True
    return result


_REPOSITORY_COMPONENT = re.compile(r"(?:[\w@+.\-]+|\([\w.\-]+\)|\[[\w.\-]+\])")


def scan_private_repository_name(text: str, patterns: list[str]) -> dict:
    """Git在庫の相対名だけを区別する。本文/URI/host埋込みには適用しない。"""
    result = scan_private_text(text, patterns)
    parts = text.split("/")
    relative = len(parts) > 1 and all(
        part not in {"", ".", ".."}
        and not part.startswith("-")
        and _REPOSITORY_COMPONENT.fullmatch(part)
        for part in parts
    )
    if relative:
        # 名前に含まれるdenylist literalはそのまま残す。安全な相対名はsource式ではない。
        result["local_path_match"] = False
        result["reconstructed_path_match"] = False
        result["status"] = "fail" if result["literal_match_count"] else "pass"
    return result


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_TEXT_LIMIT = 2_000_000
PNG_IMAGE_LIMIT = 64_000_000


def scan_private_png(
    data: bytes, patterns: list[str], *, metadata_observer=None
) -> dict:
    """PNG containerのraw照合とmetadata照合。圧縮画素をsource codeと解釈しない。"""
    import struct
    import zlib
    from urllib.parse import unquote

    if not patterns or not all(isinstance(p, str) and p.strip() for p in patterns):
        raise ValueError("private_identity_policy_empty")
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("private_identity_png_unverified")
    if metadata_observer is not None and not callable(metadata_observer):
        raise ValueError("private_identity_png_unverified")
    hits = set()
    path_match = reconstructed = metadata_path_match = False
    metadata_size = 0
    image_parts = []
    image_size = 0
    image_rows = []
    palette_seen = False

    def text_views(raw):
        values = {raw.decode("utf-8", errors="replace"), raw.decode("latin-1")}
        # chunk payloads need both alignments: the former whole-container view
        # could expose UTF-16 text whose code units start at either payload offset.
        for codec in ("utf-16-le", "utf-16-be"):
            for start in (0, 1):
                values.add(raw[start:].decode(codec, errors="replace"))
        return {candidate for value in values for candidate in (value, unquote(value))}

    # Validated coding/pixel/CRC bytes are not text. Their structural checks below
    # must complete before any result can pass; ancillary metadata is still scanned.
    def inspect_text(value):
        nonlocal path_match, reconstructed, metadata_path_match, metadata_size
        metadata_size += len(value.encode("utf-8"))
        if metadata_size > PNG_TEXT_LIMIT:
            raise ValueError("private_identity_png_text_limit")
        for candidate in {value, unquote(value)}:
            if metadata_observer is not None:
                metadata_observer(candidate)
            views = static_string_views(candidate)
            variants = [
                candidate,
                *(v for v, _ in views),
                *_reconstruct_literal_candidates(candidate, views),
            ]
            for variant in variants:
                hits.update(hit_patterns(variant, patterns))
            metadata_match = hit_local_absolute_path(candidate) or any(
                hit_local_absolute_path(v) for v, _ in views
            )
            metadata_path_match |= metadata_match
            path_match |= metadata_match
            reconstructed |= any(
                hit_local_absolute_path(v) and v not in original
                for v, original in views
            )

    def inflate(raw):
        try:
            decoder = zlib.decompressobj()
            limit = PNG_TEXT_LIMIT - metadata_size
            value = decoder.decompress(raw, limit + 1)
            if len(value) > limit or decoder.unconsumed_tail:
                raise ValueError("private_identity_png_text_limit")
            if not decoder.eof or decoder.unused_data:
                raise ValueError("private_identity_png_unverified")
            return value
        except zlib.error:
            raise ValueError("private_identity_png_unverified") from None

    def keyword(raw):
        if (
            not 1 <= len(raw) <= 79
            or raw.startswith(b" ")
            or raw.endswith(b" ")
            or b"  " in raw
            or any(not (32 <= b <= 126 or 161 <= b <= 255) for b in raw)
        ):
            raise ValueError("private_identity_png_unverified")
        inspect_text(raw.decode("latin-1"))

    position = 8
    chunks = 0
    seen_header = seen_pixels = pixels_closed = False
    while position < len(data):
        chunks += 1
        if chunks > 65536 or len(data) - position < 12:
            raise ValueError("private_identity_png_unverified")
        length = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4 : position + 8]
        end = position + 12 + length
        if end > len(data) or not re.fullmatch(b"[A-Za-z]{4}", kind) or kind[2] & 32:
            raise ValueError("private_identity_png_unverified")
        payload = data[position + 8 : position + 8 + length]
        crc = struct.unpack_from(">I", data, position + 8 + length)[0]
        if zlib.crc32(kind + payload) != crc:
            raise ValueError("private_identity_png_unverified")
        position = end
        if kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND"}:
            for value in text_views(payload):
                hits.update(hit_patterns(value, patterns))
                raw_path = hit_local_absolute_path(value)
                path_match |= raw_path
                metadata_path_match |= raw_path
        if not seen_header and kind != b"IHDR":
            raise ValueError("private_identity_png_unverified")
        if seen_pixels and kind != b"IDAT":
            pixels_closed = True
        if kind == b"IHDR":
            if seen_header or length != 13:
                raise ValueError("private_identity_png_unverified")
            width, height, depth, color, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                not 0 < width < 2**31
                or not 0 < height < 2**31
                or depth not in depths.get(color, set())
                or compression
                or filtering
                or interlace not in (0, 1)
            ):
                raise ValueError("private_identity_png_unverified")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            passes = (
                [(0, 0, 1, 1)]
                if not interlace
                else [
                    (0, 0, 8, 8),
                    (4, 0, 8, 8),
                    (0, 4, 4, 8),
                    (2, 0, 4, 4),
                    (0, 2, 2, 4),
                    (1, 0, 2, 2),
                    (0, 1, 1, 2),
                ]
            )
            for x, y, dx, dy in passes:
                columns = max(0, (width - x + dx - 1) // dx)
                rows = max(0, (height - y + dy - 1) // dy)
                if columns and rows:
                    image_rows.append(
                        ((columns * channels * depth + 7) // 8, rows, columns)
                    )
            expected_image_size = sum(
                (row_bytes + 1) * rows for row_bytes, rows, _ in image_rows
            )
            if expected_image_size > PNG_IMAGE_LIMIT or (
                color == 3 and width * height > PNG_IMAGE_LIMIT
            ):
                raise ValueError("private_identity_png_image_limit")
            seen_header = True
        elif kind == b"IDAT":
            if pixels_closed:
                raise ValueError("private_identity_png_unverified")
            if color == 3 and not palette_seen:
                raise ValueError("private_identity_png_unverified")
            image_size += len(payload)
            if image_size > PNG_IMAGE_LIMIT:
                raise ValueError("private_identity_png_image_limit")
            image_parts.append(payload)
            seen_pixels = True
        elif kind == b"IEND":
            if length or not seen_pixels or position != len(data):
                raise ValueError("private_identity_png_unverified")
            try:
                decoder = zlib.decompressobj()
                pixels = decoder.decompress(
                    b"".join(image_parts), expected_image_size + 1
                )
                if (
                    len(pixels) != expected_image_size
                    or not decoder.eof
                    or decoder.unconsumed_tail
                    or decoder.unused_data
                ):
                    raise ValueError("private_identity_png_unverified")
            except zlib.error:
                raise ValueError("private_identity_png_unverified") from None
            offset = 0
            for row_bytes, rows, columns in image_rows:
                previous = bytes(row_bytes) if color == 3 else b""
                for _ in range(rows):
                    filter_kind = pixels[offset]
                    if filter_kind > 4:
                        raise ValueError("private_identity_png_unverified")
                    # indexed samples are one byte apart before bit unpacking.
                    # Reset the previous row at every Adam7 pass boundary.
                    if color == 3 and palette_entries < 2**depth:
                        row = bytearray(pixels[offset + 1 : offset + row_bytes + 1])
                        for index in range(row_bytes):
                            left = row[index - 1] if index else 0
                            above = previous[index]
                            upper_left = previous[index - 1] if index else 0
                            predictor = 0
                            if filter_kind == 1:
                                predictor = left
                            elif filter_kind == 2:
                                predictor = above
                            elif filter_kind == 3:
                                predictor = (left + above) // 2
                            elif filter_kind == 4:
                                paeth = left + above - upper_left
                                distances = (
                                    abs(paeth - left),
                                    abs(paeth - above),
                                    abs(paeth - upper_left),
                                )
                                predictor = (left, above, upper_left)[
                                    distances.index(min(distances))
                                ]
                            row[index] = (row[index] + predictor) & 255
                        for column in range(columns):
                            bit = column * depth
                            sample = (row[bit // 8] >> (8 - depth - bit % 8)) & (
                                (1 << depth) - 1
                            )
                            if sample >= palette_entries:
                                raise ValueError("private_identity_png_unverified")
                        previous = row
                    offset += row_bytes + 1
            return {
                "status": "fail" if hits or path_match else "pass",
                "literal_match_count": len(hits),
                "local_path_match": bool(path_match),
                "reconstructed_path_match": bool(reconstructed),
                "metadata_path_match": bool(metadata_path_match),
                **(
                    {"metadata_observer_complete": True}
                    if metadata_observer is not None
                    else {}
                ),
            }
        elif kind == b"PLTE":
            if (
                seen_pixels
                or palette_seen
                or color in (0, 4)
                or not length
                or length > 768
                or length % 3
                or color == 3
                and length // 3 > 2**depth
            ):
                raise ValueError("private_identity_png_unverified")
            palette_seen = True
            palette_entries = length // 3
        elif kind in (b"tEXt", b"zTXt", b"iTXt"):
            try:
                key, rest = payload.split(b"\0", 1)
                keyword(key)
                if (
                    metadata_observer is not None
                    and key.lower() == b"xml:com.adobe.xmp"
                ):
                    # This image path has no bounded XML semantics decoder.
                    raise ValueError("private_identity_png_unverified")
                if kind == b"tEXt":
                    text = rest.decode("latin-1")
                elif kind == b"zTXt":
                    if not rest or rest[0] != 0:
                        raise ValueError("private_identity_png_unverified")
                    text = inflate(rest[1:]).decode("latin-1")
                else:
                    if (
                        len(rest) < 2
                        or rest[0] not in (0, 1)
                        or rest[0] == 1
                        and rest[1] != 0
                    ):
                        raise ValueError("private_identity_png_unverified")
                    language, translated, raw_text = rest[2:].split(b"\0", 2)
                    inspect_text(language.decode("ascii"))
                    inspect_text(translated.decode("utf-8"))
                    text = (inflate(raw_text) if rest[0] else raw_text).decode("utf-8")
                if "\0" in text:
                    raise ValueError("private_identity_png_unverified")
                inspect_text(text)
            except (UnicodeDecodeError, ValueError) as exc:
                if str(exc) in (
                    "private_identity_png_text_limit",
                    "private_identity_normalization_incomplete",
                ):
                    raise
                raise ValueError("private_identity_png_unverified") from None
        elif kind in (b"acTL", b"fcTL", b"fdAT"):
            # frame毎の復号を検証していないAPNGを、静止画の画素免除へ入れない。
            raise ValueError("private_identity_png_animation_unverified")
        else:
            if not kind[0] & 32:
                raise ValueError("private_identity_png_unverified")
            if metadata_observer is not None:
                # Only parsed text and validated core chunks have cover metadata
                # semantics here. Raw ancillary views cannot certify structures.
                raise ValueError("private_identity_png_unverified")
            # 未知の付加chunkは免除しない。既存の文字viewと正規化で調べる。
            for value in text_views(payload):
                inspect_text(value)
    raise ValueError("private_identity_png_unverified")


JPEG_SIGNATURE = b"\xff\xd8\xff"
JPEG_DATA_LIMIT = 64_000_000
JPEG_METADATA_LIMIT = 2_000_000
_JPEG_DECODE = r"""
import sys, warnings
try:
    import simplejpeg
except ImportError:
    sys.exit(86)
try:
    warnings.simplefilter("error")
    data = sys.stdin.buffer.read(64000001)
    if len(data) > 64000000:
        sys.exit(87)
    height, width, _, _ = simplejpeg.decode_jpeg_header(data, strict=True)
    if min(height, width) <= 0 or width * height * 3 > 64000000:
        sys.exit(87)
    image = simplejpeg.decode_jpeg(data, colorspace="RGB", strict=True)
    if image.shape[:2] != (height, width) or image.nbytes > 64000000:
        sys.exit(87)
except Exception:
    sys.exit(87)
"""


def scan_private_jpeg(
    data: bytes, patterns: list[str], *, metadata_observer=None
) -> dict:
    """JPEGの復号を分離processで検証し、画素以外の全segmentを文字検査する。"""
    import os
    import subprocess
    import sys
    from urllib.parse import unquote

    if not patterns or not all(isinstance(p, str) and p.strip() for p in patterns):
        raise ValueError("private_identity_policy_empty")
    if not data.startswith(JPEG_SIGNATURE):
        raise ValueError("private_identity_jpeg_unverified")
    if metadata_observer is not None and not callable(metadata_observer):
        raise ValueError("private_identity_jpeg_unverified")
    if len(data) > JPEG_DATA_LIMIT:
        raise ValueError("private_identity_jpeg_image_limit")
    hits = set()
    path_match = reconstructed = metadata_match = False

    def views(raw, metadata=False):
        values = {raw.decode("utf-8", errors="replace")}
        if metadata:
            values.add(raw.decode("latin-1"))
            for codec in ("utf-16-le", "utf-16-be"):
                for start in (0, 1):
                    values.add(raw[start:].decode(codec, errors="replace"))
        else:
            try:
                values.add(raw.decode("utf-16"))
            except UnicodeDecodeError:
                pass
        return {v for text in values for v in (text, unquote(text))}

    # Validated coding and entropy bytes are binary, not identity text.
    # Marker boundaries expose all non-pixel bytes, including between progressive scans.
    position = 2
    in_scan = seen_scan = False
    # COM と APP1 XMP の本文だけ source 正規化へ渡す。
    # Exif や未解析の APP payload はバイナリなので、raw照合だけに留める。
    xmp_header = b"http://ns.adobe.com/xap/1.0/\x00"
    segments = []
    raw_segments = []
    total = markers = 0
    while position < len(data):
        if in_scan:
            found = data.find(b"\xff", position)
            if found < 0:
                raise ValueError("private_identity_jpeg_unverified")
            position = found
            end = position + 1
            while end < len(data) and data[end] == 255:
                end += 1
            if end == len(data):
                raise ValueError("private_identity_jpeg_unverified")
            if data[end] == 0 or 0xD0 <= data[end] <= 0xD7:
                position = end + 1
                continue
            in_scan = False
        if data[position] != 255:
            raise ValueError("private_identity_jpeg_unverified")
        while position < len(data) and data[position] == 255:
            position += 1
        if position >= len(data):
            raise ValueError("private_identity_jpeg_unverified")
        kind = data[position]
        position += 1
        markers += 1
        if markers > 65536:
            raise ValueError("private_identity_jpeg_unverified")
        if kind == 0xD9:
            if not seen_scan or position != len(data):
                raise ValueError("private_identity_jpeg_unverified")
            break
        if kind not in {0xC0, 0xC2, 0xC4, 0xDA, 0xDB, 0xDD, 0xFE, *range(0xE0, 0xF0)}:
            raise ValueError("private_identity_jpeg_unverified")
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            raise ValueError("private_identity_jpeg_unverified")
        payload = data[position + 2 : position + length]
        position += length
        total += len(payload)
        if total > JPEG_METADATA_LIMIT:
            raise ValueError("private_identity_jpeg_text_limit")
        if (
            metadata_observer is not None
            and 0xE0 <= kind <= 0xEF
            and not (
                kind == 0xE0
                and payload.startswith(b"JFIF\0")
                or kind == 0xEE
                and payload.startswith(b"Adobe")
            )
        ):
            # Only the already shape-validated JFIF/Adobe APP headers are
            # supported here. ICC/Exif/XMP/other APP need semantic decoders.
            raise ValueError("private_identity_jpeg_unverified")
        if kind == 0xE0 and payload.startswith(b"JFIF\0"):
            if (
                len(payload) < 14
                or payload[5] != 1
                or payload[6] > 2
                or payload[7] > 2
                or len(payload) != 14 + 3 * payload[12] * payload[13]
            ):
                raise ValueError("private_identity_jpeg_unverified")
        elif kind == 0xEE and payload.startswith(b"Adobe"):
            if len(payload) != 12 or payload[-1] > 2:
                raise ValueError("private_identity_jpeg_unverified")
        elif kind == 0xFE:
            segments.append(payload)
        elif kind == 0xE1 and payload.startswith(xmp_header):
            segments.append(payload[len(xmp_header) :])
        elif 0xE0 <= kind <= 0xEF:
            raw_segments.append(payload)
        if kind == 0xDA:
            in_scan = seen_scan = True
    else:
        raise ValueError("private_identity_jpeg_unverified")
    try:
        decoded = subprocess.run(
            [sys.executable, "-I", "-c", _JPEG_DECODE],
            input=data,
            env={
                key: os.environ[key]
                for key in ("SYSTEMROOT", "WINDIR")
                if key in os.environ
            },
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("private_identity_jpeg_unverified") from None
    if decoded.returncode == 86:
        raise ValueError("private_identity_jpeg_decoder_unavailable")
    if decoded.returncode or decoded.stdout or decoded.stderr:
        raise ValueError("private_identity_jpeg_unverified")
    for raw in raw_segments:
        for candidate in views(raw, metadata=True):
            if metadata_observer is not None:
                metadata_observer(candidate)
            hits.update(hit_patterns(candidate, patterns))
            if hit_local_absolute_path(candidate):
                metadata_match = True
                path_match = True
    for raw in segments:
        for candidate in views(raw, metadata=True):
            if metadata_observer is not None:
                metadata_observer(candidate)
            result = scan_private_text(candidate, patterns)
            if result["literal_match_count"]:
                literals = static_string_views(candidate)
                for value in [
                    candidate,
                    *(v for v, _ in literals),
                    *_reconstruct_literal_candidates(candidate, literals),
                ]:
                    hits.update(hit_patterns(value, patterns))
            metadata_match |= result["local_path_match"]
            path_match |= result["local_path_match"]
            reconstructed |= result["reconstructed_path_match"]
    return {
        "status": "fail" if hits or path_match else "pass",
        "literal_match_count": len(hits),
        "local_path_match": bool(path_match),
        "reconstructed_path_match": bool(reconstructed),
        "metadata_path_match": bool(metadata_match),
        **(
            {"metadata_observer_complete": True}
            if metadata_observer is not None
            else {}
        ),
    }


def scan_private_psd(data: bytes, patterns: list[str]) -> dict:
    """PSDの構造とメタデータを検査し、圧縮画素をソースコードにしない。"""
    import io
    import warnings
    import logging
    import threading
    from contextlib import contextmanager
    from collections.abc import Mapping
    from urllib.parse import unquote

    if not patterns or not all(isinstance(p, str) and p.strip() for p in patterns):
        raise ValueError("private_identity_policy_empty")
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("private_identity_psd_size_limit")
    if not data.startswith(b"8BPS\x00\x01"):
        raise ValueError("private_identity_psd_unverified")
    try:
        from psd_tools.psd import PSD
    except ImportError:
        raise ValueError("private_identity_psd_decoder_unavailable") from None

    @contextmanager
    def strict_decode():
        class RejectWarning(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING and record.thread == owner:
                    raise ValueError("private_identity_psd_unverified")

        owner = threading.get_ident()
        handler = RejectWarning()
        logger = logging.getLogger("psd_tools")
        logger.addHandler(handler)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                yield
        finally:
            logger.removeHandler(handler)

    def validate_encoded(raw, compression, rows, row_bytes):
        import struct
        import zlib

        expected_size = rows * row_bytes
        if int(compression) == 0:
            if len(raw) != expected_size:
                raise ValueError("private_identity_psd_unverified")
        elif int(compression) == 1:
            table_size = rows * 2
            if len(raw) < table_size:
                raise ValueError("private_identity_psd_unverified")
            counts = struct.unpack(">" + "H" * rows, raw[:table_size])
            if sum(counts) != len(raw) - table_size:
                raise ValueError("private_identity_psd_unverified")
            offset = table_size
            for count in counts:
                end = offset + count
                produced = 0
                while offset < end:
                    control = raw[offset]
                    offset += 1
                    if control < 128:
                        amount = control + 1
                        offset += amount
                        produced += amount
                    elif control > 128:
                        offset += 1
                        produced += 257 - control
                    if offset > end or produced > row_bytes:
                        raise ValueError("private_identity_psd_unverified")
                if produced != row_bytes:
                    raise ValueError("private_identity_psd_unverified")
        elif int(compression) in (2, 3):
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(raw, expected_size + 1)
            if (
                len(decoded) != expected_size
                or not decoder.eof
                or decoder.unused_data
                or decoder.unconsumed_tail
            ):
                raise ValueError("private_identity_psd_unverified")
        else:
            raise ValueError("private_identity_psd_unverified")

    try:
        with strict_decode():
            document = PSD.read(io.BytesIO(data))
            header = document.header
            expected = (
                ((header.width * header.depth + 7) // 8)
                * header.height
                * header.channels
            )
            if expected > 128 * 1024 * 1024:
                raise ValueError("image_limit")
            validate_encoded(
                document.image_data.data,
                document.image_data.compression,
                header.height * header.channels,
                (header.width * header.depth + 7) // 8,
            )
            merged_pixels = document.image_data.get_data(
                header, split=False, max_output_bytes=128 * 1024 * 1024
            )
            if len(merged_pixels) != expected:
                raise ValueError("image_size_mismatch")
    except Exception:
        raise ValueError("private_identity_psd_unverified") from None
    hits = set()
    path_match = metadata_match = reconstructed = False
    size = nodes = 0

    def raw_views(raw):
        values = {raw.decode("utf-8", errors="replace")}
        for encoding in ("utf-16-le", "utf-16-be"):
            values.add(raw.decode(encoding, errors="replace"))
        return {unquote(v) for v in values} | values

    # 圧縮画素を含む全byteの直接照合は残す。
    for raw in (data, merged_pixels):
        for value in raw_views(raw):
            hits.update(hit_patterns(value, patterns))
            path_match |= hit_local_absolute_path(value)

    def text(value):
        nonlocal size, path_match, metadata_match, reconstructed
        size += len(value.encode("utf-8", errors="replace"))
        if size > 8 * 1024 * 1024:
            raise ValueError("private_identity_psd_text_limit")
        for candidate in (value, unquote(value)):
            result = scan_private_text(candidate, patterns)
            if result["literal_match_count"]:
                hits.add("metadata")
            path_match |= result["local_path_match"]
            metadata_match |= result["local_path_match"]
            reconstructed |= result.get("reconstructed_path_match", False)

    def iptc(raw):
        # IIMのrecord/dataset見出しと長さを、本文から分離して読む。
        offset = 0
        while offset < len(raw):
            if offset + 5 > len(raw) or raw[offset] != 0x1C:
                raise ValueError("private_identity_psd_unverified")
            length = int.from_bytes(raw[offset + 3 : offset + 5], "big")
            offset += 5
            if length & 0x8000:
                octets = length & 0x7FFF
                if not 1 <= octets <= 4 or offset + octets > len(raw):
                    raise ValueError("private_identity_psd_unverified")
                length = int.from_bytes(raw[offset : offset + octets], "big")
                offset += octets
            if offset + length > len(raw):
                raise ValueError("private_identity_psd_unverified")
            for candidate in raw_views(raw[offset : offset + length]):
                text(candidate)
            offset += length

    def visit(value):
        nonlocal nodes
        nodes += 1
        if nodes > 100000:
            raise ValueError("private_identity_psd_text_limit")
        from psd_tools.psd.image_resources import ImageResource

        if isinstance(value, ImageResource) and int(value.key) == 1028:
            visit(value.name)
            if not isinstance(value.data, bytes):
                raise ValueError("private_identity_psd_unverified")
            iptc(value.data)
        elif isinstance(value, str):
            text(value)
        elif isinstance(value, bytes):
            # 未対応のframingでも本文を捨てず、正規化できない場合は停止する。
            for candidate in raw_views(value):
                text(candidate)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif hasattr(type(value), "__attrs_attrs__"):
            for field in type(value).__attrs_attrs__:
                visit(getattr(value, field.name))
        elif value is not None and not isinstance(value, (int, float, bool)):
            raise ValueError("private_identity_psd_unverified")

    visit(document.image_resources)
    info = document.layer_and_mask_information
    expanded = len(merged_pixels)
    if info.layer_info is not None:
        layer_info = info.layer_info
        if len(layer_info.layer_records) != len(layer_info.channel_image_data):
            raise ValueError("private_identity_psd_unverified")
        for record, channels in zip(
            layer_info.layer_records, layer_info.channel_image_data
        ):
            if len(record.channel_info) != len(channels):
                raise ValueError("private_identity_psd_unverified")
            for description, channel in zip(record.channel_info, channels):
                rectangle = record
                prefix = ""
                if description.id in (-2, -3):
                    rectangle = record.mask_data
                    prefix = "real_" if description.id == -3 else ""
                if rectangle is None:
                    raise ValueError("private_identity_psd_unverified")
                edges = [
                    getattr(rectangle, prefix + key)
                    for key in ("top", "left", "bottom", "right")
                ]
                if any(edge is None for edge in edges):
                    raise ValueError("private_identity_psd_unverified")
                top, left, bottom, right = edges
                width, height = right - left, bottom - top
                if width < 0 or height < 0:
                    raise ValueError("private_identity_psd_unverified")
                required = ((width * header.depth + 7) // 8) * height
                if required == 0:
                    if channel.data:
                        raise ValueError("private_identity_psd_unverified")
                    continue
                expanded += required
                if expanded > 128 * 1024 * 1024:
                    raise ValueError("private_identity_psd_size_limit")
                try:
                    with strict_decode():
                        validate_encoded(
                            channel.data,
                            channel.compression,
                            height,
                            (width * header.depth + 7) // 8,
                        )
                        decoded = channel.get_data(
                            width,
                            height,
                            header.depth,
                            header.version,
                            max_output_bytes=128 * 1024 * 1024,
                        )
                    if len(decoded) != required:
                        raise ValueError("channel_size_mismatch")
                except Exception:
                    raise ValueError("private_identity_psd_unverified") from None
                for value in raw_views(decoded):
                    hits.update(hit_patterns(value, patterns))
                    path_match |= hit_local_absolute_path(value)
        visit(layer_info.layer_records)
    visit(info.global_layer_mask_info)
    visit(info.tagged_blocks)
    return {
        "status": "fail" if hits or path_match else "pass",
        "literal_match_count": len(hits),
        "local_path_match": path_match,
        "metadata_path_match": metadata_match,
        "reconstructed_path_match": reconstructed,
    }


def scan_private_gif(data: bytes, patterns: list[str]) -> dict:
    """GIFの拡張本文と全フレームの画素を検査する。画素を式として評価しない。"""
    import io
    import struct
    import warnings
    from urllib.parse import unquote

    if not patterns or not all(isinstance(p, str) and p.strip() for p in patterns):
        raise ValueError("private_identity_policy_empty")
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("private_identity_gif_size_limit")
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("private_identity_gif_unverified")
    try:
        from PIL import Image, ImageFile, GifImagePlugin
    except ImportError:
        raise ValueError("private_identity_gif_decoder_unavailable") from None
    if ImageFile.LOAD_TRUNCATED_IMAGES or int(GifImagePlugin.LOADING_STRATEGY) == 2:
        raise ValueError("private_identity_gif_unverified")
    hits = set()
    path_match = metadata_match = reconstructed = False
    metadata_size = 0

    def views(raw):
        values = {raw.decode("utf-8", errors="replace")}
        for encoding in ("utf-16-le", "utf-16-be"):
            values.add(raw.decode(encoding, errors="replace"))
        return values | {unquote(v) for v in values}

    def literal(raw):
        nonlocal path_match
        for value in views(raw):
            hits.update(hit_patterns(value, patterns))
            path_match |= hit_local_absolute_path(value)

    def metadata(raw):
        nonlocal metadata_size, path_match, metadata_match, reconstructed
        metadata_size += len(raw)
        if metadata_size > 8 * 1024 * 1024:
            raise ValueError("private_identity_gif_size_limit")
        for value in views(raw):
            result = scan_private_text(value, patterns)
            if result["literal_match_count"]:
                hits.add("metadata")
            path_match |= result["local_path_match"]
            metadata_match |= result["local_path_match"]
            reconstructed |= result.get("reconstructed_path_match", False)

    literal(data)
    screen_w, screen_h = struct.unpack("<HH", data[6:10])
    if not screen_w or not screen_h:
        raise ValueError("private_identity_gif_unverified")
    palette_size = 3 * (2 ** ((data[10] & 7) + 1)) if data[10] & 128 else 0
    offset = 13 + palette_size
    palette = data[13:offset]
    frames = []
    expanded = 0

    def blocks(start, collect=True):
        chunks = []
        while start < len(data):
            length = data[start]
            start += 1
            if not length:
                return b"".join(chunks) if collect else b"", start
            if start + length > len(data):
                break
            if collect:
                chunks.append(data[start : start + length])
            start += length
        raise ValueError("private_identity_gif_unverified")

    while offset < len(data):
        kind = data[offset]
        if kind == 0x3B:
            if offset != len(data) - 1 or not frames:
                raise ValueError("private_identity_gif_unverified")
            break
        if kind == 0x21:
            if offset + 2 >= len(data):
                raise ValueError("private_identity_gif_unverified")
            begin = offset
            label = data[offset + 1]
            if label not in (0xF9, 0xFE, 0xFF, 0x01):
                raise ValueError("private_identity_gif_unverified")
            if label in (0xFF, 0x01) and data[offset + 2] != (
                11 if label == 0xFF else 12
            ):
                raise ValueError("private_identity_gif_unverified")
            payload, offset = blocks(offset + 2)
            if label == 0xF9:
                if len(payload) != 4 or data[begin + 2] != 4 or offset != begin + 8:
                    raise ValueError("private_identity_gif_unverified")
            else:
                metadata(payload)
            continue
        if kind != 0x2C or offset + 10 >= len(data):
            raise ValueError("private_identity_gif_unverified")
        start = offset
        left, top, width, height = struct.unpack("<HHHH", data[offset + 1 : offset + 9])
        packed = data[offset + 9]
        if (
            not width
            or not height
            or left + width > screen_w
            or top + height > screen_h
            or packed & 0x18
        ):
            raise ValueError("private_identity_gif_unverified")
        if width * height * 4 > 128 * 1024 * 1024:
            raise ValueError("private_identity_gif_size_limit")
        expanded += width * height * 4
        if expanded > 512 * 1024 * 1024 or len(frames) >= 512:
            raise ValueError("private_identity_gif_size_limit")
        local_size = 3 * (2 ** ((packed & 7) + 1)) if packed & 128 else 0
        offset += 10 + local_size
        if not palette_size and not local_size:
            raise ValueError("private_identity_gif_unverified")
        if offset >= len(data) or not 2 <= data[offset] <= 8:
            raise ValueError("private_identity_gif_unverified")
        _, offset = blocks(offset + 1, collect=False)
        frames.append((start, offset, width, height))
    else:
        raise ValueError("private_identity_gif_unverified")
    for start, end, width, height in frames:
        # 各frameを単独で読み、透明・差分frameの元の画素も合成で消さない。
        frame = (
            b"GIF89a"
            + struct.pack("<HH", width, height)
            + data[10:13]
            + palette
            + b"\x2c\0\0\0\0"
            + data[start + 5 : end]
            + b"\x3b"
        )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                with Image.open(io.BytesIO(frame)) as image:
                    image.load()
                    if image.size != (width, height):
                        raise ValueError("frame_size")
                    literal(image.tobytes())
                    literal(image.convert("RGBA").tobytes())
        except Exception:
            raise ValueError("private_identity_gif_unverified") from None
    return {
        "status": "fail" if hits or path_match else "pass",
        "literal_match_count": len(hits),
        "local_path_match": path_match,
        "metadata_path_match": metadata_match,
        "reconstructed_path_match": reconstructed,
    }


def scan_private_caff(data: bytes, patterns: list[str], image_cache=None) -> dict:
    """CAFFの表・XML・全埋込み画像を、圧縮画素を式として評価せず検査する。"""
    import hashlib
    import json
    from urllib.parse import unquote
    import xml.etree.ElementTree as ET

    if not patterns or not all(isinstance(p, str) and p.strip() for p in patterns):
        raise ValueError("private_identity_policy_empty")
    archive = _CAFF_MODULE.Archive(data)
    hits = set()
    path_match = metadata_match = reconstructed = False

    def record(result, metadata=False):
        nonlocal path_match, metadata_match, reconstructed
        if result["literal_match_count"]:
            hits.add("match")
        path_match |= result["local_path_match"]
        metadata_match |= metadata and result["local_path_match"]
        reconstructed |= result.get("reconstructed_path_match", False)

    def metadata_text(value, *, path_value=False):
        nonlocal path_match, metadata_match
        # XML sourceとscalarは同じ正規化を使う。scalarのpathは制御文字も含めて判定する。
        for view in {value, unquote(value)}:
            record(scan_private_text(view, patterns), True)
            if path_value and macos_shared_parent_matches(view, entire_path=True):
                path_match = metadata_match = True

    def literal(raw):
        nonlocal path_match
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            text = raw.decode(encoding, errors="replace")
            for value in (text, unquote(text)):
                hits.update(hit_patterns(value, patterns))
                path_match |= hit_local_absolute_path(value)

    # ファイル表を検証した後は、PNGの画素を外側で再び文字列扱いしない。
    if archive.preview_size:
        preview = data[
            archive.preview_start : archive.preview_start + archive.preview_size
        ]
        record(scan_private_png(preview, patterns), True)
    main_count = 0
    for entry, content in archive.contents():
        for value in (entry.name, entry.tag):
            metadata_text(value, path_value=True)
        if entry.tag == "main_xml":
            main_count += 1
            try:
                xml_text = content.decode("utf-8", "strict")
            except UnicodeError:
                raise ValueError("private_identity_caff_unverified") from None
            # UTF-16/32のASCII列はUTF-8 decodeに成功する場合がある。NULと別encoding宣言を拒否する。
            declared_encoding = re.match(
                r"^\ufeff?<\?xml\s+[^?]*?encoding\s*=\s*['\"]([^'\"]+)['\"]", xml_text
            )
            if "\0" in xml_text or (
                declared_encoding
                and declared_encoding.group(1).casefold().replace("-", "") != "utf8"
            ):
                raise ValueError("private_identity_caff_unverified")
            if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
                raise ValueError("private_identity_caff_unverified")

            class BoundedBuilder(ET.TreeBuilder):
                count = 0

                def start(self, tag, attrs):
                    self.count += 1
                    if self.count > _CAFF_MODULE.MAX_XML_NODES:
                        raise ValueError("private_identity_caff_size_limit")
                    return super().start(tag, attrs)

            try:
                root = ET.fromstring(
                    xml_text, parser=ET.XMLParser(target=BoundedBuilder())
                )
            except ET.ParseError:
                raise ValueError("private_identity_caff_unverified") from None
            literal(content)
            metadata_text(xml_text)
            count = 0
            for node in root.iter():
                count += 1
                if count > 100000:
                    raise ValueError("private_identity_caff_size_limit")
                for value in (
                    node.tag,
                    node.text,
                    node.tail,
                    *node.attrib.keys(),
                    *node.attrib.values(),
                ):
                    if value:
                        metadata_text(value, path_value=True)
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            digest = (
                hashlib.sha256(content).hexdigest(),
                hashlib.sha256(
                    json.dumps(
                        patterns, ensure_ascii=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
            )
            result = image_cache.get(digest) if image_cache is not None else None
            if result is None:
                result = scan_private_png(content, patterns)
                if image_cache is not None:
                    if len(image_cache) >= 4096:
                        image_cache.clear()
                    image_cache[digest] = result
            record(result, True)
        else:
            raise ValueError("private_identity_caff_unverified")
    if main_count != 1:
        raise ValueError("private_identity_caff_unverified")
    return {
        "status": "fail" if hits or path_match else "pass",
        "literal_match_count": len(hits),
        "local_path_match": path_match,
        "metadata_path_match": metadata_match,
        "reconstructed_path_match": reconstructed,
    }


import importlib.util as _caff_importlib
import hashlib as _caff_hashlib
from pathlib import Path as _CaffPath

_CAFF_PATH = _CaffPath(__file__).with_name("caff_archive.py")
_CAFF_SOURCE = _CAFF_PATH.read_bytes()
CAFF_DECODER_SHA256 = _caff_hashlib.sha256(_CAFF_SOURCE).hexdigest()
_CAFF_SPEC = _caff_importlib.spec_from_file_location(
    "_identity_caff_archive", _CAFF_PATH
)
_CAFF_MODULE = _caff_importlib.module_from_spec(_CAFF_SPEC)
import sys as _caff_sys

_caff_sys.modules[_CAFF_SPEC.name] = _CAFF_MODULE
exec(compile(_CAFF_SOURCE, str(_CAFF_PATH), "exec"), _CAFF_MODULE.__dict__)

# 消費側は別のCAFFパーサを持たず、これを利用する。
CaffArchive = _CAFF_MODULE.Archive
