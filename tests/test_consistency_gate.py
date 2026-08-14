from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "consistency_gate.py"
SPEC = importlib.util.spec_from_file_location("consistency_gate", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text(
        "# Demo\n\n[guide](docs/guide.md)\n\n`python src/app.py`\n",
        encoding="utf-8",
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "generated.txt").write_text("generated\n", encoding="utf-8")
    digest = hashlib.sha256((repo / "generated.txt").read_bytes()).hexdigest()
    config = {
        "schema": "repo-preflight.consistency/v1",
        "mode": "shadow",
        "markdown": {"include": ["README.md", "docs/**/*.md"]},
        "readme_contracts": {
            "required_paths": ["src/app.py"],
            "commands": [{"text": "python src/app.py", "paths": ["src/app.py"]}],
        },
        "impact_map": [
            {"change": ["src/**"], "requires_any": ["README.md", "docs/**"]}
        ],
        "generated_artifacts": [
            {"path": "generated.txt", "sources": ["src/**"], "sha256": digest}
        ],
    }
    (repo / ".repo-preflight-consistency.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    base = git(repo, "rev-parse", "HEAD")
    return repo, base


def test_passes_complete_contract(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    assert MODULE.check(repo, base_ref=base)["status"] == "pass"


def test_broken_markdown_link_is_reported(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "docs" / "guide.md").unlink()
    report = MODULE.check(repo, base_ref=base)
    assert "markdown_link_missing:README.md:docs/guide.md" in report["findings"]


def test_readme_declared_command_must_be_present_and_paths_exist(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "src" / "app.py").unlink()
    report = MODULE.check(repo, base_ref=base)
    assert "readme_command_missing:python src/app.py" in report["findings"]
    assert "readme_path_missing:src/app.py" in report["findings"]


def test_impact_map_requires_related_docs_change(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert report["impact"][0]["status"] == "fail"
    assert "related_docs_update_missing:src/**" in report["findings"]


def test_generated_hash_drift_is_reported(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "generated.txt").write_text("drift\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert "generated_artifact_drift:generated.txt" in report["findings"]


def test_changed_ssot_requires_generated_artifact_refresh(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert "generated_artifact_update_missing:generated.txt" in report["findings"]


def test_declared_path_cannot_escape_repository(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["readme_contracts"]["required_paths"] = ["../outside"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert MODULE.check(repo, base_ref=base)["status"] == "tool_error"


def test_shadow_and_enforce_have_same_findings_but_different_gate_status(
    tmp_path: Path,
):
    repo, base = make_repo(tmp_path)
    (repo / "docs" / "guide.md").unlink()
    shadow = MODULE.check(repo, base_ref=base)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mode"] = "enforce"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    enforce = MODULE.check(repo, base_ref=base)
    assert shadow["findings"] == enforce["findings"]
    assert shadow["status"] == "shadow_findings"
    assert enforce["status"] == "fail"


def test_invalid_config_fails_closed(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / ".repo-preflight-consistency.json").write_text("{}", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["invalid_consistency_config"]
