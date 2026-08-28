import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scripts" / "runtime_smoke.py"
INSTALL = ROOT / "scripts" / "install_runtime_skills.py"
RUN = ROOT / "runtime" / "shared" / "run_preflight.py"


def test_runtime_adapters_exist_and_name_repo_preflight():
    for rel in (
        "SKILL.md",
        "runtime/claude-code/SKILL.md",
        "runtime/grok/SKILL.md",
        "runtime/agents/openai.yaml",
        "runtime/shared/run_preflight.py",
        "docs/runtime-support.md",
        "scripts/ai_entry_contract.py",
        "schemas/ai-entry-contract.schema.json",
        "assets/ai-entry-contract.example.json",
        "docs/ai-constitution-entry-contract.md",
    ):
        path = ROOT / rel
        assert path.is_file(), rel
        text = path.read_text(encoding="utf-8")
        assert "repo-preflight" in text or "readiness_scan" in text


def test_claude_and_grok_adapters_are_portable():
    for rel in ("runtime/claude-code/SKILL.md", "runtime/grok/SKILL.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "--intent" in text
        assert "open_pr" in text
        assert "run_preflight.py" in text
        assert "REPO_PREFLIGHT_ROOT=" not in text or "REPO_PREFLIGHT_ROOT=\n" in text
        # 絶対 path を skill に焼かない。検査文字列は連結で保持する
        # (連続 literal だと自分自身の personal path scan に検出される)
        assert "C:\\Us" + "ers\\" not in text
        assert "/Us" + "ers/" not in text or "Use before" in text  # English prose ok
        assert "guarantees" in text.lower() or "保証" in text or "MUST" in text


def test_runtime_adapters_expose_configure_settings():
    # root SKILL.md が configure_settings を intent 対話として保証する以上、
    # 各 runtime adapter も description / intent 列挙 / trigger 語で同じ intent を
    # 明示しないと乖離する (adapter だけ古いままだと agent が intent を発火しない)。
    for rel in (
        "runtime/claude-code/SKILL.md",
        "runtime/grok/SKILL.md",
        "runtime/agents/openai.yaml",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "configure_settings" in text, rel


def test_github_adoption_documents_intent_dialogue_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    runtime = (ROOT / "docs/runtime-support.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for text, label in (
        (readme, "README.md"),
        (skill, "SKILL.md"),
        (runtime, "docs/runtime-support.md"),
        (changelog, "CHANGELOG.md"),
    ):
        assert "dialogue" in text.lower() or "対話" in text, label
        assert "--intent" in text, label
    for intent in ("create_repo", "push", "open_pr", "merge"):
        assert intent in skill, intent
        assert intent in readme, intent
    assert "repo-preflight.dialogue/v3" in readme
    assert "GitHub を採用" in readme
    assert "github.com のページが対話 UI" in runtime or "ページが対話 UI" in runtime


def test_run_preflight_discovers_root_from_clone(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(RUN), "--intent", "create_repo"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    assert payload["schema"] == "repo-preflight.dialogue/v3"
    assert payload["intent"] == "create_repo"


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


def test_install_runtime_skills_portable_layout(tmp_path: Path):
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
    assert dry_payload.get("portable") is True

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
    assert applied.returncode == 0, applied.stdout + applied.stderr
    skill_dir = home / ".claude" / "skills" / "repo-preflight"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "run_preflight.py").is_file()
    assert (skill_dir / "checkout").exists()
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert str(ROOT.resolve()) not in body
    assert "run_preflight.py" in body

    # install 先の launcher から create_repo が動く
    launched = subprocess.run(
        [
            sys.executable,
            str(skill_dir / "run_preflight.py"),
            "--intent",
            "create_repo",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    report = json.loads(launched.stdout)
    assert report["schema"] == "repo-preflight.dialogue/v3"
    assert report["intent"] == "create_repo"


def _install(home: Path) -> None:
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
    assert applied.returncode == 0, applied.stdout + applied.stderr


def _check(home: Path) -> tuple[int, dict]:
    result = subprocess.run(
        [
            sys.executable,
            str(INSTALL),
            "--repo",
            str(ROOT),
            "--home",
            str(home),
            "--check",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, json.loads(result.stdout)


def _fresh_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills").mkdir(parents=True)
    return home


def test_check_reports_not_installed_without_copies(tmp_path: Path):
    code, payload = _check(_fresh_home(tmp_path))
    assert code == 0
    assert payload["schema"] == "repo-preflight.check-runtime-skills/v1"
    assert payload["status"] == "pass"
    assert payload["read_only"] is True
    assert {item["status"] for item in payload["results"]} == {"not_installed"}


def test_check_passes_right_after_install(tmp_path: Path):
    """射影 (REPO_PREFLIGHT_ROOT= 行の除去) を通さないと必ず誤検知する回帰."""
    home = _fresh_home(tmp_path)
    _install(home)
    code, payload = _check(home)
    assert code == 0, payload
    assert payload["status"] == "pass"
    for item in payload["results"]:
        assert item["status"] == "ok", item
        assert item["findings"] == []


def test_check_detects_skill_md_drift(tmp_path: Path):
    home = _fresh_home(tmp_path)
    _install(home)
    skill = home / ".claude" / "skills" / "repo-preflight" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")

    code, payload = _check(home)
    assert code == 1
    assert payload["status"] == "drift"
    drifted = [i for i in payload["results"] if i["status"] == "drift"]
    assert len(drifted) == 1
    assert drifted[0]["runtime"] == "claude-code"
    assert drifted[0]["findings"] == ["skill_md_drift"]


def test_check_detects_run_preflight_drift(tmp_path: Path):
    home = _fresh_home(tmp_path)
    _install(home)
    run = home / ".agents" / "skills" / "repo-preflight" / "run_preflight.py"
    run.write_text(run.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    code, payload = _check(home)
    assert code == 1
    drifted = [i for i in payload["results"] if i["status"] == "drift"]
    assert [i["runtime"] for i in drifted] == ["agents"]
    assert drifted[0]["findings"] == ["run_preflight_drift"]


def test_check_detects_missing_and_dangling_checkout(tmp_path: Path):
    home = _fresh_home(tmp_path)
    _install(home)
    dest = home / ".claude" / "skills" / "repo-preflight"
    checkout = dest / "checkout"
    if checkout.is_symlink() or checkout.exists():
        if checkout.is_symlink():
            checkout.unlink()
        elif checkout.is_dir():
            shutil.rmtree(checkout)
    code, payload = _check(home)
    assert code == 1
    entry = next(i for i in payload["results"] if i["runtime"] == "claude-code")
    assert entry["link"] == "missing"
    assert "checkout_missing" in entry["findings"]


def test_check_detects_readme_drift(tmp_path: Path):
    home = _fresh_home(tmp_path)
    _install(home)
    readme = home / ".claude" / "skills" / "repo-preflight" / "README.md"
    readme.write_text("# tampered\n", encoding="utf-8")

    code, payload = _check(home)
    assert code == 1
    entry = next(i for i in payload["results"] if i["runtime"] == "claude-code")
    assert entry["findings"] == ["readme_drift"]


def test_check_is_read_only(tmp_path: Path):
    home = _fresh_home(tmp_path)
    _install(home)
    before = {
        p: p.read_bytes()
        for p in sorted(home.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }
    _check(home)
    after = {
        p: p.read_bytes()
        for p in sorted(home.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }
    assert before == after


def test_check_and_apply_are_mutually_exclusive(tmp_path: Path):
    result = subprocess.run(
        [
            sys.executable,
            str(INSTALL),
            "--repo",
            str(ROOT),
            "--home",
            str(_fresh_home(tmp_path)),
            "--check",
            "--apply",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 2
    assert "mutually exclusive" in result.stderr
