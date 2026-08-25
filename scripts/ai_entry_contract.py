#!/usr/bin/env python3
"""AI 憲法エントリーポイントの検査と、安全な materialized 投影を行う。

共通憲法はソース文書であり、各ランタイムの入口は 3 戦略のいずれかを取る:
``pointer`` (Claude/Gemini の ``@`` import のようにソースを参照する)、
``materialized`` (import 構文を持たないランタイム向けにソースの生成コピーを持つ)、
``manual`` (Cursor のユーザー設定のように製品側での人手確認が必要)。

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
ENTRY_FIELDS = {"id", "runtime", "path", "strategy", "required", "evidence"}
POINTER_TOKEN_RE = re.compile(r"@([^@\s\"'`<>|;,()\[\]]+)")
EXIT_CODES = {"pass": 0, "blocked": 1, "tool_error": 2, "human_review": 3}


def normalize_text(value: str) -> str:
    """本文を変えずに改行コードだけを LF へ正規化する。"""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def canonical_source_text(value: str) -> str:
    """hash と投影で共通の末尾改行 1 個の形へそろえる。"""

    return normalize_text(value).rstrip("\n") + "\n"


def resolve_template(value: str, *, home: Path, project: Path | None) -> Path:
    """明示的でポータブルな placeholder ({HOME}/{PROJECT}) だけを解決する。

    ``~`` は実行ユーザーの実 home に暗黙依存し ``--home`` の差し替えを迂回する
    ため受け付けない (fail-closed)。置換後に placeholder が残った場合も拒否する。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("path_value_invalid")
    if value.lstrip().startswith("~"):
        raise ValueError("tilde_unsupported_use_home_placeholder")
    if "{PROJECT}" in value and project is None:
        raise ValueError("project_required")
    expanded = value.replace("{HOME}", str(home))
    if project is not None:
        expanded = expanded.replace("{PROJECT}", str(project))
    if "{HOME}" in expanded or "{PROJECT}" in expanded:
        raise ValueError("template_placeholder_unresolved")
    return Path(expanded).resolve()


def _iter_pointer_lines(text: str) -> Iterator[str]:
    """コードフェンス外の行を、行内コードスパンと HTML コメントを除いて返す。"""

    in_fence = False
    for line in normalize_text(text).splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        cleaned = re.sub(r"`[^`]*`", "", line)
        yield re.sub(r"<!--.*?-->", "", cleaned)


def _candidate_paths(token: str, *, home: Path, base: Path) -> Iterator[Path]:
    """pointer トークンをファイルパス候補へ解決する。

    ``~/`` は gate に渡された home で展開する (実 home ではない)。相対パスは
    entry ファイルのあるディレクトリ基準で解決する。
    """

    for raw in dict.fromkeys((token, token.rstrip(".,:;!?"))):
        if not raw:
            continue
        if raw == "~" or raw.startswith("~/"):
            raw = str(home) + raw[1:]
        elif raw.startswith("~"):
            continue
        try:
            path = Path(raw)
            yield (path if path.is_absolute() else base / path).resolve()
        except (OSError, ValueError):
            continue


def has_pointer(text: str, source: Path, *, home: Path, base: Path) -> bool:
    """``@<path>`` pointer が実際に source ファイルへ到達するかを判定する。

    部分文字列一致ではなくパス解決で比較する。存在するパスは ``samefile`` で
    (大文字小文字非区別ファイルシステムや symlink を含めて) 同一性を判定し、
    存在しないパスだけ正規化文字列の一致に fallback する。
    """

    source_resolved = source.resolve()
    source_norm = os.path.normcase(str(source_resolved))
    for line in _iter_pointer_lines(text):
        for token in POINTER_TOKEN_RE.findall(line):
            for candidate in _candidate_paths(token, home=home, base=base):
                try:
                    if candidate.samefile(source_resolved):
                        return True
                except (OSError, ValueError):
                    if os.path.normcase(str(candidate)) == source_norm:
                        return True
    return False


def _marker_counts(normalized: str) -> tuple[int, int, int]:
    """(header, begin, end) 各マーカーの出現数を返す。"""

    return (
        len(HEADER_RE.findall(normalized)),
        normalized.count(BEGIN_MARKER),
        normalized.count(END_MARKER),
    )


def source_contains_markers(source_text: str) -> bool:
    """source 本文が投影マーカー自体を含むか (含む場合は投影不能)。"""

    headers, begins, ends = _marker_counts(normalize_text(source_text))
    return bool(headers or begins or ends)


def _generated_span(normalized: str) -> tuple[re.Match[str], int, int] | None:
    """一意な生成ブロックの (header, begin, end) を返す。

    マーカーが 1 つも無ければ None。個数が 1 ずつでない・順序が壊れている・
    header と begin の間に本文がある場合は曖昧として ValueError にする
    (最初の出現だけを黙って採用すると、marker を引用した overlay や
    複製ブロックを破壊・見逃しするため)。
    """

    headers, begins, ends = _marker_counts(normalized)
    if headers == 0 and begins == 0 and ends == 0:
        return None
    if headers != 1 or begins != 1 or ends != 1:
        raise ValueError("projection_markers_ambiguous")
    header = HEADER_RE.search(normalized)
    begin = normalized.find(BEGIN_MARKER)
    end = normalized.find(END_MARKER)
    if header is None or begin < 0 or end < begin or header.start() > begin:
        raise ValueError("projection_markers_ambiguous")
    if normalized[header.end() : begin].strip():
        raise ValueError("projection_markers_ambiguous")
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
        raise ValueError("source_contains_projection_markers")
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
        raise ValueError("existing_target_not_generated")
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
        raise ValueError("manifest_shape_invalid")
    if payload.get("schema") != SCHEMA:
        raise ValueError("manifest_schema_mismatch")
    if not isinstance(payload.get("source"), str) or not payload["source"].strip():
        raise ValueError("manifest_source_invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest_entries_missing")

    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("manifest_entry_not_object")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("manifest_entry_id_invalid")
        if entry_id in seen_ids:
            raise ValueError(f"manifest_entry_id_duplicate:{entry_id}")
        seen_ids.add(entry_id)
        if set(entry) - ENTRY_FIELDS:
            raise ValueError(f"manifest_entry_fields_unknown:{entry_id}")
        if not isinstance(entry.get("runtime"), str) or not entry["runtime"].strip():
            raise ValueError(f"manifest_runtime_missing:{entry_id}")
        if entry.get("strategy") not in STRATEGIES:
            raise ValueError(f"manifest_strategy_invalid:{entry_id}")
        if "required" in entry and not isinstance(entry["required"], bool):
            raise ValueError(f"manifest_required_invalid:{entry_id}")
        if entry["strategy"] == "manual":
            if not isinstance(entry.get("evidence"), str) or not entry["evidence"].strip():
                raise ValueError(f"manifest_evidence_missing:{entry_id}")
        elif not isinstance(entry.get("path"), str) or not entry["path"].strip():
            raise ValueError(f"manifest_path_missing:{entry_id}")
    return payload


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
    """entry 1 件を検査する。エラーはこの entry の結果に閉じ込める。"""

    strategy = entry["strategy"]
    if strategy == "manual":
        return _entry_result(
            entry,
            status="human_review",
            findings=["manual_runtime_evidence_required"],
        )

    try:
        target = resolve_template(entry["path"], home=home, project=project)
    except ValueError as exc:
        return _entry_result(entry, status="tool_error", findings=[str(exc)])
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
        ok = has_pointer(target_text, source, home=home, base=target.parent)
        return _entry_result(
            entry,
            status="pass" if ok else "stale",
            findings=[] if ok else ["source_pointer_missing"],
        )

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


def check_manifest(
    manifest_path: Path, *, home: Path, project: Path | None = None
) -> dict[str, Any]:
    """全 entry を検査し、secret-safe な JSON レポートを返す。"""

    try:
        manifest = load_manifest(manifest_path)
        source = resolve_template(manifest["source"], home=home, project=project)
    except json.JSONDecodeError:
        return _tool_error(["manifest_json_invalid"])
    except OSError as exc:
        return _tool_error([f"manifest_unreadable:{type(exc).__name__}"])
    except ValueError as exc:
        return _tool_error([str(exc)])
    except (KeyError, TypeError) as exc:
        return _tool_error([f"manifest_invalid:{type(exc).__name__}"])

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
    source_has_markers = source_contains_markers(source_text)

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

    required_failures = [
        result for result in results if result["required"] and result["status"] != "pass"
    ]
    if not required_failures:
        status = "pass"
    elif all(result["status"] == "human_review" for result in required_failures):
        # 残っているのが manual の確認待ちだけなら、drift とは区別して返す。
        status = "human_review"
    else:
        status = "blocked"
    return {
        "schema": SCHEMA,
        "status": status,
        "source": {"exists": True, "sha256": source_hash},
        "entries": results,
        "findings": [
            f"{result['id']}:{finding}"
            for result in required_failures
            for finding in result["findings"]
        ],
    }


def _same_path(a: Path, b: Path) -> bool:
    """resolve 済みパス 2 つが同一ファイルを指すか。"""

    try:
        return a.samefile(b)
    except (OSError, ValueError):
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


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
        manifest = load_manifest(manifest_path)
        entry = next(
            (item for item in manifest["entries"] if item["id"] == entry_id), None
        )
        if entry is None:
            raise ValueError("entry_id_unknown")
        if entry["strategy"] != "materialized":
            raise ValueError("apply_requires_materialized_entry")
        source = resolve_template(manifest["source"], home=home, project=project)
        if not source.is_file():
            raise ValueError("source_missing")
        source_text = source.read_text(encoding="utf-8")
        if source_contains_markers(source_text):
            raise ValueError("source_contains_projection_markers")
        target = resolve_template(entry["path"], home=home, project=project)
        if _same_path(source, target):
            raise ValueError("source_target_identical")
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        rendered = render_materialized(source_text, existing=existing)
    except json.JSONDecodeError:
        return _tool_error(["manifest_json_invalid"])
    except ValueError as exc:
        return _tool_error([str(exc)])
    except (OSError, KeyError, TypeError, UnicodeError) as exc:
        return _tool_error([f"apply_failed:{type(exc).__name__}"])

    existing_mode = (
        stat.S_IMODE(os.stat(target).st_mode) if existing is not None else None
    )
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
    except OSError as exc:
        return _tool_error([f"target_write_failed:{type(exc).__name__}"])
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
    elif args.apply:
        report = apply_entry(manifest, entry_id=args.entry_id, home=home, project=project)
    else:
        report = check_manifest(manifest, home=home, project=project)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_CODES.get(report.get("status"), 2)


if __name__ == "__main__":
    raise SystemExit(main())
