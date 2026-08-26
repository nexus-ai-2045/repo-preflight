from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_entry_contract.py"
_SPEC = importlib.util.spec_from_file_location("ai_entry_contract", _MODULE_PATH)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def write_manifest(tmp_path: Path, entries: list[dict], *, source: str | None = None) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": gate.SCHEMA,
                "source": source or "{HOME}/AI-CONSTITUTION.md",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return manifest


def make_home(tmp_path: Path, source_text: str = "source\n") -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AI-CONSTITUTION.md").write_text(source_text, encoding="utf-8")
    return home


def pointer_entry(path: str = "{HOME}/CLAUDE.md") -> dict:
    return {
        "id": "claude",
        "runtime": "claude-code",
        "path": path,
        "strategy": "pointer",
    }


def materialized_entry(path: str = "{HOME}/AGENTS.md") -> dict:
    return {
        "id": "grok",
        "runtime": "grok",
        "path": path,
        "strategy": "materialized",
    }


def test_pointer_entry_passes_and_report_is_content_safe(tmp_path: Path) -> None:
    home = make_home(tmp_path, "# private source\n")
    source = home / "AI-CONSTITUTION.md"
    (home / "CLAUDE.md").write_text(f"@{source}\n", encoding="utf-8")
    manifest = write_manifest(tmp_path, [pointer_entry()])

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "pass"
    assert report["entries"][0]["status"] == "pass"
    assert "private source" not in json.dumps(report)


def test_pointer_missing_is_blocked(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / "CLAUDE.md").write_text("# no import\n", encoding="utf-8")
    manifest = write_manifest(tmp_path, [pointer_entry()])

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["claude:source_pointer_missing"]


def test_pointer_case_matching_follows_filesystem_semantics(tmp_path: Path) -> None:
    # 大文字小文字の同一視は OS ではなくファイルシステムの性質。判定は
    # samefile ベースなので、case 違いの pointer が実際にファイルへ届くなら
    # pass、届かないなら blocked になる (macOS の APFS 既定は posix だが
    # case-insensitive)。
    home = make_home(tmp_path)
    source = home / "AI-CONSTITUTION.md"
    variant = Path(str(source).replace("AI-CONSTITUTION", "ai-constitution"))
    (home / "CLAUDE.md").write_text(f"@{variant}\n", encoding="utf-8")
    manifest = write_manifest(tmp_path, [pointer_entry()])

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


def test_pointer_home_tilde_notation_matches(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / "CLAUDE.md").write_text("@~/AI-CONSTITUTION.md\n", encoding="utf-8")
    manifest = write_manifest(tmp_path, [pointer_entry()])

    assert gate.check_manifest(manifest, home=home)["status"] == "pass"


def test_pointer_relative_path_resolves_from_entry_directory(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / "CLAUDE.md").write_text("@AI-CONSTITUTION.md\n", encoding="utf-8")
    manifest = write_manifest(tmp_path, [pointer_entry()])

    assert gate.check_manifest(manifest, home=home)["status"] == "pass"


def test_pointer_ignores_fences_comments_and_prefix_paths(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    source = home / "AI-CONSTITUTION.md"
    backup = Path(str(source) + ".backup")
    backup.write_text("old copy\n", encoding="utf-8")
    (home / "CLAUDE.md").write_text(
        "\n".join(
            [
                "```markdown",
                f"@{source}",
                "```",
                f"<!-- disabled: @{source} -->",
                f"example: `@{source}`",
                f"@{backup}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest = write_manifest(tmp_path, [pointer_entry()])

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["claude:source_pointer_missing"]


def test_project_placeholder_requires_project(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(tmp_path, [materialized_entry("{PROJECT}/AGENTS.md")])

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["grok:project_required"]
    assert report["entries"][0]["status"] == "tool_error"


def test_tilde_paths_are_rejected(tmp_path: Path) -> None:
    # ~ は --home の差し替えを迂回して実 home に解決されるため fail-closed。
    home = make_home(tmp_path)
    manifest = write_manifest(
        tmp_path, [pointer_entry()], source="~/AI-CONSTITUTION.md"
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["tilde_unsupported_use_home_placeholder"]


def test_materialized_projection_passes_and_detects_source_drift(
    tmp_path: Path,
) -> None:
    home = make_home(tmp_path, "# source\n")
    source = home / "AI-CONSTITUTION.md"
    target = home / "AGENTS.md"
    target.write_text(
        gate.render_materialized(source.read_text(encoding="utf-8"))
        + "\n# runtime overlay\n",
        encoding="utf-8",
    )
    manifest = write_manifest(tmp_path, [materialized_entry()])

    assert gate.check_manifest(manifest, home=home)["status"] == "pass"
    source.write_text("# changed\n", encoding="utf-8")
    report = gate.check_manifest(manifest, home=home)
    assert report["status"] == "blocked"
    assert report["findings"] == [
        "grok:source_hash_mismatch",
        "grok:common_block_mismatch",
    ]


def test_duplicate_generated_blocks_are_flagged_ambiguous(tmp_path: Path) -> None:
    # 正しいブロックの後ろに改ざん済み複製があっても最初の 1 個だけ見て pass
    # にしない。marker が一意でないファイルは fail-closed で stale にする。
    home = make_home(tmp_path, "# source\n")
    block = gate.render_materialized("# source\n")
    forged = block.replace("# source\n", "# tampered\n")
    (home / "AGENTS.md").write_text(block + forged, encoding="utf-8")
    manifest = write_manifest(tmp_path, [materialized_entry()])

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["grok:projection_markers_ambiguous"]


def test_source_containing_markers_is_rejected(tmp_path: Path) -> None:
    home = make_home(tmp_path, f"# doc\nexample: {gate.END_MARKER}\n")
    manifest = write_manifest(
        tmp_path, [materialized_entry("{HOME}/.grok/AGENTS.md")]
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["source_contains_projection_markers"]
    assert not (home / ".grok" / "AGENTS.md").exists()


def test_check_flags_marker_containing_source_for_materialized(tmp_path: Path) -> None:
    home = make_home(tmp_path, "# source\n")
    target = home / "AGENTS.md"
    target.write_text(gate.render_materialized("# source\n"), encoding="utf-8")
    (home / "AI-CONSTITUTION.md").write_text(
        f"# source\n{gate.BEGIN_MARKER}\n", encoding="utf-8"
    )
    manifest = write_manifest(tmp_path, [materialized_entry()])

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"
    assert report["findings"] == ["grok:source_contains_projection_markers"]


def test_overlay_quoting_header_is_not_destroyed(tmp_path: Path) -> None:
    # marker を引用した overlay を「最初の header」と誤認して本文を削らない。
    home = make_home(tmp_path, "# source\n")
    quoted = (
        "note: <!-- repo-preflight:ai-constitution source-sha256="
        + "a" * 64
        + " -->\n"
    )
    original = quoted + gate.render_materialized("# source\n")
    target = home / "AGENTS.md"
    target.write_text(original, encoding="utf-8")
    manifest = write_manifest(tmp_path, [materialized_entry()])

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["projection_markers_ambiguous"]
    assert target.read_text(encoding="utf-8") == original


def test_header_begin_gap_text_is_ambiguous(tmp_path: Path) -> None:
    home = make_home(tmp_path, "# source\n")
    block = gate.render_materialized("# source\n")
    mutated = block.replace(
        gate.BEGIN_MARKER, "USER NOTE\n" + gate.BEGIN_MARKER, 1
    )
    target = home / "AGENTS.md"
    target.write_text(mutated, encoding="utf-8")
    manifest = write_manifest(tmp_path, [materialized_entry()])

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["projection_markers_ambiguous"]
    assert target.read_text(encoding="utf-8") == mutated


def test_apply_round_trip_with_leading_blank_lines(tmp_path: Path) -> None:
    home = make_home(tmp_path, "\n\n# x\n")
    manifest = write_manifest(
        tmp_path, [materialized_entry("{HOME}/.grok/AGENTS.md")]
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "pass"
    assert report["applied_entry"] == "grok"


def test_reapply_is_idempotent_and_preserves_overlays(tmp_path: Path) -> None:
    home = make_home(tmp_path, "# source\n")
    target = home / "AGENTS.md"
    target.write_text(
        "intro overlay\n"
        + gate.render_materialized("# source\n")
        + "\n# suffix overlay\n",
        encoding="utf-8",
    )
    manifest = write_manifest(tmp_path, [materialized_entry()])

    first = gate.apply_entry(manifest, entry_id="grok", home=home)
    content_after_first = target.read_text(encoding="utf-8")
    second = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert first["status"] == "pass"
    assert second["status"] == "pass"
    assert target.read_text(encoding="utf-8") == content_after_first
    assert content_after_first.startswith("intro overlay\n")
    assert content_after_first.endswith("\n# suffix overlay\n")


def test_apply_refuses_unmarked_existing_target(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / "AGENTS.md").write_text("existing overlay\n", encoding="utf-8")
    manifest = write_manifest(tmp_path, [materialized_entry()])

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["existing_target_not_generated"]
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "existing overlay\n"


def test_apply_creates_one_materialized_entry_and_rechecks(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(
        tmp_path, [materialized_entry("{HOME}/.grok/AGENTS.md")]
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "pass"
    assert report["applied_entry"] == "grok"
    assert (home / ".grok" / "AGENTS.md").is_file()


def test_apply_canonicalizes_multiple_trailing_newlines(tmp_path: Path) -> None:
    home = make_home(tmp_path, "source\n\n")
    manifest = write_manifest(
        tmp_path, [materialized_entry("{HOME}/.grok/AGENTS.md")]
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "pass"


def test_apply_returns_structured_error_when_replace_fails(
    tmp_path: Path, monkeypatch
) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(
        tmp_path, [materialized_entry("{HOME}/.grok/AGENTS.md")]
    )

    def fail_replace(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(gate.os, "replace", fail_replace)
    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["target_write_failed:OSError"]


def test_apply_error_findings_do_not_leak_paths(tmp_path: Path) -> None:
    # secret-safe 契約: エラー経路でも絶対パス (username 含む) を載せない。
    home = make_home(tmp_path)
    (home / "AGENTS.md").mkdir()
    manifest = write_manifest(tmp_path, [materialized_entry()])

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"][0].startswith("apply_failed:")
    assert str(home) not in json.dumps(report)


def test_apply_refuses_empty_existing_target(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    target = home / "AGENTS.md"
    target.write_text("", encoding="utf-8")
    manifest = write_manifest(tmp_path, [materialized_entry()])

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["existing_target_not_generated"]
    assert target.read_text(encoding="utf-8") == ""


def test_apply_refuses_source_as_target(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(
        tmp_path, [materialized_entry("{HOME}/AI-CONSTITUTION.md")]
    )

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["source_target_identical"]
    assert (home / "AI-CONSTITUTION.md").read_text(encoding="utf-8") == "source\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX のパーミッション意味論のみ対象")
def test_apply_preserves_existing_target_mode(tmp_path: Path) -> None:
    import stat as stat_module

    home = make_home(tmp_path, "# source\n")
    target = home / "AGENTS.md"
    target.write_text(gate.render_materialized("# old\n"), encoding="utf-8")
    os.chmod(target, 0o644)
    manifest = write_manifest(tmp_path, [materialized_entry()])

    report = gate.apply_entry(manifest, entry_id="grok", home=home)

    assert report["status"] == "pass"
    assert stat_module.S_IMODE(os.stat(target).st_mode) == 0o644


def test_manifest_required_must_be_boolean(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(
        tmp_path, [dict(materialized_entry(), required=0)]
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_required_invalid:grok"]


def test_manifest_rejects_non_string_path_and_source(tmp_path: Path) -> None:
    # 型違いは traceback でなく JSON レポート契約の中で拒否する。
    home = make_home(tmp_path)
    manifest = write_manifest(tmp_path, [dict(materialized_entry(), path=123)])
    report = gate.check_manifest(manifest, home=home)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_path_missing:grok"]

    bad_source = tmp_path / "bad_source.json"
    bad_source.write_text(
        json.dumps({"schema": gate.SCHEMA, "source": 123, "entries": [pointer_entry()]}),
        encoding="utf-8",
    )
    report = gate.check_manifest(bad_source, home=home)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_source_invalid"]


def test_manifest_rejects_missing_or_duplicate_ids(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    entry = materialized_entry()
    nameless = {key: value for key, value in entry.items() if key != "id"}
    report = gate.check_manifest(
        write_manifest(tmp_path, [nameless]), home=home
    )
    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_entry_id_invalid"]

    report = gate.check_manifest(
        write_manifest(tmp_path, [entry, dict(entry)]), home=home
    )
    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_entry_id_duplicate:grok"]


def test_manifest_rejects_unknown_entry_fields(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(
        tmp_path,
        [
            {
                "id": "cursor",
                "runtime": "cursor",
                "strategy": "manual",
                "evidnce": "typo field",
            }
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_entry_fields_unknown:cursor"]


def test_manifest_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    # schema は root も additionalProperties:false。コード側もそろえる。
    home = make_home(tmp_path)
    manifest = tmp_path / "extra.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": gate.SCHEMA,
                "source": "{HOME}/AI-CONSTITUTION.md",
                "entries": [pointer_entry()],
                "unexpected_extra_key": "x",
            }
        ),
        encoding="utf-8",
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_fields_unknown"]


def test_manual_entry_requires_evidence(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    manifest = write_manifest(
        tmp_path, [{"id": "cursor", "runtime": "cursor", "strategy": "manual"}]
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["manifest_evidence_missing:cursor"]


def test_manual_entry_stops_at_human_review_and_surfaces_evidence(
    tmp_path: Path,
) -> None:
    home = make_home(tmp_path)
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

    # manual の確認待ちだけなら drift (blocked) とは区別する。
    assert report["status"] == "human_review"
    assert report["entries"][0]["status"] == "human_review"
    assert report["entries"][0]["evidence"] == "Cursor Settings > Rules"


def test_manual_and_drift_together_report_blocked(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / "CLAUDE.md").write_text("# no import\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [
            pointer_entry(),
            {
                "id": "cursor",
                "runtime": "cursor",
                "strategy": "manual",
                "evidence": "Cursor Settings > Rules",
            },
        ],
    )

    report = gate.check_manifest(manifest, home=home)

    assert report["status"] == "blocked"


def test_exit_codes_distinguish_outcomes(tmp_path: Path, capsys) -> None:
    home = make_home(tmp_path)
    source = home / "AI-CONSTITUTION.md"

    (home / "CLAUDE.md").write_text(f"@{source}\n", encoding="utf-8")
    ok_manifest = write_manifest(tmp_path, [pointer_entry()])
    assert gate.main(["--manifest", str(ok_manifest), "--home", str(home)]) == 0

    (home / "CLAUDE.md").write_text("# no import\n", encoding="utf-8")
    assert gate.main(["--manifest", str(ok_manifest), "--home", str(home)]) == 1

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert gate.main(["--manifest", str(broken), "--home", str(home)]) == 2

    manual = write_manifest(
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
    assert gate.main(["--manifest", str(manual), "--home", str(home)]) == 3
    capsys.readouterr()


def test_entry_id_without_apply_is_an_error(tmp_path: Path, capsys) -> None:
    # 「1 件だけ確認した」誤読を生む黙殺をしない。
    home = make_home(tmp_path)
    manifest = write_manifest(tmp_path, [pointer_entry()])

    code = gate.main(
        ["--manifest", str(manifest), "--home", str(home), "--entry-id", "claude"]
    )

    out = capsys.readouterr().out
    assert code == 2
    assert "entry_id_requires_apply" in out
