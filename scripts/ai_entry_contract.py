#!/usr/bin/env python3
"""AI 憲法エントリーポイントの検査と、安全な materialized 投影を行う。

共通憲法はソース文書であり、各ランタイムの入口は 3 戦略のいずれかを取る:
``pointer`` (Claude/Gemini の ``@`` import や Codex の明示的な読込指示のように
ソースを参照する)、``materialized`` (import 構文を持たないランタイム向けに
ソースの生成コピーを持つ)、``manual`` (Cursor のユーザー設定のように製品側での
人手確認が必要)。

コマンドは既定で read-only。書き込みは ``--apply --entry-id`` で明示選択した
materialized entry 1 件のみで、既存の非生成ファイルは決して上書きしない。
レポートは secret-safe (ソース本文や絶対パスを載せない) を契約とする。

exit code: 0=pass / 1=blocked (drift・stale・missing) / 2=tool_error /
3=human_review (required な manual entry の確認待ちだけが残っている)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = "repo-preflight.ai-entry-contract/v1"
BEGIN_MARKER = "<!-- repo-preflight:ai-constitution begin -->"
END_MARKER = "<!-- repo-preflight:ai-constitution end -->"
HEADER_RE = re.compile(
    r"<!--\s*repo-preflight:ai-constitution source-sha256=([0-9a-f]{64})\s*-->"
)
STRATEGIES = {"pointer", "materialized", "manual"}
POINTER_KINDS = {"import", "instruction"}
# 行頭または空白直後の ``@`` だけを import とみなす (``user@host`` を拾わない)。
POINTER_TOKEN_RE = re.compile(r"(?<!\S)@([^@\s\"'`<>|;,()\[\]]+)")
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
# CommonMark のフェンス開始行 (インデント 3 以下、同じ文字 3 個以上)。
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
BLOCKQUOTE_PREFIX_RE = re.compile(r"^(?: {0,3}> ?)+")
BACKTICK_RUN_RE = re.compile(r"`+")
PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
KNOWN_PLACEHOLDERS = {"HOME", "PROJECT"}
EXIT_CODES = {"pass": 0, "blocked": 1, "tool_error": 2, "human_review": 3}
INSTRUCTION_SUBJECT_RE = re.compile(
    r"(?:共通原則|正本|\b(?:constitution|canonical|source)\b)",
    re.IGNORECASE,
)
INSTRUCTION_ACTION_RE = re.compile(
    r"(?:読|参照|\b(?:read|load|consult)\b)", re.IGNORECASE
)
INSTRUCTION_ORDER_RE = re.compile(
    r"(?:必ず|先に|開始前|最初|\b(?:must|before|first)\b)",
    re.IGNORECASE,
)
INSTRUCTION_NEGATIVE_RE = re.compile(
    r"(?:読まない|読み込まない|参照しない|"
    r"\bdo\s+not\s+(?:read|load|consult)\b|"
    r"\bdon['’]t\s+(?:read|load|consult)\b|"
    r"\bnever\s+(?:read|load|consult)\b|"
    r"\bnot\s+(?:read|load|consult)\b)",
    re.IGNORECASE,
)


class ContractError(ValueError):
    """この gate 自身が定義した snake_case の finding code を運ぶ例外。

    レポートへそのまま出してよいのはこの例外の文字列だけ。標準ライブラリ等の
    例外は絶対パスや username を含みうるため、型名だけに丸める。``:`` 以降の
    補足は manifest の entry id で、レポートの findings にも同じ id が載る。
    """


def normalize_text(value: str) -> str:
    """本文を変えずに改行コードだけを LF へ正規化する。"""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def canonical_source_text(value: str) -> str:
    """hash と投影で共通の末尾改行 1 個の形へそろえる。"""

    return normalize_text(value).rstrip("\n") + "\n"


def resolve_template(value: str, *, home: Path, project: Path | None) -> Path:
    """明示的でポータブルな placeholder ({HOME}/{PROJECT}) だけを解決する。

    ``~`` は実行ユーザーの実 home に暗黙依存し ``--home`` の差し替えを迂回する
    ため受け付けない (fail-closed)。{HOME}/{PROJECT} 以外の placeholder も拒否する。
    """

    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        # NUL を含むパスは OS API で ValueError になり、is_file() は黙って
        # False を返す (欠落と誤読される) ため、解決前に manifest の誤りとして止める。
        raise ContractError("path_value_invalid")
    if value.lstrip().startswith("~"):
        raise ContractError("tilde_unsupported_use_home_placeholder")
    if set(PLACEHOLDER_RE.findall(value)) - KNOWN_PLACEHOLDERS:
        # {USERPROFILE} 等の未知 placeholder を文字どおりのパスとして扱わない。
        raise ContractError("template_placeholder_unresolved")
    if "{PROJECT}" in value and project is None:
        raise ContractError("project_required")
    expanded = value.replace("{HOME}", str(home))
    if project is not None:
        expanded = expanded.replace("{PROJECT}", str(project))
    return Path(expanded).resolve()


def _safe_error_code(exc: BaseException, prefix: str) -> str:
    """例外を secret-safe な finding 文字列へ変換する。

    自前で raise した ContractError だけは文字列をそのまま返す。メッセージの
    形で判定すると entry id に ``/`` を含む #41 の finding (例:
    ``manifest_runtime_missing:gemini/cli``) まで型名に潰れるため、型で判定する。
    それ以外 (OSError や標準ライブラリの ValueError) は絶対パスや username を
    含みうるため、型名だけに丸める。
    """

    if isinstance(exc, ContractError):
        return str(exc)
    return f"{prefix}:{type(exc).__name__}"


def _same_path(a: Path, b: Path) -> bool:
    """2 つのパスが同一ファイルを指すかをファイルシステムの意味論で判定する。

    存在するパスは ``samefile`` で (大文字小文字非区別のファイルシステムや
    symlink を含めて) 判定し、存在しないパスだけ ``os.path.normcase`` した
    文字列一致に fallback する。OS 名で大文字小文字の扱いを代理しない。
    """

    try:
        return a.samefile(b)
    except (OSError, ValueError):
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _strip_inline(text: str, in_comment: bool) -> tuple[str, bool]:
    """段落テキストから HTML コメントと CommonMark のコードスパンを除く。

    コードスパンは長さ n の backtick 列で開き、同じ長さ n の backtick 列で
    閉じる (2 連 backtick で開いた span は 1 個の backtick では閉じない)。閉じ側が無い
    backtick 列は文字どおりの backtick として残す。戻り値の bool は末尾で
    HTML コメントが開いたままかどうか。
    """

    out: list[str] = []
    index = 0
    while index < len(text):
        if in_comment:
            close = text.find("-->", index)
            if close < 0:
                return "".join(out), True
            index = close + 3
            in_comment = False
            continue
        if text.startswith("<!--", index):
            in_comment = True
            index += 4
            continue
        if text[index] == "`":
            run = BACKTICK_RUN_RE.match(text, index)
            assert run is not None
            closing = next(
                (
                    match
                    for match in BACKTICK_RUN_RE.finditer(text, run.end())
                    if len(match.group()) == len(run.group())
                ),
                None,
            )
            if closing is None:
                out.append(run.group())
                index = run.end()
            else:
                index = closing.end()
            continue
        out.append(text[index])
        index += 1
    return "".join(out), in_comment


def _indent_width(line: str) -> int:
    """行頭の空白幅を tab=4 桁で数える。"""

    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _closes_fence(line: str, fence: tuple[str, int]) -> bool:
    """CommonMark の閉じフェンス (同じ文字・開始以上の長さ・後続は空白のみ)。"""

    char, length = fence
    if _indent_width(line) > 3:
        return False
    body = line.strip()
    run = len(body) - len(body.lstrip(char))
    return run >= length and not body[run:].strip()


def _iter_import_lines(text: str) -> list[str]:
    """import として評価してよい可視テキストを行単位で返す。

    CommonMark に合わせて、フェンスコードブロック (開始と同じ文字・同じ以上の
    長さでだけ閉じる)、インデントコードブロック (段落の途中以外で 4 桁以上
    字下げされた行)、コードスパン、HTML コメントの中身を除く。blockquote の
    ``>`` は剥がして中身を同じ規則で読む。閉じないフェンスは文書末まで
    コードとみなす (例示を import と誤認するより false red に倒す)。
    """

    visible: list[str] = []
    paragraph: list[str] = []
    in_comment = False
    fence: tuple[str, int, bool] | None = None

    def flush() -> None:
        nonlocal in_comment
        if paragraph:
            stripped, in_comment = _strip_inline("\n".join(paragraph), in_comment)
            visible.extend(stripped.split("\n"))
            paragraph.clear()

    for raw in normalize_text(text).split("\n"):
        line = BLOCKQUOTE_PREFIX_RE.sub("", raw, count=1)
        quoted = line != raw
        if fence is not None:
            char, length, fence_quoted = fence
            if _closes_fence(line if fence_quoted else raw, (char, length)):
                fence = None
            continue
        comment_open = (
            _strip_inline("\n".join(paragraph), in_comment)[1]
            if paragraph
            else in_comment
        )
        if comment_open:
            # HTML コメントの中ではフェンスも空行も区切りにならない。
            paragraph.append(line)
            continue
        if not line.strip():
            flush()
            continue
        opening = FENCE_OPEN_RE.match(line)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            flush()
            fence = (opening.group(1)[0], len(opening.group(1)), quoted)
            continue
        if not paragraph and _indent_width(line) >= 4:
            # 段落の途中でない 4 桁字下げはインデントコードブロック。
            continue
        paragraph.append(line)
        if re.match(r"^ {0,3}#{1,6}(?:\s|$)", line):
            # ATX 見出しは 1 行で閉じるブロックなので、次行を段落続きにしない。
            flush()
    flush()
    return visible


def _candidate_paths(token: str, *, home: Path, base: Path) -> Iterator[Path]:
    """pointer トークンをファイルパス候補へ解決する。

    ``~/`` は gate に渡された home で展開する (実 home ではない)。相対パスは
    entry ファイルのあるディレクトリ基準で解決する。文末の句読点は外した形も
    候補にする。
    """

    for raw in dict.fromkeys((token, token.rstrip(".,:;!?"))):
        if not raw:
            continue
        if raw == "~" or raw.startswith(("~/", "~\\")):
            raw = str(home) + raw[1:]
        elif raw.startswith("~"):
            continue
        try:
            path = Path(raw)
            yield (path if path.is_absolute() else base / path).resolve()
        except (OSError, ValueError):
            continue


def _has_instruction_semantics(lines: list[str], path_line: int) -> bool:
    """近傍に正本を先に読む肯定的な指示があることを要求する。"""

    context = "\n".join(lines[max(0, path_line - 2) : path_line + 3])
    if INSTRUCTION_NEGATIVE_RE.search(context):
        return False
    return bool(
        INSTRUCTION_SUBJECT_RE.search(context)
        and INSTRUCTION_ACTION_RE.search(context)
        and INSTRUCTION_ORDER_RE.search(context)
    )


def _has_import_pointer(text: str, source: Path, *, home: Path, base: Path) -> bool:
    """``@<path>`` トークンがパス解決の結果 source ファイルへ到達するか。"""

    for line in _iter_import_lines(text):
        for token in POINTER_TOKEN_RE.findall(line):
            for candidate in _candidate_paths(token, home=home, base=base):
                if _same_path(candidate, source):
                    return True
    return False


def _has_instruction_pointer(text: str, source: Path) -> bool:
    """インラインコード内の絶対パスが source を指し、近傍に読込指示があるか。"""

    lines = normalize_text(text).splitlines()
    for line_number, line in enumerate(lines):
        for span in CODE_SPAN_RE.findall(line):
            try:
                path = Path(span.strip())
            except (OSError, ValueError):
                continue
            if not path.is_absolute():
                continue
            if _same_path(path, source) and _has_instruction_semantics(
                lines, line_number
            ):
                return True
    return False


def has_pointer(
    text: str,
    source: Path,
    *,
    home: Path,
    base: Path,
    pointer_kind: str = "import",
) -> bool:
    """runtime 固有の明示 pointer が source を指すかを、source 本文を読まずに判定する。

    部分文字列一致ではなくパス解決で比較する。``import`` はコードフェンス・
    インラインコード・HTML コメント内の例示を除外する。``instruction`` は
    インラインコード内のパスと近傍の肯定的な読込指示を要求する。
    """

    source_resolved = source.resolve()
    if pointer_kind == "import":
        return _has_import_pointer(text, source_resolved, home=home, base=base)
    if pointer_kind == "instruction":
        return _has_instruction_pointer(text, source_resolved)
    raise ContractError("pointer_kind_invalid")


def _validate_pointer_kind(entry: dict[str, Any]) -> None:
    """任意の pointer 構文識別子を検証する。"""

    pointer_kind = entry.get("pointer_kind", "import")
    if entry["strategy"] != "pointer" and "pointer_kind" in entry:
        raise ContractError(f"manifest_pointer_kind_invalid:{entry['id']}")
    if not isinstance(pointer_kind, str) or pointer_kind not in POINTER_KINDS:
        raise ContractError(f"manifest_pointer_kind_invalid:{entry['id']}")


def _entry_fields() -> set[str]:
    return {
        "id",
        "runtime",
        "path",
        "strategy",
        "pointer_kind",
        "required",
        "evidence",
    }


def _marker_counts(normalized: str) -> tuple[int, int, int]:
    """(header, begin, end) 各マーカーの出現数を返す。"""

    return (
        len(HEADER_RE.findall(normalized)),
        normalized.count(BEGIN_MARKER),
        normalized.count(END_MARKER),
    )


def source_contains_markers(source_text: str) -> bool:
    """source 本文が投影マーカー自体を含むか (含む場合は投影不能)。"""

    return any(_marker_counts(normalize_text(source_text)))


def _generated_span(normalized: str) -> tuple[re.Match[str], int, int] | None:
    """一意な生成ブロックの (header, begin, end) を返す。

    マーカーが 1 つも無ければ None。個数が 1 ずつでない・順序が壊れている・
    header と begin の間に空白以外がある場合は曖昧として ValueError にする
    (最初の出現だけを黙って採用すると、marker を引用した overlay や
    複製ブロックを破壊・見逃しするため)。
    """

    counts = _marker_counts(normalized)
    if not any(counts):
        return None
    if counts != (1, 1, 1):
        raise ContractError("projection_markers_ambiguous")
    header = HEADER_RE.search(normalized)
    begin = normalized.find(BEGIN_MARKER)
    end = normalized.find(END_MARKER)
    if header is None or header.end() > begin or end < begin:
        raise ContractError("projection_markers_ambiguous")
    if normalized[header.end() : begin].strip():
        raise ContractError("projection_markers_ambiguous")
    return header, begin, end


def generated_common_block(text: str) -> tuple[str, str] | None:
    """生成投影から (宣言 hash, 共通ブロック本文) を取り出す。"""

    normalized = normalize_text(text)
    span = _generated_span(normalized)
    if span is None:
        return None
    header, begin, end = span
    # render は BEGIN の直後に区切りの改行 1 個だけを足すので、その 1 個だけ
    # 剥ぐ。lstrip だと source 先頭の空行まで消えて恒久 mismatch になる。
    block = normalized[begin + len(BEGIN_MARKER) : end].removeprefix("\n")
    return header.group(1), block


def render_materialized(source_text: str, existing: str | None = None) -> str:
    """共通ブロックを描画し、生成ファイルの overlay (前置・後置) を保存する。"""

    if source_contains_markers(source_text):
        raise ContractError("source_contains_projection_markers")
    source = canonical_source_text(source_text)
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    generated = (
        f"<!-- repo-preflight:ai-constitution source-sha256={source_hash} -->\n"
        f"{BEGIN_MARKER}\n"
        f"{source}"
        f"{END_MARKER}\n"
    )
    if existing is None:
        return generated

    normalized = normalize_text(existing)
    span = _generated_span(normalized)
    if span is None:
        raise ContractError("existing_target_not_generated")
    header, _begin, end = span
    suffix = normalized[end + len(END_MARKER) :].removeprefix("\n")
    return normalized[: header.start()] + generated + suffix


def _entry_result(
    entry: dict[str, Any], *, status: str, findings: list[str]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": entry.get("id"),
        "runtime": entry.get("runtime"),
        "strategy": entry.get("strategy"),
        "required": bool(entry.get("required", True)),
        "status": status,
        "findings": findings,
    }
    if entry.get("strategy") == "manual" and isinstance(entry.get("evidence"), str):
        # human_review 判定を受けた人がレポートだけで確認先へ辿れるようにする。
        result["evidence"] = entry["evidence"]
    return result


def _tool_error(
    findings: list[str], *, source: dict[str, Any] | None = None
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "tool_error",
        "findings": findings,
        "entries": [],
    }
    if source is not None:
        report["source"] = source
    return report


def load_manifest(path: Path) -> dict[str, Any]:
    """manifest を読み込み、schema と同等の構造検証を fail-closed で行う。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ContractError("manifest_shape_invalid")
    if set(payload) != {"schema", "source", "entries"}:
        raise ContractError("manifest_fields_mismatch")
    if payload.get("schema") != SCHEMA:
        raise ContractError("manifest_schema_mismatch")
    if not isinstance(payload.get("source"), str) or not payload["source"].strip():
        raise ContractError("manifest_source_invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ContractError("manifest_entries_missing")

    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ContractError("manifest_entry_ids_invalid")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ContractError("manifest_entry_ids_invalid")
        ids.append(entry_id)
        if set(entry) - _entry_fields():
            raise ContractError(f"manifest_entry_fields_invalid:{entry_id}")
        if not isinstance(entry.get("runtime"), str) or not entry["runtime"].strip():
            raise ContractError(f"manifest_runtime_missing:{entry_id}")
        if not isinstance(entry.get("strategy"), str):
            raise ContractError(f"manifest_strategy_invalid:{entry_id}")
        if "required" in entry and not isinstance(entry["required"], bool):
            raise ContractError(f"manifest_required_invalid:{entry_id}")
        if entry.get("strategy") not in STRATEGIES:
            raise ContractError(f"manifest_strategy_invalid:{entry_id}")
        _validate_pointer_kind(entry)
        if entry["strategy"] == "manual":
            if (
                not isinstance(entry.get("evidence"), str)
                or not entry["evidence"].strip()
            ):
                raise ContractError(f"manifest_evidence_missing:{entry_id}")
        elif "path" not in entry or not entry["path"]:
            raise ContractError(f"manifest_path_missing:{entry_id}")
        elif not isinstance(entry["path"], str):
            raise ContractError(f"manifest_path_invalid:{entry_id}")
        if "evidence" in entry and not isinstance(entry["evidence"], str):
            raise ContractError(f"manifest_evidence_invalid:{entry_id}")
    if len(set(ids)) != len(ids):
        raise ContractError("manifest_entry_ids_invalid")
    return payload


def _check_materialized(
    entry: dict[str, Any],
    target_text: str,
    *,
    expected: str,
    source_hash: str,
    source_has_markers: bool,
) -> dict[str, Any]:
    """materialized entry 1 件の生成ブロックを検査する。"""

    if source_has_markers:
        return _entry_result(
            entry, status="stale", findings=["source_contains_projection_markers"]
        )
    try:
        generated = generated_common_block(target_text)
    except ValueError:
        return _entry_result(
            entry, status="stale", findings=["projection_markers_ambiguous"]
        )
    if generated is None:
        return _entry_result(
            entry, status="stale", findings=["generated_projection_markers_missing"]
        )
    declared_hash, common_block = generated
    findings: list[str] = []
    if declared_hash != source_hash:
        findings.append("source_hash_mismatch")
    if common_block != expected:
        findings.append("common_block_mismatch")
    return _entry_result(
        entry, status="pass" if not findings else "stale", findings=findings
    )


def _check_entry(
    entry: dict[str, Any],
    *,
    source: Path,
    expected: str,
    source_hash: str,
    source_has_markers: bool,
    home: Path,
    project: Path | None,
) -> dict[str, Any]:
    """entry 1 件を検査する。

    path の解決失敗は manifest 自体の問題としてそのまま ValueError を送出し、
    呼び出し側でレポート全体を tool_error にする。
    """

    strategy = entry["strategy"]
    if strategy == "manual":
        return _entry_result(
            entry,
            status="human_review",
            findings=["manual_runtime_evidence_required"],
        )

    target = resolve_template(entry["path"], home=home, project=project)
    if not target.is_file():
        return _entry_result(entry, status="missing", findings=["entry_missing"])
    try:
        target_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _entry_result(
            entry,
            status="tool_error",
            findings=[f"entry_unreadable:{type(exc).__name__}"],
        )

    if strategy == "pointer":
        ok = has_pointer(
            target_text,
            source,
            home=home,
            base=target.parent,
            pointer_kind=entry.get("pointer_kind", "import"),
        )
        return _entry_result(
            entry,
            status="pass" if ok else "stale",
            findings=[] if ok else ["source_pointer_missing"],
        )
    return _check_materialized(
        entry,
        target_text,
        expected=expected,
        source_hash=source_hash,
        source_has_markers=source_has_markers,
    )


def _overall_status(required_failures: list[dict[str, Any]]) -> str:
    """required entry の失敗から全体 status を決める。"""

    if not required_failures:
        return "pass"
    if all(result["status"] == "human_review" for result in required_failures):
        # 残っているのが manual の確認待ちだけなら、drift とは区別して返す。
        return "human_review"
    return "blocked"


def check_manifest(
    manifest_path: Path, *, home: Path, project: Path | None = None
) -> dict[str, Any]:
    """全 entry を検査し、secret-safe な JSON レポートを返す。"""

    try:
        manifest = load_manifest(manifest_path)
        source = resolve_template(manifest["source"], home=home, project=project)
    except json.JSONDecodeError:
        return _tool_error(["manifest_json_invalid"])
    except (OSError, UnicodeError) as exc:
        return _tool_error([f"manifest_unreadable:{type(exc).__name__}"])
    except (ValueError, KeyError, TypeError) as exc:
        return _tool_error([_safe_error_code(exc, "manifest_invalid")])

    if not source.is_file():
        return _tool_error(["source_missing"], source={"exists": False})

    try:
        source_text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _tool_error(
            [f"source_unreadable:{type(exc).__name__}"], source={"exists": True}
        )
    expected = canonical_source_text(source_text)
    source_hash = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    source_summary = {"exists": True, "sha256": source_hash}
    source_has_markers = source_contains_markers(source_text)

    try:
        results = [
            _check_entry(
                entry,
                source=source,
                expected=expected,
                source_hash=source_hash,
                source_has_markers=source_has_markers,
                home=home,
                project=project,
            )
            for entry in manifest["entries"]
        ]
    except (OSError, ValueError, TypeError) as exc:
        return _tool_error(
            [_safe_error_code(exc, "entry_invalid")], source=source_summary
        )

    required_failures = [
        result
        for result in results
        if result["required"] and result["status"] != "pass"
    ]
    return {
        "schema": SCHEMA,
        "status": _overall_status(required_failures),
        "source": source_summary,
        "entries": results,
        "findings": [
            f"{result['id']}:{finding}"
            for result in required_failures
            for finding in result["findings"]
        ],
    }


def _prepare_apply(
    manifest_path: Path, *, entry_id: str, home: Path, project: Path | None
) -> tuple[Path, str, bool]:
    """apply 対象を検証し (target, 描画結果, 既存 target の有無) を返す。"""

    manifest = load_manifest(manifest_path)
    entry = next((item for item in manifest["entries"] if item["id"] == entry_id), None)
    if entry is None:
        raise ContractError("entry_id_unknown")
    if entry["strategy"] != "materialized":
        raise ContractError("apply_requires_materialized_entry")
    source = resolve_template(manifest["source"], home=home, project=project)
    if not source.is_file():
        raise ContractError("source_missing")
    target = resolve_template(entry["path"], home=home, project=project)
    if _same_path(source, target):
        # 正本自体へ投影を書くと marker がネストして正本が壊れる。
        raise ContractError("source_target_identical")
    source_text = source.read_text(encoding="utf-8")
    if source_contains_markers(source_text):
        raise ContractError("source_contains_projection_markers")
    existing = target.read_text(encoding="utf-8") if target.exists() else None
    rendered = render_materialized(source_text, existing=existing)
    return target, rendered, existing is not None


def _write_atomically(target: Path, rendered: str, *, keep_mode: bool) -> None:
    """一時ファイル経由で target を置換する。既存 target のモードを引き継ぐ。"""

    existing_mode = stat.S_IMODE(os.stat(target).st_mode) if keep_mode else None
    fd: int | None = None
    temporary: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".repo-preflight-entry-", dir=target.parent
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(rendered)
        if existing_mode is not None:
            # mkstemp は 0600 で作るため、既存 target のモードを引き継がないと
            # os.replace 後に POSIX でパーミッションが黙って狭まる。
            os.chmod(temporary, existing_mode)
        os.replace(temporary, target)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def apply_entry(
    manifest_path: Path,
    *,
    entry_id: str,
    home: Path,
    project: Path | None = None,
) -> dict[str, Any]:
    """materialized entry 1 件を適用し、適用後の検査レポートを返す。

    成功時はレポートに ``applied_entry`` を付け、書き込みが完了した entry を
    exit code や他 entry の状態と独立に識別できるようにする。エラーメッセージは
    絶対パスを含めない (secret-safe)。
    """

    try:
        target, rendered, existed = _prepare_apply(
            manifest_path, entry_id=entry_id, home=home, project=project
        )
    except json.JSONDecodeError:
        return _tool_error(["manifest_json_invalid"])
    except UnicodeError as exc:
        # UnicodeDecodeError は ValueError の subclass なので先に捕捉する。
        return _tool_error([f"apply_failed:{type(exc).__name__}"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _tool_error([_safe_error_code(exc, "apply_failed")])

    try:
        _write_atomically(target, rendered, keep_mode=existed)
    except (OSError, ValueError) as exc:
        # os.replace 等は NUL 等の不正パスで ValueError を送出する。例外文には
        # 絶対パスが入るため、書き込み失敗も型名だけの tool_error にする。
        return _tool_error([f"target_write_failed:{type(exc).__name__}"])
    report = check_manifest(manifest_path, home=home, project=project)
    report["applied_entry"] = entry_id
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--project", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--entry-id")
    args = parser.parse_args(argv)

    manifest = args.manifest.resolve()
    home = args.home.resolve()
    project = args.project.resolve() if args.project else None
    if args.apply and not args.entry_id:
        report = _tool_error(["apply_requires_entry_id"])
    elif args.entry_id and not args.apply:
        # entry 単位の read-only 検査は提供していない。全件検査が黙って走ると
        # 「1 件だけ確認した」と誤読させるため、明示エラーにする。
        report = _tool_error(["entry_id_requires_apply"])
    else:
        try:
            if args.apply:
                report = apply_entry(
                    manifest, entry_id=args.entry_id, home=home, project=project
                )
            else:
                report = check_manifest(manifest, home=home, project=project)
        except Exception as exc:  # noqa: BLE001 - secret-safe の最終防衛線
            # 想定外の例外でも traceback (絶対パスや username を含む) を出さず、
            # blocked と区別できる tool_error の JSON にする。
            report = _tool_error([f"internal_error:{type(exc).__name__}"])

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_CODES.get(report.get("status"), 2)


if __name__ == "__main__":
    raise SystemExit(main())
