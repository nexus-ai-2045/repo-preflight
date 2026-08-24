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
