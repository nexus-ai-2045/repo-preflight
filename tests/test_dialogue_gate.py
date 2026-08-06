import importlib.util
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_SCRIPT = ROOT / "scripts" / "readiness_scan.py"
DIALOGUE_SCRIPT = ROOT / "scripts" / "dialogue_gate.py"

SCAN_SPEC = importlib.util.spec_from_file_location("readiness_scan", SCAN_SCRIPT)
SCAN = importlib.util.module_from_spec(SCAN_SPEC)
assert SCAN_SPEC.loader
sys.modules[SCAN_SPEC.name] = SCAN
SCAN_SPEC.loader.exec_module(SCAN)

DIALOGUE_SPEC = importlib.util.spec_from_file_location("dialogue_gate", DIALOGUE_SCRIPT)
DIALOGUE = importlib.util.module_from_spec(DIALOGUE_SPEC)
assert DIALOGUE_SPEC.loader
sys.modules[DIALOGUE_SPEC.name] = DIALOGUE
DIALOGUE_SPEC.loader.exec_module(DIALOGUE)


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


def test_create_repo_dialogue_asks_private_default_without_scan():
    dialogue = DIALOGUE.build_dialogue(
        intent="create_repo",
        scan=None,
        guarantees=["g"],
        non_guarantees=["n"],
    )
    assert dialogue["schema"] == DIALOGUE.DIALOGUE_SCHEMA
    assert dialogue["status"] == "needs_human_input"
    ids = {item["id"] for item in dialogue["proposals"]}
    assert "confirm_visibility_private_default" in ids
    assert "confirm_repo_identity" in ids
    assert dialogue["publication_decision"] == "blocked_human_review_required"
    assert "エージェント" in "\n".join(dialogue["agent_instructions"]) or any(
        "ユーザー" in line for line in dialogue["agent_instructions"]
    )


def test_open_pr_proposes_missing_documents(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "SECURITY.md").unlink()
    scan = SCAN.scan(repo)
    dialogue = DIALOGUE.build_dialogue(intent="open_pr", scan=scan, audience="local")
    ids = {item["id"] for item in dialogue["proposals"]}
    assert "create_missing_security_md" in ids
    assert dialogue["status"] == "needs_human_input"
    question = next(
        item["question"]
        for item in dialogue["proposals"]
        if item["id"] == "create_missing_security_md"
    )
    assert "SECURITY.md" in question
    assert "作成しますか" in question


def test_secret_findings_block_without_ignore_option(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / "leak.txt").write_text("github_pat_" + "A" * 30, encoding="utf-8")
    scan = SCAN.scan(repo)
    dialogue = DIALOGUE.build_dialogue(intent="publish", scan=scan, audience="public")
    secret = next(
        item
        for item in dialogue["proposals"]
        if item["id"] == "resolve_secret_findings"
    )
    option_ids = {opt["id"] for opt in secret["options"]}
    assert "ignore" not in option_ids
    assert dialogue["status"] == "blocked"


def test_publish_without_audience_asks_choice(tmp_path: Path):
    repo = make_repo(tmp_path)
    scan = SCAN.scan(repo, release=True)
    dialogue = DIALOGUE.build_dialogue(
        intent="publish", scan=scan, audience="unspecified"
    )
    ids = {item["id"] for item in dialogue["proposals"]}
    assert "choose_audience" in ids


def test_cli_intent_create_repo_without_repo_path():
    console = StringIO()
    stdout = StringIO()
    code = SCAN.main(
        ["--intent", "create_repo", "--human"],
        stdin_is_tty=False,
        console=console,
        stdout=stdout,
    )
    dialogue = json.loads(stdout.getvalue())
    assert code == 1
    assert dialogue["intent"] == "create_repo"
    assert dialogue["schema"] == DIALOGUE.DIALOGUE_SCHEMA
    assert "保証すること" in console.getvalue()
    assert "private" in console.getvalue().lower() or any(
        item["id"] == "confirm_visibility_private_default"
        for item in dialogue["proposals"]
    )


def test_cli_intent_open_pr_includes_scan(tmp_path: Path):
    repo = make_repo(tmp_path)
    console = StringIO()
    stdout = StringIO()
    code = SCAN.main(
        ["--repo", str(repo), "--intent", "open_pr"],
        stdin_is_tty=False,
        console=console,
        stdout=stdout,
    )
    dialogue = json.loads(stdout.getvalue())
    assert dialogue["intent"] == "open_pr"
    assert dialogue["scan"] is not None
    assert dialogue["scan"]["status"] in {"pass", "blocked"}
    assert dialogue["guarantees"]
    assert dialogue["non_guarantees"]
    assert code in {0, 1}


def test_format_dialogue_lists_numbered_proposals():
    dialogue = DIALOGUE.build_dialogue(intent="create_repo", scan=None)
    text = DIALOGUE.format_dialogue_for_agent(dialogue)
    assert "1." in text
    assert "confirmations" not in text.lower() or "最終確認" in text
    assert "保証すること" in text
    assert "保証しないこと" in text
