from __future__ import annotations

import json
from pathlib import Path

from scripts import ai_entry_contract as gate


def write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": gate.SCHEMA,
                "source": "{HOME}/AI-CONSTITUTION.md",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_pointer_entry_passes_and_report_is_content_safe(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("# private source\n", encoding="utf-8")
    entry = home / "CLAUDE.md"
    entry.write_text(f"@{source}\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "claude",
                "runtime": "claude-code",
                "path": "{HOME}/CLAUDE.md",
                "strategy": "pointer",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "pass"
    assert report["entries"][0]["status"] == "pass"
    assert "private source" not in json.dumps(report)


def test_instruction_pointer_entry_passes_for_non_import_loader(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("source\n", encoding="utf-8")
    (home / "AGENTS.md").write_text(
        f"共通原則の正本は、必ず次を先に読みます。\n\n`{source}`\n",
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "codex",
                "runtime": "codex",
                "path": "{HOME}/AGENTS.md",
                "strategy": "pointer",
                "pointer_kind": "instruction",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "pass"


def test_instruction_pointer_path_only_is_blocked(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("source\n", encoding="utf-8")
    (home / "AGENTS.md").write_text(
        f"参照先のパス: `{source}`\n",
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "codex",
                "runtime": "codex",
                "path": "{HOME}/AGENTS.md",
                "strategy": "pointer",
                "pointer_kind": "instruction",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["codex:source_pointer_missing"]


def test_instruction_pointer_negative_read_instruction_is_blocked(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("source\n", encoding="utf-8")
    (home / "AGENTS.md").write_text(
        f"共通原則の正本を読まないでください: `{source}`\n",
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "codex",
                "runtime": "codex",
                "path": "{HOME}/AGENTS.md",
                "strategy": "pointer",
                "pointer_kind": "instruction",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["codex:source_pointer_missing"]


def test_instruction_pointer_english_negative_read_instruction_is_blocked(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("source\n", encoding="utf-8")
    (home / "AGENTS.md").write_text(
        f"Do not read the canonical source: `{source}`\n",
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "codex",
                "runtime": "codex",
                "path": "{HOME}/AGENTS.md",
                "strategy": "pointer",
                "pointer_kind": "instruction",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["codex:source_pointer_missing"]


def test_instruction_pointer_wrong_path_is_blocked(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("source\n", encoding="utf-8")
    (home / "AGENTS.md").write_text(
        f"共通原則の正本を先に読みます: `{home / 'OTHER.md'}`\n",
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "codex",
                "runtime": "codex",
                "path": "{HOME}/AGENTS.md",
                "strategy": "pointer",
                "pointer_kind": "instruction",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["codex:source_pointer_missing"]


def test_pointer_kind_must_be_known_and_pointer_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/AGENTS.md",
                "strategy": "materialized",
                "pointer_kind": "instruction",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_pointer_kind_invalid:grok"]


def test_pointer_missing_is_blocked(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    (home / "CLAUDE.md").write_text("# no import\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "claude",
                "runtime": "claude-code",
                "path": "{HOME}/CLAUDE.md",
                "strategy": "pointer",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["claude:source_pointer_missing"]


def test_pointer_case_mismatch_is_not_accepted_on_posix(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("source\n", encoding="utf-8")
    (home / "CLAUDE.md").write_text(
        f"@{str(source).replace('AI-CONSTITUTION', 'ai-constitution')}\n",
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "claude",
                "runtime": "claude-code",
                "path": "{HOME}/CLAUDE.md",
                "strategy": "pointer",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    if gate.os.name == "nt":
        assert report["status"] == "pass"
    else:
        assert report["status"] == "blocked"
        assert report["findings"] == ["claude:source_pointer_missing"]


def test_project_placeholder_requires_project(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{PROJECT}/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["project_required"]


def test_materialized_projection_passes_and_detects_source_drift(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("# source\n", encoding="utf-8")
    target = home / "AGENTS.md"
    target.write_text(
        gate.render_materialized(source.read_text(encoding="utf-8"))
        + "\n# runtime overlay\n",
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )

    assert gate.check_manifest(manifest, home=home)["status"] == "pass"
    source.write_text("# changed\n", encoding="utf-8")
    report = gate.check_manifest(manifest, home=home)
    assert report["status"] == "blocked"
    assert report["findings"] == [
        "grok:source_hash_mismatch",
        "grok:common_block_mismatch",
    ]


def test_apply_refuses_unmarked_existing_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    (home / "AGENTS.md").write_text("existing overlay\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["existing_target_not_generated"]
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "existing overlay\n"


def test_apply_creates_one_materialized_entry_and_rechecks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/.grok/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "pass"
    assert (home / ".grok" / "AGENTS.md").is_file()


def test_apply_canonicalizes_multiple_trailing_newlines(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/.grok/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "pass"


def test_apply_returns_structured_error_when_replace_fails(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/.grok/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )

    def fail_replace(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(gate.os, "replace", fail_replace)
    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["target_write_failed:OSError"]


def test_apply_refuses_empty_existing_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    target = home / "AGENTS.md"
    target.write_text("", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["existing_target_not_generated"]
    assert target.read_text(encoding="utf-8") == ""


def test_manifest_required_must_be_boolean(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/AGENTS.md",
                "strategy": "materialized",
                "required": 0,
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_required_invalid:grok"]


def test_manifest_non_string_source_is_structured_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["source"] = 123
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_source_invalid"]


def test_manifest_non_string_path_is_structured_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "grok",
                "runtime": "grok",
                "path": "{HOME}/AGENTS.md",
                "strategy": "materialized",
            }
        ],
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["entries"][0]["path"] = 123
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_path_invalid:grok"]


def test_manual_entry_stops_at_human_review(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "cursor",
                "runtime": "cursor",
                "strategy": "manual",
                "evidence": "Cursor Settings > Rules",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["entries"][0]["status"] == "human_review"
