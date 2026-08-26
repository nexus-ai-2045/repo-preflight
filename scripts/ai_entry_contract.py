#!/usr/bin/env python3
"""Check and safely materialize AI constitution entry points.

The common constitution is a source document. Runtime entry points may either
support a source pointer (for example Claude/Gemini ``@`` imports or a Codex
instruction pointer), contain a generated copy of the source, or need manual
product-level evidence (for example Cursor user settings).

The command is read-only by default. ``--apply --entry-id`` is required for a
single, explicitly selected materialized target. Existing non-generated files
are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
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


def normalize_text(value: str) -> str:
    """Normalize line endings without changing the source's content."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def canonical_source_text(value: str) -> str:
    """Use one trailing newline convention for hashes and projections."""

    return normalize_text(value).rstrip("\n") + "\n"


def resolve_template(value: str, *, home: Path, project: Path | None) -> Path:
    """Resolve only explicit, portable path placeholders."""

    if "{PROJECT}" in value and project is None:
        raise ValueError("project_required")
    expanded = value.replace("{HOME}", str(home))
    if project is not None:
        expanded = expanded.replace("{PROJECT}", str(project))
    return Path(os.path.expanduser(expanded)).resolve()


def path_forms(path: Path) -> set[str]:
    """Return slash variants using the host path case semantics."""

    raw = str(path)
    forms = {
        raw.replace("/", "\\"),
        raw.replace("\\", "/"),
        path.as_posix(),
    }
    return {form.casefold() for form in forms} if os.name == "nt" else forms


def has_pointer(text: str, source: Path, *, pointer_kind: str = "import") -> bool:
    """Check a runtime-specific explicit pointer without reading source content."""

    forms = path_forms(source)
    for line in normalize_text(text).splitlines():
        normalized = line.replace("\\", "/")
        if os.name == "nt":
            normalized = normalized.casefold()
        normalized_forms = {form.replace("\\", "/") for form in forms}
        if pointer_kind == "import":
            pointer_forms = ["@" + form for form in normalized_forms]
            if any(pointer in normalized for pointer in pointer_forms):
                return True
        elif pointer_kind == "instruction":
            code_spans = re.findall(r"`([^`]+)`", normalized)
            if any(span in normalized_forms for span in code_spans):
                return True
        else:
            raise ValueError("pointer_kind_invalid")
    return False


def _validate_pointer_kind(entry: dict[str, Any]) -> None:
    """Validate the optional pointer syntax discriminator."""

    pointer_kind = entry.get("pointer_kind", "import")
    if entry["strategy"] != "pointer" and "pointer_kind" in entry:
        raise ValueError(f"manifest_pointer_kind_invalid:{entry['id']}")
    if not isinstance(pointer_kind, str) or pointer_kind not in POINTER_KINDS:
        raise ValueError(f"manifest_pointer_kind_invalid:{entry['id']}")


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


def generated_common_block(text: str) -> tuple[str, str] | None:
    """Return (declared hash, common block) for a generated projection."""

    normalized = normalize_text(text)
    header = HEADER_RE.search(normalized)
    begin = normalized.find(BEGIN_MARKER)
    end = normalized.find(END_MARKER)
    if header is None or begin < 0 or end < begin:
        return None
    begin_content = begin + len(BEGIN_MARKER)
    block = normalized[begin_content:end].lstrip("\n")
    return header.group(1), block


def render_materialized(source_text: str, existing: str | None = None) -> str:
    """Render the generated common block and preserve a generated overlay."""

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
    header = HEADER_RE.search(normalized)
    begin = normalized.find(BEGIN_MARKER)
    end = normalized.find(END_MARKER)
    if header is None or begin < 0 or end < begin:
        raise ValueError("existing_target_not_generated")
    start = header.start() if header.end() <= begin else begin
    suffix = normalized[end + len(END_MARKER) :]
    return normalized[:start] + generated + suffix.lstrip("\n")


def _entry_result(
    entry: dict[str, Any], *, status: str, findings: list[str]
) -> dict[str, Any]:
    return {
        "id": entry.get("id"),
        "runtime": entry.get("runtime"),
        "strategy": entry.get("strategy"),
        "required": bool(entry.get("required", True)),
        "status": status,
        "findings": findings,
    }


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest_shape_invalid")
    if set(payload) != {"schema", "source", "entries"}:
        raise ValueError("manifest_fields_mismatch")
    if payload.get("schema") != SCHEMA:
        raise ValueError("manifest_schema_mismatch")
    if not isinstance(payload.get("source"), str) or not payload["source"].strip():
        raise ValueError("manifest_source_invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest_entries_missing")

    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("manifest_entry_ids_invalid")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("manifest_entry_ids_invalid")
        ids.append(entry_id)
        if set(entry) - _entry_fields():
            raise ValueError(f"manifest_entry_fields_invalid:{entry_id}")
        if not isinstance(entry.get("runtime"), str) or not entry["runtime"].strip():
            raise ValueError(f"manifest_runtime_missing:{entry_id}")
        if not isinstance(entry.get("strategy"), str):
            raise ValueError(f"manifest_strategy_invalid:{entry_id}")
        if "required" in entry and not isinstance(entry["required"], bool):
            raise ValueError(f"manifest_required_invalid:{entry_id}")
        if entry.get("strategy") not in STRATEGIES:
            raise ValueError(f"manifest_strategy_invalid:{entry_id}")
        _validate_pointer_kind(entry)
        if entry["strategy"] == "manual":
            if (
                not isinstance(entry.get("evidence"), str)
                or not entry["evidence"].strip()
            ):
                raise ValueError(f"manifest_evidence_missing:{entry_id}")
        elif "path" not in entry or not entry["path"]:
            raise ValueError(f"manifest_path_missing:{entry_id}")
        elif not isinstance(entry["path"], str):
            raise ValueError(f"manifest_path_invalid:{entry_id}")
        if "evidence" in entry and not isinstance(entry["evidence"], str):
            raise ValueError(f"manifest_evidence_invalid:{entry_id}")
    if len(set(ids)) != len(ids):
        raise ValueError("manifest_entry_ids_invalid")
    return payload


def check_manifest(
    manifest_path: Path, *, home: Path, project: Path | None = None
) -> dict[str, Any]:
    """Check all entries and return a secret-safe JSON report."""

    try:
        manifest = load_manifest(manifest_path)
        source = resolve_template(manifest["source"], home=home, project=project)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        return {
            "schema": SCHEMA,
            "status": "tool_error",
            "findings": [str(exc)],
            "entries": [],
        }

    if not source.is_file():
        return {
            "schema": SCHEMA,
            "status": "tool_error",
            "source": {"exists": False},
            "findings": ["source_missing"],
            "entries": [],
        }

    try:
        source_text = source.read_text(encoding="utf-8")
        source_hash = hashlib.sha256(
            canonical_source_text(source_text).encode("utf-8")
        ).hexdigest()
    except (OSError, UnicodeError) as exc:
        return {
            "schema": SCHEMA,
            "status": "tool_error",
            "source": {"exists": True},
            "findings": [f"source_unreadable:{type(exc).__name__}"],
            "entries": [],
        }

    results: list[dict[str, Any]] = []
    for entry in manifest["entries"]:
        strategy = entry["strategy"]
        if strategy == "manual":
            results.append(
                _entry_result(
                    entry,
                    status="human_review",
                    findings=["manual_runtime_evidence_required"],
                )
            )
            continue

        try:
            target = resolve_template(entry["path"], home=home, project=project)
        except (TypeError, ValueError) as exc:
            return {
                "schema": SCHEMA,
                "status": "tool_error",
                "source": {"exists": True, "sha256": source_hash},
                "findings": [str(exc)],
                "entries": [],
            }
        if not target.is_file():
            results.append(
                _entry_result(entry, status="missing", findings=["entry_missing"])
            )
            continue
        try:
            target_text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            results.append(
                _entry_result(
                    entry,
                    status="tool_error",
                    findings=[f"entry_unreadable:{type(exc).__name__}"],
                )
            )
            continue

        if strategy == "pointer":
            ok = has_pointer(
                target_text,
                source,
                pointer_kind=entry.get("pointer_kind", "import"),
            )
            results.append(
                _entry_result(
                    entry,
                    status="pass" if ok else "stale",
                    findings=[] if ok else ["source_pointer_missing"],
                )
            )
            continue

        generated = generated_common_block(target_text)
        if generated is None:
            results.append(
                _entry_result(
                    entry,
                    status="stale",
                    findings=["generated_projection_markers_missing"],
                )
            )
            continue
        declared_hash, common_block = generated
        expected = canonical_source_text(source_text)
        findings: list[str] = []
        if declared_hash != source_hash:
            findings.append("source_hash_mismatch")
        if common_block != expected:
            findings.append("common_block_mismatch")
        results.append(
            _entry_result(
                entry,
                status="pass" if not findings else "stale",
                findings=findings,
            )
        )

    required_failures = [
        result
        for result in results
        if result["required"] and result["status"] != "pass"
    ]
    status = "blocked" if required_failures else "pass"
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


def apply_entry(
    manifest_path: Path,
    *,
    entry_id: str,
    home: Path,
    project: Path | None = None,
) -> dict[str, Any]:
    """Apply one materialized entry, then return a fresh check report."""

    try:
        manifest = load_manifest(manifest_path)
        entry = next(item for item in manifest["entries"] if item["id"] == entry_id)
        if entry["strategy"] != "materialized":
            raise ValueError("apply_requires_materialized_entry")
        source = resolve_template(manifest["source"], home=home, project=project)
        if not source.is_file():
            raise ValueError("source_missing")
        source_text = source.read_text(encoding="utf-8")
        target = resolve_template(entry["path"], home=home, project=project)
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        rendered = render_materialized(source_text, existing=existing)
    except StopIteration as exc:
        return {
            "schema": SCHEMA,
            "status": "tool_error",
            "findings": ["entry_id_unknown"],
        }
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        KeyError,
        UnicodeError,
    ) as exc:
        return {"schema": SCHEMA, "status": "tool_error", "findings": [str(exc)]}

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
        os.replace(temporary, target)
    except OSError as exc:
        return {
            "schema": SCHEMA,
            "status": "tool_error",
            "findings": [f"target_write_failed:{type(exc).__name__}"],
        }
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
    return check_manifest(manifest_path, home=home, project=project)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--project", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--entry-id")
    args = parser.parse_args(argv)

    manifest = args.manifest.resolve()
    if args.apply and not args.entry_id:
        report = {
            "schema": SCHEMA,
            "status": "tool_error",
            "findings": ["apply_requires_entry_id"],
        }
    elif args.apply:
        report = apply_entry(
            manifest,
            entry_id=args.entry_id,
            home=args.home.resolve(),
            project=args.project.resolve() if args.project else None,
        )
    else:
        report = check_manifest(
            manifest,
            home=args.home.resolve(),
            project=args.project.resolve() if args.project else None,
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
