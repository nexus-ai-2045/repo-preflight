import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scripts" / "runtime_smoke.py"
INSTALL = ROOT / "scripts" / "install_runtime_skills.py"


def test_runtime_adapters_exist_and_name_repo_preflight():
    for rel in (
        "SKILL.md",
        "runtime/claude-code/SKILL.md",
        "runtime/grok/SKILL.md",
        "runtime/agents/openai.yaml",
        "docs/runtime-support.md",
    ):
        path = ROOT / rel
        assert path.is_file(), rel
        text = path.read_text(encoding="utf-8")
        assert "repo-preflight" in text


def test_claude_and_grok_adapters_mention_intent_triggers():
    for rel in ("runtime/claude-code/SKILL.md", "runtime/grok/SKILL.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "--intent" in text
        assert "open_pr" in text
        assert "guarantees" in text.lower() or "保証" in text or "MUST" in text


def test_runtime_smoke_passes():
    result = subprocess.run(
        [sys.executable, str(SMOKE), "--repo", str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["status"] == "pass"
    assert "cli" in payload["supported_runtimes"]
    assert "claude-code" in payload["supported_runtimes"]
    assert "grok" in payload["supported_runtimes"]


def test_install_runtime_skills_dry_run_and_apply(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".grok").mkdir()

    dry = subprocess.run(
        [
            sys.executable,
            str(INSTALL),
            "--repo",
            str(ROOT),
            "--home",
            str(home),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    dry_payload = json.loads(dry.stdout)
    assert dry.returncode == 0
    assert dry_payload["apply"] is False
    assert all(item["status"] == "would_write" for item in dry_payload["results"])

    applied = subprocess.run(
        [
            sys.executable,
            str(INSTALL),
            "--repo",
            str(ROOT),
            "--home",
            str(home),
            "--apply",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert applied.returncode == 0
    claude_skill = home / ".claude" / "skills" / "repo-preflight" / "SKILL.md"
    agents_skill = home / ".agents" / "skills" / "repo-preflight" / "SKILL.md"
    assert claude_skill.is_file()
    assert agents_skill.is_file()
    body = claude_skill.read_text(encoding="utf-8")
    assert (
        f"REPO_PREFLIGHT_ROOT={ROOT.resolve()}" in body.replace("\\", "/")
        or str(ROOT.resolve()) in body
    )
    assert "readiness_scan.py" in body
