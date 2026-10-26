"""readme_diagram: 印の間だけ mermaid を埋め、identity 検査と冪等を固定する。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from synthetic_paths import synthetic_home_path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readme_diagram.py"
SPEC = importlib.util.spec_from_file_location("readme_diagram", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)

MARKED = (
    "# サンプル\n\n"
    "要約です。\n\n"
    "## 目的\n\n目的です。\n\n"
    f"{MODULE.START_MARKER}\n"
    "古い図\n"
    f"{MODULE.END_MARKER}\n\n"
    "## 制約\n\n制約です。\n"
)

UNMARKED = "# サンプル\n\n要約です。\n\n## 目的\n\n目的です。\n"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "README.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_unmarked_readme_is_left_unchanged(tmp_path: Path):
    path = _write(tmp_path, UNMARKED)
    before = path.read_text(encoding="utf-8")
    report = MODULE.sync_readme_diagram(path, apply=True)
    assert report["status"] == "pass"
    assert report["reason"] == "markers_absent"
    assert report["changed"] is False
    assert report["applied"] is False
    assert path.read_text(encoding="utf-8") == before


def test_markers_receive_canonical_mermaid(tmp_path: Path):
    path = _write(tmp_path, MARKED)
    report = MODULE.sync_readme_diagram(path, apply=True)
    assert report["status"] == "pass"
    assert report["applied"] is True
    text = path.read_text(encoding="utf-8")
    assert MODULE.START_MARKER in text
    assert MODULE.END_MARKER in text
    assert "```mermaid" in text
    assert "flowchart TD" in text
    assert "版と文書をそろえる" in text
    assert "古い図" not in text
    # 印の外は残す
    assert "## 目的" in text
    assert "## 制約" in text


def test_apply_is_idempotent(tmp_path: Path):
    path = _write(tmp_path, MARKED)
    first = MODULE.sync_readme_diagram(path, apply=True)
    after_first = path.read_text(encoding="utf-8")
    second = MODULE.sync_readme_diagram(path, apply=True)
    after_second = path.read_text(encoding="utf-8")
    assert first["status"] == "pass"
    assert second["status"] == "pass"
    assert second["changed"] is False
    assert second["applied"] is False
    assert after_first == after_second


def test_public_and_private_visibility_behave_the_same(tmp_path: Path):
    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    public_readme = _write(tmp_path / "public", MARKED)
    private_readme = _write(tmp_path / "private", MARKED)

    public_plan = MODULE.plan_readme_diagram(
        public_readme.read_text(encoding="utf-8"), visibility="public"
    )
    private_plan = MODULE.plan_readme_diagram(
        private_readme.read_text(encoding="utf-8"), visibility="private"
    )
    assert public_plan["status"] == private_plan["status"] == "pass"
    assert public_plan["rendered"] == private_plan["rendered"]
    assert public_plan["changed"] == private_plan["changed"]

    MODULE.sync_readme_diagram(public_readme, visibility="public", apply=True)
    MODULE.sync_readme_diagram(private_readme, visibility="private", apply=True)
    assert public_readme.read_text(encoding="utf-8") == private_readme.read_text(
        encoding="utf-8"
    )


def test_diagram_home_path_is_blocked_by_identity_gate(monkeypatch):
    poisoned = (
        "```mermaid\n"
        "flowchart TD\n"
        "    A[" + synthetic_home_path("linux", "someone/project") + "] --> B[検査]\n"
        "```\n"
    )
    monkeypatch.setattr(MODULE, "CANONICAL_DIAGRAM", poisoned)
    plan = MODULE.plan_readme_diagram(MARKED, visibility="public")
    assert plan["status"] == "blocked"
    assert plan["reason"] == "diagram_identity_blocked"
    assert "実ホームパスの形" in plan["findings"]
    assert plan["changed"] is False


def test_diagram_personal_email_is_blocked_by_identity_gate(monkeypatch):
    poisoned = (
        "```mermaid\n"
        "flowchart TD\n"
        "    A[連絡先 "
        + "".join(["user", chr(64), "gmail", ".com"])
        + "] --> B[検査]\n"
        "```\n"
    )
    monkeypatch.setattr(MODULE, "CANONICAL_DIAGRAM", poisoned)
    plan = MODULE.plan_readme_diagram(MARKED, visibility="private")
    assert plan["status"] == "blocked"
    assert plan["reason"] == "diagram_identity_blocked"
    assert "個人メールの形" in plan["findings"]


def test_ambiguous_markers_do_not_rewrite(tmp_path: Path):
    text = (
        f"{MODULE.START_MARKER}\n"
        "one\n"
        f"{MODULE.END_MARKER}\n"
        f"{MODULE.START_MARKER}\n"
        "two\n"
        f"{MODULE.END_MARKER}\n"
    )
    path = _write(tmp_path, text)
    report = MODULE.sync_readme_diagram(path, apply=True)
    assert report["status"] == "blocked"
    assert report["reason"] == "markers_ambiguous"
    assert path.read_text(encoding="utf-8") == text


def test_canonical_diagram_passes_identity_gate():
    findings = MODULE.check_diagram_identity(MODULE.diagram_body())
    assert findings == []


def test_release_readme_checks_remain_required():
    """既存の release README 検査入口が外れていないこと（弱体化防止）。"""
    scan_path = Path(__file__).resolve().parents[1] / "scripts" / "readiness_scan.py"
    source = scan_path.read_text(encoding="utf-8")
    assert "check_release_readme_freshness" in source
    assert "run_readme_release_gate" in source
    assert "readme_release_freshness" in source
    assert "readme_release_design" in source
    # 図更新は別入口。release 検査を図更新の成功で省略する分岐を置かない
    assert "readme_diagram" not in source or "skip_readme_release" not in source


def test_cli_reports_pass_for_unmarked(tmp_path: Path, capsys):
    _write(tmp_path, UNMARKED)
    code = MODULE.main(["--repo", str(tmp_path), "--json"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"status": "pass"' in out
    assert "markers_absent" in out


def test_inline_marker_mentions_are_not_markers(tmp_path: Path):
    text = (
        f"# サンプル\n\n印 `{MODULE.START_MARKER}` と `{MODULE.END_MARKER}` の説明。\n\n"
        f"{MODULE.START_MARKER}\n古い図\n{MODULE.END_MARKER}\n"
    )
    plan = MODULE.plan_readme_diagram(text)
    assert plan["status"] == "pass"
    assert plan["changed"] is True
    assert plan["rendered"].startswith(text.split(MODULE.START_MARKER + "\n")[0])


def test_outside_of_markers_is_byte_identical(tmp_path: Path):
    head = "# サンプル\r\n\r\n要約です。  \r\n\r\n"
    tail = "\r\n## 制約\r\n\r\n制約です。"
    text = f"{head}{MODULE.START_MARKER}\r\n古い図\r\n{MODULE.END_MARKER}{tail}"
    path = tmp_path / "README.md"
    path.write_bytes(text.encode("utf-8"))
    report = MODULE.sync_readme_diagram(path, apply=True)
    assert report["status"] == "pass"
    after = path.read_bytes().decode("utf-8")
    assert after.startswith(head + MODULE.START_MARKER + "\r\n")
    assert after.endswith(MODULE.END_MARKER + tail)
    assert "\n" not in after.replace("\r\n", "")
    second = MODULE.sync_readme_diagram(path, apply=True)
    assert second["changed"] is False


def test_end_before_start_is_ambiguous():
    text = f"{MODULE.END_MARKER}\n図\n{MODULE.START_MARKER}\n"
    plan = MODULE.plan_readme_diagram(text)
    assert plan["status"] == "blocked"
    assert plan["reason"] == "markers_ambiguous"


def test_shipped_example_is_already_current():
    example = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "examples"
        / "README-with-diagram.md"
    )
    with example.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    plan = MODULE.plan_readme_diagram(text, visibility="public")
    assert plan["status"] == "pass"
    assert plan["reason"] == "already_current"
    assert plan["changed"] is False


def test_markers_inside_backtick_fence_are_not_markers():
    text = (
        "# サンプル\n\n書き方の例:\n\n"
        "```markdown\n"
        f"{MODULE.START_MARKER}\n"
        "ここに図\n"
        f"{MODULE.END_MARKER}\n"
        "```\n"
    )
    plan = MODULE.plan_readme_diagram(text)
    assert plan["status"] == "pass"
    assert plan["reason"] == "markers_absent"
    assert plan["changed"] is False


def test_markers_inside_tilde_fence_are_ignored_next_to_real_markers():
    example = (
        "~~~~\n" f"{MODULE.START_MARKER}\n" "```\n" f"{MODULE.END_MARKER}\n" "~~~~\n"
    )
    text = (
        f"# サンプル\n\n{example}\n"
        f"{MODULE.START_MARKER}\n古い図\n{MODULE.END_MARKER}\n"
    )
    plan = MODULE.plan_readme_diagram(text)
    assert plan["status"] == "pass"
    assert plan["changed"] is True
    rendered = plan["rendered"]
    assert rendered.startswith(f"# サンプル\n\n{example}\n")
    assert "古い図" not in rendered
    assert "flowchart TD" in rendered.split(example, 1)[1]


def test_unclosed_fence_hides_following_markers():
    text = "```\n" f"{MODULE.START_MARKER}\n" "図\n" f"{MODULE.END_MARKER}\n"
    plan = MODULE.plan_readme_diagram(text)
    assert plan["reason"] == "markers_absent"
    assert plan["changed"] is False
