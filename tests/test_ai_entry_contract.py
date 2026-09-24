from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# scripts/ は package ではない (scripts/__init__.py が無い)。他のテストと同じく
# ファイルパスから読み込み、repo root が sys.path に無い素の pytest でも collect できる。
_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_entry_contract.py"
_SPEC = importlib.util.spec_from_file_location("ai_entry_contract", _MODULE_PATH)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


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


def test_pointer_case_matching_follows_filesystem_semantics(tmp_path: Path) -> None:
    # 大文字小文字の同一視は OS 名ではなくファイルシステムの性質。判定は
    # samefile ベースなので、case 違いの pointer が実際にファイルへ届くなら
    # pass、届かないなら blocked になる (macOS の APFS 既定は posix だが
    # case-insensitive)。
    home = tmp_path / "home"
    home.mkdir()
    source = home / "AI-CONSTITUTION.md"
    source.write_text("source\n", encoding="utf-8")
    variant = Path(str(source).replace("AI-CONSTITUTION", "ai-constitution"))
    (home / "CLAUDE.md").write_text(f"@{variant}\n", encoding="utf-8")
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

    try:
        reaches = variant.samefile(source)
    except OSError:
        reaches = False
    if reaches:
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


def test_optional_entry_does_not_block_selected_manifest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text("source\n", encoding="utf-8")
    (home / "CLAUDE.md").write_text(
        "@" + str(home / "AI-CONSTITUTION.md") + "\n", encoding="utf-8"
    )
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "claude",
                "runtime": "claude-code",
                "path": "{HOME}/CLAUDE.md",
                "strategy": "pointer",
            },
            {
                "id": "codex",
                "runtime": "codex",
                "path": "{HOME}/AGENTS.md",
                "strategy": "pointer",
                "pointer_kind": "instruction",
                "required": False,
            },
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "pass"
    assert report["entries"][0]["status"] == "pass"
    assert report["entries"][1]["status"] == "missing"
    assert report["findings"] == []


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

    # manual の確認待ちだけなら drift (blocked) とは区別して human_review にする。
    assert report["status"] == "human_review"
    assert report["entries"][0]["status"] == "human_review"
    assert report["entries"][0]["evidence"] == "Cursor Settings > Rules"


# ---------------------------------------------------------------------------
# PR#40 レビュー指摘 (B1〜B12) の回帰テスト。
# ---------------------------------------------------------------------------

GROK_ENTRY = {
    "id": "grok",
    "runtime": "grok",
    "path": "{HOME}/AGENTS.md",
    "strategy": "materialized",
}
CLAUDE_ENTRY = {
    "id": "claude",
    "runtime": "claude-code",
    "path": "{HOME}/CLAUDE.md",
    "strategy": "pointer",
}
CURSOR_ENTRY = {
    "id": "cursor",
    "runtime": "cursor",
    "strategy": "manual",
    "evidence": "Cursor Settings > Rules",
}


def make_home(tmp_path: Path, source_text: str = "# source\n") -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text(source_text, encoding="utf-8")
    return home


def pointer_report(tmp_path: Path, entry_text: str) -> dict:
    home = make_home(tmp_path)
    (home / "CLAUDE.md").write_text(entry_text, encoding="utf-8")
    manifest = write_manifest(tmp_path, [CLAUDE_ENTRY])
    return gate.check_manifest(manifest, home=home)


def test_b1_source_with_end_marker_is_refused_not_grown(tmp_path: Path) -> None:
    # source に END marker があると、旧実装は再 apply ごとに末尾へ残骸が増えた。
    home = make_home(tmp_path, f"# source\n{gate.END_MARKER}\ntail\n")
    manifest = write_manifest(tmp_path, [GROK_ENTRY])

    applied = gate.apply_entry(manifest, entry_id="grok", home=home)
    checked = gate.check_manifest(manifest, home=home)

    assert applied["status"] == "tool_error"
    assert applied["findings"] == ["source_contains_projection_markers"]
    assert not (home / "AGENTS.md").exists()
    assert checked["status"] == "blocked"
    assert checked["findings"] == ["grok:entry_missing"]


def test_b1_check_reports_source_markers_for_existing_projection(
    tmp_path: Path,
) -> None:
    home = make_home(tmp_path, "# source\n")
    (home / "AGENTS.md").write_text(
        gate.render_materialized("# source\n"), encoding="utf-8"
    )
    (home / "AI-CONSTITUTION.md").write_text(
        f"# source\n{gate.END_MARKER}\n", encoding="utf-8"
    )
    manifest = write_manifest(tmp_path, [GROK_ENTRY])

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["grok:source_contains_projection_markers"]


def test_b1_reapply_is_idempotent_and_keeps_overlay(tmp_path: Path) -> None:
    home = make_home(tmp_path, "# source\n")
    target = home / "AGENTS.md"
    target.write_text(
        "# prefix overlay\n"
        + gate.render_materialized("# old\n")
        + "\n\n# suffix overlay\n",
        encoding="utf-8",
    )
    manifest = write_manifest(tmp_path, [GROK_ENTRY])

    first = gate.apply_entry(manifest, entry_id="grok", home=home)
    once = target.read_text(encoding="utf-8")
    second = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert first["status"] == second["status"] == "pass"
    assert target.read_text(encoding="utf-8") == once
    assert once.startswith("# prefix overlay\n")
    assert once.endswith(f"{gate.END_MARKER}\n\n\n# suffix overlay\n")


def test_b2_apply_refuses_source_target_identical(tmp_path: Path) -> None:
    home = make_home(tmp_path, "# source\n")
    manifest = write_manifest(
        tmp_path, [{**GROK_ENTRY, "path": "{HOME}/AI-CONSTITUTION.md"}]
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["source_target_identical"]
    assert (home / "AI-CONSTITUTION.md").read_text(encoding="utf-8") == "# source\n"


@pytest.mark.parametrize(
    "overlay_kind",
    ["quoted_end_marker", "quoted_header", "text_between_header_and_begin"],
)
def test_b3_ambiguous_overlay_fails_closed_without_deleting(
    tmp_path: Path, overlay_kind: str
) -> None:
    home = make_home(tmp_path, "# source\n")
    generated = gate.render_materialized("# source\n")
    if overlay_kind == "quoted_end_marker":
        text = f"overlay quotes `{gate.END_MARKER}` here\n" + generated
    elif overlay_kind == "quoted_header":
        header = generated.splitlines()[0]
        text = generated + f"\nexample header: {header}\n"
    else:
        header, rest = generated.split("\n", 1)
        text = f"{header}\nuser note between header and begin\n{rest}"
    target = home / "AGENTS.md"
    target.write_text(text, encoding="utf-8")
    manifest = write_manifest(tmp_path, [GROK_ENTRY])

    checked = gate.check_manifest(manifest, home=home)
    applied = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert checked["status"] == "blocked"
    assert checked["findings"] == ["grok:projection_markers_ambiguous"]
    assert applied["status"] == "tool_error"
    assert applied["findings"] == ["projection_markers_ambiguous"]
    assert target.read_text(encoding="utf-8") == text


def test_b4_duplicated_generated_block_is_not_false_green(tmp_path: Path) -> None:
    home = make_home(tmp_path, "# source\n")
    generated = gate.render_materialized("# source\n")
    tampered = generated.replace("# source", "# tampered copy")
    (home / "AGENTS.md").write_text(generated + tampered, encoding="utf-8")
    manifest = write_manifest(tmp_path, [GROK_ENTRY])

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["grok:projection_markers_ambiguous"]


def test_b5_source_leading_blank_lines_round_trip(tmp_path: Path) -> None:
    home = make_home(tmp_path, "\n\n# source after blank lines\n")
    manifest = write_manifest(tmp_path, [GROK_ENTRY])

    applied = gate.apply_entry(manifest, entry_id="grok", home=home)
    checked = gate.check_manifest(manifest, home=home)

    assert applied["status"] == "pass"
    assert checked["status"] == "pass"


@pytest.mark.parametrize(
    ("path_value", "finding"),
    [
        ("~/AGENTS.md", "tilde_unsupported_use_home_placeholder"),
        ("{USERPROFILE}/AGENTS.md", "template_placeholder_unresolved"),
        ("   ", "path_value_invalid"),
    ],
)
def test_b6_resolve_template_rejects_implicit_home(
    tmp_path: Path, path_value: str, finding: str
) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(tmp_path, [{**GROK_ENTRY, "path": path_value}])

    report = gate.check_manifest(manifest, home=home)

    # #41: entry の resolve 失敗は manifest 自体の問題としてレポート全体を止める。
    assert report["status"] == "tool_error"
    assert report["findings"] == [finding]
    assert report["entries"] == []
    with pytest.raises(ValueError, match=finding):
        gate.resolve_template(path_value, home=home, project=None)


@pytest.mark.parametrize(
    "entry_text",
    [
        "```\n@{source}\n```\n",
        "例: `@{source}`\n",
        "<!-- @{source} -->\n",
        "<!--\n@{source}\n-->\n",
        "@{source}.backup\n",
        "mail@{source}\n",
    ],
    ids=["fence", "inline_code", "html_comment", "multiline_comment", "backup", "mid"],
)
def test_b7_import_pointer_examples_are_not_false_green(
    tmp_path: Path, entry_text: str
) -> None:
    source = tmp_path / "home" / "AI-CONSTITUTION.md"
    report = pointer_report(tmp_path, entry_text.format(source=source))

    assert report["status"] == "blocked"
    assert report["findings"] == ["claude:source_pointer_missing"]


@pytest.mark.parametrize(
    "entry_text",
    [
        "@~/AI-CONSTITUTION.md\n",
        "@AI-CONSTITUTION.md\n",
        "@./AI-CONSTITUTION.md\n",
        "@../home/AI-CONSTITUTION.md\n",
        "See @~/AI-CONSTITUTION.md.\n",
    ],
    ids=["home_tilde", "relative", "dot_relative", "parent_relative", "sentence"],
)
def test_b7_import_pointer_resolves_home_and_relative_paths(
    tmp_path: Path, entry_text: str
) -> None:
    report = pointer_report(tmp_path, entry_text)

    assert report["status"] == "pass"


def test_b7_tilde_pointer_uses_gate_home_not_real_home(tmp_path: Path) -> None:
    # ``~/`` は gate の home で展開する。別名のファイルへは到達しない。
    report = pointer_report(tmp_path, "@~/OTHER.md\n")

    assert report["status"] == "blocked"


def test_b8_exit_codes_distinguish_human_review(tmp_path: Path, capsys) -> None:
    home = make_home(tmp_path)
    (home / "CLAUDE.md").write_text("@~/AI-CONSTITUTION.md\n", encoding="utf-8")
    cases = [
        ([CLAUDE_ENTRY], 0, "pass"),
        ([CLAUDE_ENTRY, CURSOR_ENTRY], 3, "human_review"),
        ([CLAUDE_ENTRY, {**CURSOR_ENTRY, "required": False}], 0, "pass"),
        ([GROK_ENTRY, CURSOR_ENTRY], 1, "blocked"),
    ]
    for entries, exit_code, status in cases:
        manifest = write_manifest(tmp_path, entries)
        code = gate.main(["--manifest", str(manifest), "--home", str(home)])
        report = json.loads(capsys.readouterr().out)
        assert (code, report["status"]) == (exit_code, status)

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert gate.main(["--manifest", str(broken), "--home", str(home)]) == 2
    assert json.loads(capsys.readouterr().out)["findings"] == ["manifest_json_invalid"]


def test_b8_apply_success_is_identified_even_when_manual_remains(
    tmp_path: Path, capsys
) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(tmp_path, [GROK_ENTRY, CURSOR_ENTRY])

    code = gate.main(
        [
            "--manifest",
            str(manifest),
            "--home",
            str(home),
            "--apply",
            "--entry-id",
            "grok",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 3
    assert report["status"] == "human_review"
    assert report["applied_entry"] == "grok"
    assert report["entries"][0]["status"] == "pass"


def test_b9_os_error_details_are_not_exposed(tmp_path: Path, monkeypatch) -> None:
    home = make_home(tmp_path)
    (home / "CLAUDE.md").write_text("# entry\n", encoding="utf-8")
    manifest = write_manifest(tmp_path, [CLAUDE_ENTRY])
    secret_path = str(tmp_path / "private-user" / "secret")

    def explode(*args, **kwargs):
        raise OSError(2, "No such file or directory", secret_path)

    monkeypatch.setattr(gate, "has_pointer", explode)
    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["entry_invalid:FileNotFoundError"]
    assert "private-user" not in json.dumps(report)


def test_b9_manifest_read_failure_is_type_name_only(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    manifest_dir = tmp_path / "private-user-manifest.json"
    manifest_dir.mkdir()

    checked = gate.check_manifest(manifest_dir, home=home)
    applied = gate.apply_entry(manifest_dir, entry_id="grok", home=home)

    for report in (checked, applied):
        assert report["status"] == "tool_error"
        assert "private-user" not in json.dumps(report)
        assert all(":" in finding for finding in report["findings"])


def test_b9_stdlib_value_error_is_rounded_to_type_name() -> None:
    own = gate._safe_error_code(ValueError("manifest_path_missing:grok"), "x")
    stdlib = gate._safe_error_code(ValueError("bad value C:\\Users\\me\\a"), "x")
    path_suffix = gate._safe_error_code(ValueError("code:/home/me/secret"), "x")

    assert own == "manifest_path_missing:grok"
    assert stdlib == "x:ValueError"
    assert path_suffix == "x:ValueError"


def test_b10_apply_keeps_existing_target_mode(tmp_path: Path, monkeypatch) -> None:
    home = make_home(tmp_path)
    target = home / "AGENTS.md"
    target.write_text(gate.render_materialized("# old\n"), encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o644)
    expected_mode = target.stat().st_mode & 0o7777
    manifest = write_manifest(tmp_path, [GROK_ENTRY])
    chmod_calls: list[int] = []
    real_chmod = gate.os.chmod

    def recording_chmod(path, mode, *args, **kwargs):
        chmod_calls.append(mode)
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(gate.os, "chmod", recording_chmod)
    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "pass"
    assert chmod_calls == [expected_mode]
    if os.name != "nt":
        # mkstemp の 0600 が os.replace で残らない。
        assert target.stat().st_mode & 0o7777 == 0o644


def test_b11_entry_id_without_apply_is_error(tmp_path: Path, capsys) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(tmp_path, [GROK_ENTRY])

    code = gate.main(
        ["--manifest", str(manifest), "--home", str(home), "--entry-id", "grok"]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 2
    assert report["findings"] == ["entry_id_requires_apply"]
    assert not (home / "AGENTS.md").exists()


def test_b11_json_flag_is_removed(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(tmp_path, [GROK_ENTRY])

    with pytest.raises(SystemExit) as excinfo:
        gate.main(["--manifest", str(manifest), "--home", str(home), "--json"])

    assert excinfo.value.code == 2


def test_b12_test_module_imports_without_repo_root_on_sys_path(
    tmp_path: Path,
) -> None:
    # 素の pytest (rootdir が sys.path に入らない) でも collection error にならない。
    child_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = (
        "import runpy, sys; "
        "sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
        f"runpy.run_path({str(Path(__file__).resolve())!r})"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
