import importlib.util
import json
import subprocess
import sys
from datetime import date
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_SCRIPT = ROOT / "scripts" / "readiness_scan.py"
PREFS_SCRIPT = ROOT / "scripts" / "preferences.py"
DIALOGUE_SCRIPT = ROOT / "scripts" / "dialogue_gate.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCAN = load("readiness_scan", SCAN_SCRIPT)
PREFS = load("preferences", PREFS_SCRIPT)
DIALOGUE = load("dialogue_gate", DIALOGUE_SCRIPT)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test Author")
    git(repo, "config", "user.email", "test-author@example.invalid")
    for name in SCAN.REQUIRED:
        body = f"# {name}\n"
        if name == SCAN.REVIEW_RECORD:
            body += f"{SCAN.REVIEW_RECORD_MARKER}\n"
        (repo / name).write_text(body, encoding="utf-8")
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", "https://github.com/example/repo.git")
    return repo


def test_recommended_proposal_gets_dismiss_options():
    proposal = {
        "id": "configure_expected_identity",
        "kind": "optional_policy",
        "severity": "recommended",
        "options": [{"id": "yes", "label": "y"}, {"id": "no", "label": "n"}],
    }
    enriched = PREFS.apply_dismissal_options([proposal])[0]
    assert enriched["dismissible"] is True
    ids = {opt["id"] for opt in enriched["options"]}
    assert "dismiss_forever" in ids
    assert "dismiss_30d" in ids


def test_security_hold_is_never_dismissible():
    proposal = {
        "id": "resolve_secret_findings",
        "kind": "security_hold",
        "severity": "required",
        "options": [{"id": "fix", "label": "fix"}, {"id": "stop", "label": "stop"}],
    }
    assert PREFS.max_dismissal_mode(proposal) is None
    enriched = PREFS.apply_dismissal_options([proposal])[0]
    assert enriched["dismissible"] is False
    assert not any(
        str(opt.get("id", "")).startswith("dismiss_") for opt in enriched["options"]
    )


def test_active_dismissal_filters_proposal(tmp_path: Path):
    repo = make_repo(tmp_path)
    prefs = PREFS.empty_preferences()
    prefs = PREFS.record_dismissal(
        prefs, "configure_expected_identity", mode="forever", reason="skip"
    )
    PREFS.save_preferences(repo, prefs)

    scan = SCAN.scan(repo)
    loaded = PREFS.load_preferences(repo)
    dialogue = DIALOGUE.build_dialogue(
        intent="open_pr",
        scan=scan,
        audience="local",
        preferences=loaded,
        preferences_module=PREFS,
        github_baseline={"status": "fresh", "last_reviewed": "2026-08-06"},
    )
    ids = {item["id"] for item in dialogue["proposals"]}
    assert "configure_expected_identity" not in ids
    suppressed_ids = {item["id"] for item in dialogue["suppressed_proposals"]}
    assert "configure_expected_identity" in suppressed_ids


def test_secret_dismissal_record_does_not_suppress_hold(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "leak.txt").write_text("github_pat_" + "B" * 30, encoding="utf-8")
    prefs = PREFS.record_dismissal(
        PREFS.empty_preferences(),
        "resolve_secret_findings",
        mode="forever",
    )
    scan = SCAN.scan(repo)
    dialogue = DIALOGUE.build_dialogue(
        intent="publish",
        scan=scan,
        audience="public",
        preferences=prefs,
        preferences_module=PREFS,
        github_baseline={"status": "fresh"},
    )
    ids = {item["id"] for item in dialogue["proposals"]}
    assert "resolve_secret_findings" in ids
    assert dialogue["status"] == "blocked"


def test_github_baseline_stale_adds_proposal():
    baseline = {
        "status": "stale",
        "last_reviewed": "2025-01-01",
        "max_age_days": 90,
        "age_days": 400,
    }
    dialogue = DIALOGUE.build_dialogue(
        intent="publish",
        scan={
            "status": "pass",
            "checks": {},
            "repo": "x",
            "head": "abc",
            "publication_decision": "blocked_human_review_required",
        },
        audience="public",
        preferences=PREFS.empty_preferences(),
        preferences_module=PREFS,
        github_baseline=baseline,
    )
    ids = {item["id"] for item in dialogue["proposals"]}
    assert "refresh_github_settings_baseline" in ids


def test_github_baseline_parser_and_freshness():
    text = (
        "# guide\n"
        "<!-- repo-preflight:github-baseline last_reviewed: 2026-01-01 max_age_days: 90 -->\n"
    )
    parsed = PREFS.parse_github_baseline(text)
    assert parsed == {"last_reviewed": "2026-01-01", "max_age_days": 90}
    path = ROOT / "references" / "github-settings.md"
    status = PREFS.github_baseline_status(path, today=date(2026, 8, 6))
    assert status["status"] == "fresh"
    assert status["last_reviewed"] == "2026-08-06"
    stale = PREFS.github_baseline_status(path, today=date(2026, 12, 1))
    assert stale["status"] == "stale"


def test_cli_record_dismissal(tmp_path: Path):
    repo = make_repo(tmp_path)
    console = StringIO()
    stdout = StringIO()
    code = SCAN.main(
        [
            "--repo",
            str(repo),
            "--record-dismissal",
            "configure_expected_identity",
            "--dismissal-mode",
            "30d",
            "--dismissal-reason",
            "later",
        ],
        stdin_is_tty=False,
        console=console,
        stdout=stdout,
    )
    assert code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "recorded"
    saved = json.loads((repo / ".repo-preflight.json").read_text(encoding="utf-8"))
    assert saved["dismissals"]["configure_expected_identity"]["mode"] == "snooze"
    assert "until" in saved["dismissals"]["configure_expected_identity"]


def test_guide_contains_baseline_marker():
    text = (ROOT / "references" / "github-settings.md").read_text(encoding="utf-8")
    assert "repo-preflight:github-baseline" in text
    assert "last_reviewed:" in text
