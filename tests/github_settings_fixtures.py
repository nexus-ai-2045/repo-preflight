"""github_settings_gate のテストで共有する fixture と fake API。"""

import base64
import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "github_settings_gate.py"
SPEC = importlib.util.spec_from_file_location("github_settings_gate", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

REPO = "example/repo"
PR_HEAD_SHA = "a" * 40
PINNED_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"

CLASSIC_ENDPOINT = f"repos/{REPO}/branches/main/protection"
PULLS_ENDPOINT = (
    f"repos/{REPO}/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=30"
)
WORKFLOWS_ENDPOINT = f"repos/{REPO}/contents/.github/workflows?ref=main"
RULESETS_ENDPOINT = f"repos/{REPO}/rulesets?per_page=100"
ACTIONS_ENDPOINT = f"repos/{REPO}/actions/permissions"
WORKFLOW_PERMISSIONS_ENDPOINT = f"repos/{REPO}/actions/permissions/workflow"
SELECTED_ENDPOINT = f"repos/{REPO}/actions/permissions/selected-actions"

CI_WORKFLOW = f"""name: ci
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{PINNED_SHA}
      - uses: actions/setup-python@{PINNED_SHA}
        with:
          python-version: "3.13"
      - run: |
          echo "uses: not/an-action@v1"
"""

RELEASE_WORKFLOW = f"""name: release
on:
  push:
    branches: [main]
jobs:
  release-please:
    runs-on: ubuntu-latest
    steps:
      - uses: googleapis/release-please-action@{PINNED_SHA} # v5.0.0
"""


def check_runs_endpoint(sha: str) -> str:
    return f"repos/{REPO}/commits/{sha}/check-runs?per_page=100"


def status_endpoint(sha: str) -> str:
    return f"repos/{REPO}/commits/{sha}/status?per_page=100"


def merged_pull(number: int, sha: str, merged_at: str) -> dict[str, Any]:
    return {"number": number, "merged_at": merged_at, "head": {"sha": sha}}


def workflow_file_endpoint(name: str) -> str:
    return f"repos/{REPO}/contents/.github/workflows/{name}?ref=main"


def workflow_responses(files: dict[str, str]) -> dict[str, Any]:
    responses: dict[str, Any] = {
        WORKFLOWS_ENDPOINT: [
            {"name": name, "path": f".github/workflows/{name}", "type": "file"}
            for name in files
        ]
    }
    for name, text in files.items():
        responses[workflow_file_endpoint(name)] = {
            "type": "file",
            "encoding": "base64",
            "content": base64.encodebytes(text.encode("utf-8")).decode("ascii"),
        }
    return responses


def not_protected() -> Any:
    return MODULE.ApiUnavailable(status_code=404, reason="branch_not_protected")


def classic_protection(
    *,
    contexts: tuple[str, ...] = ("test",),
    strict: bool = True,
    approvals: int = 0,
    enforce_admins: bool = True,
    conversation_resolution: bool = True,
    allow_force_pushes: bool = False,
    allow_deletions: bool = False,
) -> dict[str, Any]:
    """GET branches/{branch}/protection の実測と同じ形。"""
    return {
        "required_status_checks": {
            "strict": strict,
            "contexts": list(contexts),
            "checks": [{"context": name, "app_id": 15368} for name in contexts],
        },
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "require_last_push_approval": False,
            "required_approving_review_count": approvals,
        },
        "required_signatures": {"enabled": False},
        "enforce_admins": {"enabled": enforce_admins},
        "required_linear_history": {"enabled": False},
        "allow_force_pushes": {"enabled": allow_force_pushes},
        "allow_deletions": {"enabled": allow_deletions},
        "block_creations": {"enabled": False},
        "required_conversation_resolution": {"enabled": conversation_resolution},
        "lock_branch": {"enabled": False},
        "allow_fork_syncing": {"enabled": False},
    }


def compliant_responses() -> dict[str, object]:
    repo = REPO
    responses: dict[str, object] = {
        f"repos/{repo}": {
            "full_name": repo,
            "visibility": "public",
            "default_branch": "main",
            "owner": {"login": "example", "type": "User"},
            "delete_branch_on_merge": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "allow_auto_merge": False,
            "security_and_analysis": {
                "dependabot_security_updates": {"status": "enabled"},
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            },
        },
        "user": {"login": "example"},
        ACTIONS_ENDPOINT: {
            "enabled": True,
            "allowed_actions": "selected",
            "sha_pinning_required": True,
        },
        WORKFLOW_PERMISSIONS_ENDPOINT: {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        SELECTED_ENDPOINT: {
            "github_owned_allowed": True,
            "verified_allowed": False,
            "patterns_allowed": [],
        },
        RULESETS_ENDPOINT: [{"id": 7, "enforcement": "active"}],
        f"repos/{repo}/rulesets/7": {
            "id": 7,
            "name": "Protect main",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 0,
                        "require_code_owner_review": True,
                        "required_review_thread_resolution": True,
                    },
                },
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [{"context": "test"}],
                    },
                },
            ],
        },
        CLASSIC_ENDPOINT: not_protected(),
        PULLS_ENDPOINT: [merged_pull(12, PR_HEAD_SHA, "2026-10-01T00:00:00Z")],
        check_runs_endpoint(PR_HEAD_SHA): {"check_runs": [{"name": "test"}]},
        status_endpoint(PR_HEAD_SHA): {"statuses": []},
        f"repos/{repo}/private-vulnerability-reporting": {"enabled": True},
        f"repos/{repo}/code-scanning/default-setup": {"state": "configured"},
    }
    responses.update(workflow_responses({"ci.yml": CI_WORKFLOW}))
    return responses


def classic_only_responses(**classic: Any) -> dict[str, object]:
    """rulesets が空で classic branch protection だけが掛かった repository。"""
    responses = compliant_responses()
    responses[RULESETS_ENDPOINT] = []
    responses[CLASSIC_ENDPOINT] = classic_protection(**classic)
    return responses


def fake_api(responses: dict[str, object]):
    calls: list[str] = []

    def get(endpoint: str):
        calls.append(endpoint)
        value = responses[endpoint]
        if isinstance(value, Exception):
            raise value
        return value

    return get, calls


def review(
    responses: dict[str, object],
    profile: str = "solo_public",
    **kwargs: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    get, calls = fake_api(responses)
    report = MODULE.review_repository(REPO, profile, api_get=get, **kwargs)
    return report, {item["name"]: item for item in report["settings"]}, calls
