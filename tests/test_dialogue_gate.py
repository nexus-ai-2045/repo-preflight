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


def test_configure_settings_emits_one_proposal_per_setting_and_keeps_mutation_separate():
    review = {
        "schema_version": "repo-preflight.github-settings-review/v1",
        "status": "needs_human_input",
        "repository": "example/repo",
        "profile": "solo_public",
        "external_actions_performed": False,
        "settings": [
            {
                "name": "default_workflow_permissions",
                "tier": "required",
                "observed_value": "write",
                "recommended_value": "read",
                "classification": "human_decision",
                "reason": "least privilege",
                "external_effect": "workflow token becomes read-only",
                "proposed_operation": {
                    "method": "PUT",
                    "endpoint": "repos/example/repo/actions/permissions/workflow",
                    "body": {"default_workflow_permissions": "read"},
                },
                "rollback": {
                    "method": "PUT",
                    "endpoint": "repos/example/repo/actions/permissions/workflow",
                    "body": {"default_workflow_permissions": "write"},
                },
                "approved": False,
                "blocks_intent": True,
            },
            {
                "name": "delete_branch_on_merge",
                "tier": "recommended",
                "observed_value": False,
                "recommended_value": True,
                "classification": "recommended_change",
                "reason": "branch hygiene",
                "external_effect": "merged branches are deleted",
                "proposed_operation": {
                    "method": "PATCH",
                    "endpoint": "repos/example/repo",
                    "body": {"delete_branch_on_merge": True},
                },
                "rollback": {
                    "method": "PATCH",
                    "endpoint": "repos/example/repo",
                    "body": {"delete_branch_on_merge": False},
                },
                "approved": False,
                "blocks_intent": False,
            },
        ],
    }

    dialogue = DIALOGUE.build_dialogue(
        intent="configure_settings",
        scan={"status": "pass", "repo": "/safe/repo", "checks": {}},
        github_settings_review=review,
    )

    assert dialogue["github_settings_review"] is review
    proposals = {
        item["id"]: item
        for item in dialogue["proposals"]
        if item["kind"] == "github_setting_change"
    }
    required = proposals["github_setting_default_workflow_permissions"]
    recommended = proposals["github_setting_delete_branch_on_merge"]
    assert required["blocks_intent"] is True
    assert recommended["blocks_intent"] is False
    assert required["proposed"]["approved"] is False
    assert required["proposed"]["operation"]["method"] == "PUT"
    assert required["proposed"]["rollback"]
    assert review["external_actions_performed"] is False
    assert dialogue["confirmations"][0]["proposed"]["action"] == "configure_settings"
    assert "別承認" in dialogue["confirmations"][0]["proposed"]["reminder"]
    assert dialogue["status"] == "needs_human_input"


def test_configure_settings_unavailable_required_observation_blocks_without_operation():
    review = {
        "status": "needs_human_input",
        "repository": "example/repo",
        "profile": "solo_public",
        "external_actions_performed": False,
        "settings": [
            {
                "name": "default_branch_ruleset",
                "tier": "required",
                "observed_value": "unknown",
                "recommended_value": "active",
                "classification": "unavailable",
                "reason": "API unavailable",
                "external_effect": "branch protection",
                "proposed_operation": None,
                "rollback": None,
                "approved": False,
                "blocks_intent": True,
            }
        ],
    }

    dialogue = DIALOGUE.build_dialogue(
        intent="configure_settings",
        scan={"status": "pass", "checks": {}},
        github_settings_review=review,
    )

    proposal = next(
        item
        for item in dialogue["proposals"]
        if item["kind"] == "github_setting_change"
    )
    assert proposal["proposed"]["operation"] is None
    assert proposal["blocks_intent"] is True
    assert dialogue["status"] == "needs_human_input"


def test_cli_open_pr_target_diff_uses_explicit_base(tmp_path: Path):
    repo = make_repo(tmp_path)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo,
        check=True,
    )
    base = "origin/main"
    (repo / "target.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "target.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "target"], cwd=repo, check=True)
    console = StringIO()
    stdout = StringIO()

    code = SCAN.main(
        [
            "--repo",
            str(repo),
            "--intent",
            "open_pr",
            "--base-ref",
            base,
        ],
        stdin_is_tty=False,
        console=console,
        stdout=stdout,
    )

    dialogue = json.loads(stdout.getvalue())
    assert dialogue["scan"]["status"] == "pass"
    assert dialogue["scan"]["scan_scope"]["mode"] == "target_diff"
    assert dialogue["scan"]["options"]["base_ref"] == base
    confirmation = dialogue["confirmations"][0]
    assert f"base={base}@" in confirmation["question"]
    binding = confirmation["proposed"]["operation_binding"]
    assert binding["base_ref"] == base
    assert binding["base_oid"] == dialogue["scan"]["scan_scope"]["base_oid"]
    assert binding["head_oid"] == dialogue["scan"]["head"]
    assert binding["rerun_if_base_or_head_changes"] is True
    assert code == 0


def test_format_dialogue_lists_numbered_proposals():
    dialogue = DIALOGUE.build_dialogue(intent="create_repo", scan=None)
    text = DIALOGUE.format_dialogue_for_agent(dialogue)
    assert "1." in text
    assert "confirmations" not in text.lower() or "最終確認" in text
    assert "保証すること" in text
    assert "保証しないこと" in text


def test_configure_settings_blocks_when_repository_identity_yields_no_settings():
    dialogue = DIALOGUE.build_dialogue(
        intent="configure_settings",
        github_settings_review={
            "status": "needs_human_input",
            "repository": None,
            "profile": "solo_public",
            "settings": [],
            "unknowns": [{"reason": "github_origin_owner_name_unavailable"}],
        },
    )

    proposal = next(
        item
        for item in dialogue["proposals"]
        if item["id"] == "inspect_github_settings"
    )
    assert proposal["blocks_intent"] is True
    assert dialogue["status"] == "needs_human_input"


def test_configure_settings_surfaces_authenticated_account_confirmation():
    dialogue = DIALOGUE.build_dialogue(
        intent="configure_settings",
        github_settings_review={
            "status": "needs_human_input",
            "repository": "example/repo",
            "profile": "solo_public",
            "settings": [
                {
                    "name": "authenticated_account",
                    "tier": "required",
                    "observed_value": "collaborator",
                    "recommended_value": "example",
                    "classification": "human_decision",
                    "reason": "acting accountを固定する",
                    "external_effect": "wrong account prevention",
                    "proposed_operation": None,
                    "rollback": None,
                    "blocks_intent": True,
                }
            ],
        },
    )

    proposal = next(
        item
        for item in dialogue["proposals"]
        if item["id"] == "github_setting_authenticated_account"
    )
    assert proposal["kind"] == "github_account_confirmation"
    assert proposal["blocks_intent"] is True
    assert "collaborator" in proposal["question"]


def test_configure_settings_surfaces_stale_github_guidance():
    dialogue = DIALOGUE.build_dialogue(
        intent="configure_settings",
        github_settings_review={
            "status": "pass",
            "repository": "example/repo",
            "profile": "solo_public",
            "settings": [],
        },
        github_baseline={
            "status": "stale",
            "last_reviewed": "2025-01-01",
            "age_days": 600,
            "max_age_days": 90,
        },
    )

    ids = {item["id"] for item in dialogue["proposals"]}
    assert "refresh_github_settings_baseline" in ids
