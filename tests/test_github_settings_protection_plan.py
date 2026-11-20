import json
import shutil
import subprocess

import pytest
from github_settings_fixtures import (
    CLASSIC_ENDPOINT,
    MODULE,
    PR_HEAD_SHA,
    PULLS_ENDPOINT,
    REPO,
    RULESETS_ENDPOINT,
    check_runs_endpoint,
    classic_protection,
    compliant_responses,
    fake_api,
    status_endpoint,
)

CHECKS = ("verify", "gate / preflight")


def unprotected_responses():
    responses = compliant_responses()
    responses[RULESETS_ENDPOINT] = []
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {
        "check_runs": [
            {"name": name, "app": {"id": MODULE.GITHUB_ACTIONS_APP_ID}}
            for name in (*CHECKS, "validate")
        ]
    }
    return responses


def plan(responses, checks=CHECKS, profile="solo_public", **kwargs):
    kwargs.setdefault("expected_login", "example")
    get, calls = fake_api(responses)
    result = MODULE.plan_branch_protection(REPO, profile, checks, api_get=get, **kwargs)
    return result, calls


def get_shape(body):
    return {
        "required_status_checks": {
            "strict": body["required_status_checks"]["strict"],
            "contexts": [
                item["context"] for item in body["required_status_checks"]["checks"]
            ],
            "checks": [
                {"context": item["context"], "app_id": 15368}
                for item in body["required_status_checks"]["checks"]
            ],
        },
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "require_last_push_approval": False,
            "required_approving_review_count": body["required_pull_request_reviews"][
                "required_approving_review_count"
            ],
        },
        "required_signatures": {"enabled": False},
        "restrictions": None,
        "enforce_admins": {"enabled": body["enforce_admins"]},
        "required_linear_history": {"enabled": False},
        "allow_force_pushes": {"enabled": body["allow_force_pushes"]},
        "allow_deletions": {"enabled": body["allow_deletions"]},
        "block_creations": {"enabled": False},
        "required_conversation_resolution": {
            "enabled": body["required_conversation_resolution"]
        },
        "lock_branch": {"enabled": False},
        "allow_fork_syncing": {"enabled": False},
    }


def test_unprotected_branch_gets_a_create_plan_with_admins_enforced():
    result, calls = plan(unprotected_responses())

    assert result["state"] == "create"
    assert result["blocked_reasons"] == []
    assert result["basis"]["state"] == "absent"
    assert result["basis"]["sha256"] is None
    assert result["operation"] == {
        "method": "PUT",
        "endpoint": CLASSIC_ENDPOINT,
        "body": {
            "required_status_checks": {
                "strict": False,
                "checks": [
                    {"context": "verify", "app_id": 15368},
                    {"context": "gate / preflight", "app_id": 15368},
                ],
            },
            "enforce_admins": True,
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "restrictions": None,
            "required_conversation_resolution": True,
            "allow_force_pushes": False,
            "allow_deletions": False,
        },
    }
    assert result["readback"]["expected"][:7] == [
        True,
        0,
        True,
        False,
        False,
        [
            {"context": "gate / preflight", "app_id": 15368},
            {"context": "verify", "app_id": 15368},
        ],
        False,
    ]
    assert result["external_actions_performed"] is False
    assert PULLS_ENDPOINT in calls


def test_team_profile_requires_one_approval():
    result, _ = plan(unprotected_responses(), profile="team_public")

    body = result["operation"]["body"]
    assert body["required_pull_request_reviews"]["required_approving_review_count"] == 1


def test_compliant_classic_protection_is_noop():
    responses = unprotected_responses()
    responses[CLASSIC_ENDPOINT] = classic_protection(contexts=CHECKS)

    result, _ = plan(responses)

    assert result["state"] == "noop"
    assert result["operation"] is None
    assert result["basis"]["state"] == "present"
    assert result["basis"]["sha256"] == MODULE.protection_digest(
        classic_protection(contexts=CHECKS)
    )


def test_existing_protection_is_overlaid_without_loosening_other_items():
    responses = unprotected_responses()
    existing = classic_protection(
        contexts=("verify",),
        strict=True,
        approvals=2,
        enforce_admins=False,
        conversation_resolution=False,
    )
    existing["required_linear_history"] = {"enabled": True}
    existing["restrictions"] = {
        "users": [{"login": "maintainer"}],
        "teams": [],
        "apps": [{"slug": "release-bot"}],
    }
    existing["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {
        "users": [{"login": "maintainer"}],
    }
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    assert result["state"] == "overlay"
    body = result["operation"]["body"]
    assert body["enforce_admins"] is True
    assert body["required_conversation_resolution"] is True
    assert body["required_status_checks"] == {
        "strict": True,
        "checks": [
            {"context": "verify", "app_id": 15368},
            {"context": "gate / preflight", "app_id": 15368},
        ],
    }
    assert body["required_pull_request_reviews"]["required_approving_review_count"] == 2
    assert body["required_pull_request_reviews"]["bypass_pull_request_allowances"] == {
        "users": ["maintainer"],
        "teams": [],
        "apps": [],
    }
    assert body["required_linear_history"] is True
    assert body["restrictions"] == {
        "users": ["maintainer"],
        "teams": [],
        "apps": ["release-bot"],
    }
    assert result["readback"]["expected"][1] == 2


def test_check_without_app_is_kept_as_any_app():
    responses = unprotected_responses()
    existing = classic_protection(contexts=CHECKS)
    existing["required_status_checks"]["checks"][0]["app_id"] = None
    existing["required_conversation_resolution"] = {"enabled": False}
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    checks = result["operation"]["body"]["required_status_checks"]["checks"]
    assert checks[0] == {"context": "verify", "app_id": -1}


def test_requested_check_not_seen_on_recent_merged_prs_blocks():
    result, _ = plan(unprotected_responses(), checks=("verify", "lint"))

    assert result["state"] == "blocked"
    assert result["blocked_reasons"] == [
        "checks_not_observed_on_recent_merged_pr_heads:lint"
    ]
    assert result["operation"] is None
    assert result["readback"] is None


def test_unreadable_check_evidence_blocks():
    responses = unprotected_responses()
    responses[PULLS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    result, _ = plan(responses)

    assert result["state"] == "blocked"
    assert result["blocked_reasons"] == [
        "check_evidence_unavailable:forbidden_or_plan_unavailable"
    ]


def test_repository_without_merged_pr_blocks():
    responses = unprotected_responses()
    responses[PULLS_ENDPOINT] = []

    result, _ = plan(responses)

    assert result["blocked_reasons"] == [
        "check_evidence_unavailable:no_recent_merged_pull_request"
    ]


def test_ruleset_on_default_branch_blocks():
    responses = compliant_responses()

    result, _ = plan(responses, checks=("test",))

    assert result["state"] == "blocked"
    assert result["blocked_reasons"] == ["ruleset_protects_default_branch:7"]


def test_plan_unavailable_classic_protection_blocks():
    responses = unprotected_responses()
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    result, _ = plan(responses)

    assert result["blocked_reasons"] == [
        "classic_protection_unavailable:forbidden_or_plan_unavailable"
    ]


def test_unknown_protection_field_blocks_instead_of_dropping_it():
    responses = unprotected_responses()
    existing = classic_protection(contexts=CHECKS)
    existing["required_merge_queue"] = {"enabled": True}
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    assert result["blocked_reasons"] == [
        "protection_shape_not_interpreted:required_merge_queue"
    ]


def test_login_mismatch_blocks():
    result, _ = plan(unprotected_responses(), expected_login="someone-else")

    assert result["blocked_reasons"] == ["authenticated_login_mismatch"]


def test_repository_identity_mismatch_blocks():
    responses = unprotected_responses()
    responses[f"repos/{REPO}"] = {
        **responses[f"repos/{REPO}"],
        "full_name": "other/repo",
    }

    result, _ = plan(responses)

    assert result["blocked_reasons"] == ["repository_identity_mismatch"]


@pytest.mark.parametrize(
    "checks",
    [(), ("",), (" verify",), ("verify ",), ("verify", "verify")],
)
def test_invalid_check_names_are_rejected(checks):
    with pytest.raises(ValueError):
        MODULE.validate_required_checks(checks)


@pytest.mark.parametrize(
    "extra",
    [[], ["--require-check", ""], ["--require-check", "a", "--require-check", "a"]],
)
def test_cli_rejects_invalid_check_names(extra):
    with pytest.raises(SystemExit) as raised:
        MODULE.main(["protection-plan", "--repository", REPO, *extra], api_get=None)
    assert raised.value.code == 2


def test_cli_prints_plan_and_exits_nonzero_when_blocked(capsys):
    get, _ = fake_api(unprotected_responses())

    code = MODULE.main(
        [
            "protection-plan",
            "--repository",
            REPO,
            "--require-check",
            "lint",
            "--expected-login",
            "example",
        ],
        api_get=get,
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["state"] == "blocked"


def test_cli_exits_zero_for_create_plan(capsys):
    get, _ = fake_api(unprotected_responses())

    code = MODULE.main(
        [
            "protection-plan",
            "--repository",
            REPO,
            "--require-check",
            "verify",
            "--expected-login",
            "example",
        ],
        api_get=get,
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["state"] == "create"


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が無い")
@pytest.mark.parametrize("check_name", ["verify", "検査 / 題"])
def test_readback_query_matches_expected_on_real_get_shape(check_name):
    responses = unprotected_responses()
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {
        "check_runs": [
            {"name": check_name, "app": {"id": MODULE.GITHUB_ACTIONS_APP_ID}}
        ]
    }
    result, _ = plan(responses, checks=(check_name,))
    read = get_shape(result["operation"]["body"])

    output = subprocess.run(
        ["jq", "-r", result["readback"]["jq"]],
        input=json.dumps(read, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert json.loads(output) == result["readback"]["expected"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が無い")
@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が無い")
def test_readback_query_detects_lost_check_app_binding():
    responses = unprotected_responses()
    existing = classic_protection(contexts=CHECKS)
    responses[CLASSIC_ENDPOINT] = existing
    result, _ = plan(responses)
    assert result["state"] == "noop"
    read = json.loads(json.dumps(existing))

    def evaluate(payload):
        return json.loads(
            subprocess.run(
                ["jq", "-r", result["readback"]["jq"]],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )

    assert evaluate(read) == result["readback"]["expected"]
    read["required_status_checks"]["checks"][0]["app_id"] = None
    assert evaluate(read) != result["readback"]["expected"]


def test_readback_query_detects_admins_not_enforced():
    result, _ = plan(unprotected_responses())
    read = get_shape(result["operation"]["body"])
    read["enforce_admins"] = {"enabled": False}

    output = subprocess.run(
        ["jq", "-r", result["readback"]["jq"]],
        input=json.dumps(read),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert json.loads(output) != result["readback"]["expected"]


def org_responses():
    responses = unprotected_responses()
    root = dict(responses[f"repos/{REPO}"])
    root["owner"] = {"login": "example-org", "type": "Organization"}
    responses[f"repos/{REPO}"] = root
    return responses


def test_missing_expected_login_blocks():
    result, _ = plan(unprotected_responses(), expected_login=None)

    assert result["blocked_reasons"] == ["expected_login_not_given"]


def test_cli_requires_expected_login():
    with pytest.raises(SystemExit) as raised:
        MODULE.main(
            ["protection-plan", "--repository", REPO, "--require-check", "a"],
            api_get=None,
        )
    assert raised.value.code == 2


def test_authenticated_login_in_pull_request_bypass_blocks():
    responses = unprotected_responses()
    existing = classic_protection(contexts=CHECKS)
    existing["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {
        "users": [{"login": "Example"}],
        "teams": [],
        "apps": [],
    }
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    assert result["blocked_reasons"] == ["authenticated_login_bypasses_pull_request"]


def test_dismissal_restrictions_are_converted_for_organization_repository():
    responses = org_responses()
    existing = classic_protection(contexts=CHECKS, conversation_resolution=False)
    existing["required_pull_request_reviews"]["dismissal_restrictions"] = {
        "users": [{"login": "lead"}],
        "teams": [{"slug": "core"}],
    }
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    reviews = result["operation"]["body"]["required_pull_request_reviews"]
    assert reviews["dismissal_restrictions"] == {
        "users": ["lead"],
        "teams": ["core"],
        "apps": [],
    }
    assert result["readback"]["expected"][7]["dismissal"] == {
        "users": ["lead"],
        "teams": ["core"],
        "apps": [],
    }


@pytest.mark.parametrize(
    ("responses", "dismissal", "reason"),
    [
        (
            org_responses,
            {"users": [], "teams": [], "apps": []},
            "empty_dismissal_restrictions_not_preserved",
        ),
        (
            unprotected_responses,
            {"users": [{"login": "lead"}]},
            "dismissal_restrictions_on_non_organization_repository",
        ),
    ],
)
def test_dismissal_restrictions_that_would_be_lost_block(responses, dismissal, reason):
    responses = responses()
    existing = classic_protection(contexts=CHECKS)
    existing["required_pull_request_reviews"]["dismissal_restrictions"] = dismissal
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    assert result["blocked_reasons"] == [reason]


def test_contexts_only_response_is_converted_to_any_app_checks():
    responses = unprotected_responses()
    existing = classic_protection(contexts=CHECKS, conversation_resolution=False)
    del existing["required_status_checks"]["checks"]
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    assert result["operation"]["body"]["required_status_checks"]["checks"] == [
        {"context": "verify", "app_id": -1},
        {"context": "gate / preflight", "app_id": -1},
    ]


def test_team_profile_raises_existing_zero_approvals_to_one():
    responses = unprotected_responses()
    responses[CLASSIC_ENDPOINT] = classic_protection(contexts=CHECKS, approvals=0)

    result, _ = plan(responses, profile="team_public")

    assert result["state"] == "overlay"
    body = result["operation"]["body"]
    assert body["required_pull_request_reviews"]["required_approving_review_count"] == 1


def test_documented_response_metadata_is_ignored():
    responses = unprotected_responses()
    existing = classic_protection(contexts=CHECKS)
    existing.update(enabled=True, name="main", protection_url="https://example.invalid")
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    assert result["state"] == "noop"


def test_ruleset_with_uninterpreted_scope_blocks():
    responses = compliant_responses()
    ruleset = dict(responses[f"repos/{REPO}/rulesets/7"])
    ruleset["conditions"] = {"ref_name": {"include": ["~ALL"], "exclude": []}}
    responses[f"repos/{REPO}/rulesets/7"] = ruleset

    result, _ = plan(responses)

    assert result["blocked_reasons"] == ["rulesets:ruleset_scope_not_interpreted:7"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が無い")
def test_readback_query_detects_loosened_preserved_items():
    responses = unprotected_responses()
    existing = classic_protection(
        contexts=CHECKS, strict=True, conversation_resolution=False
    )
    existing["required_linear_history"] = {"enabled": True}
    responses[CLASSIC_ENDPOINT] = existing
    result, _ = plan(responses)
    read = json.loads(json.dumps(existing))
    read["enforce_admins"] = {"enabled": True}
    read["required_conversation_resolution"] = {"enabled": True}

    def evaluate(payload):
        return json.loads(
            subprocess.run(
                ["jq", "-r", result["readback"]["jq"]],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )

    assert evaluate(read) == result["readback"]["expected"]
    read["required_status_checks"]["strict"] = False
    assert evaluate(read) != result["readback"]["expected"]
    read["required_status_checks"]["strict"] = True
    read["required_linear_history"] = {"enabled": False}
    assert evaluate(read) != result["readback"]["expected"]


@pytest.mark.parametrize(
    "bypass",
    [{"teams": [{"slug": "core"}]}, {"apps": [{"slug": "release-bot"}]}],
)
def test_team_or_app_pull_request_bypass_blocks(bypass):
    responses = org_responses()
    existing = classic_protection(contexts=CHECKS)
    existing["required_pull_request_reviews"]["bypass_pull_request_allowances"] = bypass
    responses[CLASSIC_ENDPOINT] = existing

    result, _ = plan(responses)

    assert result["blocked_reasons"] == ["pull_request_bypass_membership_not_resolved"]


def test_create_plan_from_commit_status_evidence_uses_any_app_binding():
    responses = unprotected_responses()
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {"check_runs": []}
    responses[status_endpoint(PR_HEAD_SHA)] = {
        "statuses": [{"context": name, "state": "success"} for name in CHECKS]
    }

    result, _ = plan(responses)

    assert result["state"] == "create"
    assert result["operation"]["body"]["required_status_checks"]["checks"] == [
        {"context": "verify", "app_id": -1},
        {"context": "gate / preflight", "app_id": -1},
    ]


def test_create_plan_preserves_observed_check_run_app_binding():
    responses = unprotected_responses()
    other_app = 119527
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {
        "check_runs": [{"name": name, "app": {"id": other_app}} for name in CHECKS]
    }

    result, _ = plan(responses)

    assert result["state"] == "create"
    assert result["operation"]["body"]["required_status_checks"]["checks"] == [
        {"context": "verify", "app_id": other_app},
        {"context": "gate / preflight", "app_id": other_app},
    ]


def test_create_plan_blocks_when_check_run_provider_is_not_resolved():
    responses = unprotected_responses()
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {
        "check_runs": [{"name": name} for name in CHECKS]
    }

    result, _ = plan(responses)

    assert result["state"] == "blocked"
    assert result["operation"] is None
    assert result["blocked_reasons"] == [
        "check_provider_not_resolved:gate / preflight",
        "check_provider_not_resolved:verify",
    ]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq が無い")
def test_readback_query_uses_contexts_when_checks_are_missing():
    responses = unprotected_responses()
    existing = classic_protection(contexts=CHECKS)
    del existing["required_status_checks"]["checks"]
    responses[CLASSIC_ENDPOINT] = existing
    result, _ = plan(responses)
    assert result["state"] == "noop"
    read = json.loads(json.dumps(existing))

    output = subprocess.run(
        ["jq", "-r", result["readback"]["jq"]],
        input=json.dumps(read),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert json.loads(output) == result["readback"]["expected"]
    assert result["readback"]["expected"][5] == [
        {"context": "gate / preflight", "app_id": -1},
        {"context": "verify", "app_id": -1},
    ]
