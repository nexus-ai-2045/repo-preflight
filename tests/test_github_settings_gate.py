import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "github_settings_gate.py"
SPEC = importlib.util.spec_from_file_location("github_settings_gate", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def compliant_responses() -> dict[str, object]:
    repo = "example/repo"
    return {
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
        f"repos/{repo}/actions/permissions": {
            "enabled": True,
            "allowed_actions": "selected",
            "sha_pinning_required": True,
        },
        f"repos/{repo}/actions/permissions/workflow": {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        f"repos/{repo}/actions/permissions/selected-actions": {
            "github_owned_allowed": True,
            "verified_allowed": False,
            "patterns_allowed": [],
        },
        f"repos/{repo}/rulesets": [{"id": 7, "enforcement": "active"}],
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
        f"repos/{repo}/commits/main/check-runs?per_page=100": {
            "check_runs": [{"name": "test"}]
        },
        f"repos/{repo}/commits/main/status": {"statuses": []},
        f"repos/{repo}/private-vulnerability-reporting": {"enabled": True},
        f"repos/{repo}/code-scanning/default-setup": {"state": "configured"},
    }


def fake_api(responses: dict[str, object]):
    calls: list[str] = []

    def get(endpoint: str):
        calls.append(endpoint)
        value = responses[endpoint]
        if isinstance(value, Exception):
            raise value
        return value

    return get, calls


def test_solo_public_compliant_profile_passes_and_only_reads():
    get, calls = fake_api(compliant_responses())

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)

    assert report["schema_version"] == MODULE.SCHEMA_VERSION
    assert report["status"] == "pass"
    assert report["external_actions_performed"] is False
    assert report["required_change_count"] == 0
    assert all(item["classification"] == "no_change" for item in report["settings"])
    assert calls
    assert "repos/example/repo/actions/permissions/selected-actions" in calls
    assert all(
        not call.startswith(("POST ", "PUT ", "PATCH ", "DELETE ")) for call in calls
    )


def test_drift_emits_separate_exact_preview_and_never_approves_it():
    responses = compliant_responses()
    root = dict(responses["repos/example/repo"])
    root["delete_branch_on_merge"] = False
    security = dict(root["security_and_analysis"])
    security["secret_scanning_push_protection"] = {"status": "disabled"}
    root["security_and_analysis"] = security
    responses["repos/example/repo"] = root
    workflow = dict(responses["repos/example/repo/actions/permissions/workflow"])
    workflow["default_workflow_permissions"] = "write"
    responses["repos/example/repo/actions/permissions/workflow"] = workflow
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)

    by_name = {item["name"]: item for item in report["settings"]}
    assert report["status"] == "needs_human_input"
    assert by_name["default_workflow_permissions"]["tier"] == "required"
    assert by_name["default_workflow_permissions"]["blocks_intent"] is True
    assert by_name["delete_branch_on_merge"]["tier"] == "recommended"
    assert by_name["delete_branch_on_merge"]["blocks_intent"] is False
    for name in (
        "default_workflow_permissions",
        "secret_scanning_push_protection",
        "delete_branch_on_merge",
    ):
        setting = by_name[name]
        assert setting["approved"] is False
        assert setting["proposed_operation"]["method"] in {"PATCH", "PUT"}
        assert setting["proposed_operation"]["endpoint"].startswith(
            "repos/example/repo"
        )
        assert setting["rollback"]
        assert setting["external_effect"]


def test_403_is_unavailable_not_false_and_blocks_required_setting():
    responses = compliant_responses()
    responses["repos/example/repo/actions/permissions/workflow"] = (
        MODULE.ApiUnavailable(status_code=403, reason="forbidden_or_plan")
    )
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)

    by_name = {item["name"]: item for item in report["settings"]}
    item = by_name["default_workflow_permissions"]
    assert item["classification"] == "unavailable"
    assert item["observed_value"] == "unknown"
    assert item["blocks_intent"] is True
    assert item["proposed_operation"] is None
    assert report["status"] == "needs_human_input"


def test_selected_actions_detail_403_is_unavailable_not_a_compliant_policy():
    responses = compliant_responses()
    responses["repos/example/repo/actions/permissions/selected-actions"] = (
        MODULE.ApiUnavailable(status_code=403, reason="forbidden_or_plan")
    )
    get, calls = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)

    by_name = {item["name"]: item for item in report["settings"]}
    assert "repos/example/repo/actions/permissions/selected-actions" in calls
    assert (
        by_name["selected_actions_github_owned_allowed"]["classification"]
        == "unavailable"
    )
    assert by_name["selected_actions_github_owned_allowed"]["blocks_intent"] is True
    assert by_name["selected_actions_patterns"]["classification"] == "unavailable"


def test_missing_security_field_is_unavailable_not_disabled():
    responses = compliant_responses()
    root = dict(responses["repos/example/repo"])
    security = dict(root["security_and_analysis"])
    security.pop("secret_scanning_push_protection")
    root["security_and_analysis"] = security
    responses["repos/example/repo"] = root
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)

    item = next(
        item
        for item in report["settings"]
        if item["name"] == "secret_scanning_push_protection"
    )
    assert item["classification"] == "unavailable"
    assert item["observed_value"] == "unknown"
    assert item["proposed_operation"] is None


def test_team_profile_requires_one_approval_but_solo_does_not():
    get, _ = fake_api(compliant_responses())
    solo = MODULE.review_repository("example/repo", "solo_public", api_get=get)
    get, _ = fake_api(compliant_responses())
    team = MODULE.review_repository("example/repo", "team_public", api_get=get)

    solo_item = next(
        item
        for item in solo["settings"]
        if item["name"] == "required_approving_review_count"
    )
    team_item = next(
        item
        for item in team["settings"]
        if item["name"] == "required_approving_review_count"
    )
    assert solo_item["classification"] == "no_change"
    assert team_item["classification"] == "human_decision"
    assert team_item["recommended_value"] == {"minimum": 1}


def test_remote_parser_accepts_https_and_ssh_but_rejects_other_hosts():
    assert (
        MODULE.repository_from_remote("https://github.com/acme/tool.git") == "acme/tool"
    )
    assert MODULE.repository_from_remote("git@github.com:acme/tool.git") == "acme/tool"
    assert (
        MODULE.repository_from_remote("ssh://git@github.com/acme/tool.git")
        == "acme/tool"
    )
    assert MODULE.repository_from_remote("https://gitlab.com/acme/tool.git") is None


def test_actions_permission_preview_preserves_required_enabled_field():
    responses = compliant_responses()
    actions = dict(responses["repos/example/repo/actions/permissions"])
    actions["allowed_actions"] = "all"
    responses["repos/example/repo/actions/permissions"] = actions
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)
    item = next(
        setting
        for setting in report["settings"]
        if setting["name"] == "allowed_actions"
    )

    assert item["proposed_operation"]["body"] == {
        "enabled": True,
        "allowed_actions": "selected",
        "sha_pinning_required": True,
    }
    assert item["rollback"]["body"]["enabled"] is True


def test_high_risk_profile_blocks_unreviewed_selected_action_patterns():
    responses = compliant_responses()
    selected = dict(
        responses["repos/example/repo/actions/permissions/selected-actions"]
    )
    selected["patterns_allowed"] = ["third-party/*"]
    responses["repos/example/repo/actions/permissions/selected-actions"] = selected
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "high_risk_public", api_get=get)
    item = next(
        setting
        for setting in report["settings"]
        if setting["name"] == "selected_actions_patterns"
    )

    assert item["tier"] == "required"
    assert item["classification"] == "human_decision"
    assert item["blocks_intent"] is True


def test_multiple_default_branch_rulesets_are_evaluated_cumulatively():
    responses = compliant_responses()
    first = dict(responses["repos/example/repo/rulesets/7"])
    original_rules = list(first["rules"])
    first["rules"] = original_rules[:2]
    second = {
        **first,
        "id": 8,
        "name": "PR and checks",
        "rules": original_rules[2:],
    }
    responses["repos/example/repo/rulesets"] = [
        {"id": 7, "enforcement": "active"},
        {"id": 8, "enforcement": "active"},
    ]
    responses["repos/example/repo/rulesets/7"] = first
    responses["repos/example/repo/rulesets/8"] = second
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)
    by_name = {item["name"]: item for item in report["settings"]}

    for name in (
        "ruleset_deletion_protection",
        "ruleset_non_fast_forward_protection",
        "ruleset_pull_request",
        "required_review_thread_resolution",
        "required_status_checks",
        "strict_required_status_checks_policy",
    ):
        assert by_name[name]["classification"] == "no_change", name


def test_required_check_context_must_exist_on_current_default_branch():
    responses = compliant_responses()
    ruleset = dict(responses["repos/example/repo/rulesets/7"])
    rules = list(ruleset["rules"])
    check_rule = dict(rules[-1])
    params = dict(check_rule["parameters"])
    params["required_status_checks"] = [{"context": "renamed-check"}]
    check_rule["parameters"] = params
    rules[-1] = check_rule
    ruleset["rules"] = rules
    responses["repos/example/repo/rulesets/7"] = ruleset
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)
    item = next(
        setting
        for setting in report["settings"]
        if setting["name"] == "required_status_checks"
    )

    assert item["classification"] == "human_decision"
    assert item["blocks_intent"] is True


def test_high_risk_profile_blocks_unreviewed_ruleset_bypass_actor():
    responses = compliant_responses()
    ruleset = dict(responses["repos/example/repo/rulesets/7"])
    ruleset["bypass_actors"] = [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]
    responses["repos/example/repo/rulesets/7"] = ruleset
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "high_risk_public", api_get=get)
    item = next(
        setting
        for setting in report["settings"]
        if setting["name"] == "ruleset_bypass_actors"
    )

    assert item["classification"] == "human_decision"
    assert item["blocks_intent"] is True


def test_recent_advanced_codeql_analysis_satisfies_code_scanning_requirement():
    responses = compliant_responses()
    responses["repos/example/repo/code-scanning/default-setup"] = {
        "state": "not-configured"
    }
    responses["repos/example/repo/code-scanning/analyses?per_page=100"] = [
        {
            "id": 42,
            "ref": "refs/heads/main",
            "created_at": "2026-08-24T00:00:00Z",
            "tool": {"name": "CodeQL"},
        }
    ]
    get, calls = fake_api(responses)

    report = MODULE.review_repository("example/repo", "high_risk_public", api_get=get)
    item = next(
        setting
        for setting in report["settings"]
        if setting["name"] == "code_scanning_default_setup"
    )

    assert "repos/example/repo/code-scanning/analyses?per_page=100" in calls
    assert item["classification"] == "no_change"
    assert item["observed_value"]["mode"] == "advanced_or_external_analysis"


def test_stale_advanced_codeql_analysis_does_not_satisfy_requirement():
    responses = compliant_responses()
    responses["repos/example/repo/code-scanning/default-setup"] = {
        "state": "not-configured"
    }
    responses["repos/example/repo/code-scanning/analyses?per_page=100"] = [
        {
            "id": 41,
            "ref": "refs/heads/main",
            "created_at": "2025-01-01T00:00:00Z",
            "tool": {"name": "CodeQL"},
        }
    ]
    get, _ = fake_api(responses)

    report = MODULE.review_repository(
        "example/repo",
        "high_risk_public",
        api_get=get,
        observed_at=MODULE.datetime(2026, 8, 24, tzinfo=MODULE.timezone.utc),
    )
    item = next(
        setting
        for setting in report["settings"]
        if setting["name"] == "code_scanning_default_setup"
    )

    assert item["classification"] == "human_decision"
    assert item["blocks_intent"] is True


def test_authenticated_account_mismatch_blocks_settings_review():
    responses = compliant_responses()
    responses["user"] = {"login": "unexpected-collaborator"}
    get, calls = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)
    item = next(
        setting
        for setting in report["settings"]
        if setting["name"] == "authenticated_account"
    )

    assert "user" in calls
    assert report["authenticated_login"] == "unexpected-collaborator"
    assert item["classification"] == "human_decision"
    assert item["blocks_intent"] is True


def test_dependabot_security_updates_are_recommended_for_solo_profile():
    responses = compliant_responses()
    root = dict(responses["repos/example/repo"])
    security = dict(root["security_and_analysis"])
    security["dependabot_security_updates"] = {"status": "disabled"}
    root["security_and_analysis"] = security
    responses["repos/example/repo"] = root
    get, _ = fake_api(responses)

    report = MODULE.review_repository("example/repo", "solo_public", api_get=get)
    item = next(
        setting
        for setting in report["settings"]
        if setting["name"] == "dependabot_security_updates"
    )

    assert item["tier"] == "recommended"
    assert item["classification"] == "recommended_change"
    assert item["blocks_intent"] is False
