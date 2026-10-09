import subprocess
from types import SimpleNamespace

from github_settings_fixtures import (
    CLASSIC_ENDPOINT,
    MODULE,
    PR_HEAD_SHA,
    PULLS_ENDPOINT,
    REPO,
    check_runs_endpoint,
    classic_only_responses,
    classic_protection,
    compliant_responses,
    merged_pull,
    review,
    status_endpoint,
)

BRANCH_SETTINGS = (
    "default_branch_ruleset",
    "ruleset_deletion_protection",
    "ruleset_non_fast_forward_protection",
    "ruleset_pull_request",
    "required_review_thread_resolution",
    "required_status_checks",
    "strict_required_status_checks_policy",
    "required_approving_review_count",
    "ruleset_bypass_actors",
)


def ruleset_rules(responses, *rules):
    ruleset = dict(responses[f"repos/{REPO}/rulesets/7"])
    ruleset["rules"] = list(rules)
    responses[f"repos/{REPO}/rulesets/7"] = ruleset


def test_classic_only_repository_satisfies_default_branch_ruleset():
    responses = classic_only_responses(contexts=("pr-body-hygiene", "public-ready"))
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {
        "check_runs": [{"name": "pr-body-hygiene"}, {"name": "public-ready"}]
    }

    report, by_name, calls = review(responses)

    for name in BRANCH_SETTINGS:
        assert by_name[name]["classification"] == "no_change", name
        assert by_name[name]["blocks_intent"] is False, name
    assert by_name["default_branch_ruleset"]["observed_value"] == (
        "classic_branch_protection"
    )
    assert by_name["default_branch_ruleset"]["enforced_by"] == [
        "classic_branch_protection"
    ]
    assert report["status"] == "pass"
    assert CLASSIC_ENDPOINT in calls


def test_classic_branch_not_protected_and_no_ruleset_is_a_required_change():
    responses = compliant_responses()
    responses[f"repos/{REPO}/rulesets"] = []

    report, by_name, _ = review(responses)

    item = by_name["default_branch_ruleset"]
    assert item["tier"] == "required"
    assert item["classification"] == "human_decision"
    assert item["blocks_intent"] is True
    assert "ruleset_deletion_protection" not in by_name
    assert report["status"] == "needs_human_input"


def test_classic_permission_denied_is_unavailable_not_false():
    responses = compliant_responses()
    responses[f"repos/{REPO}/rulesets"] = []
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=404, reason="not_found_or_plan_unavailable"
    )

    report, by_name, _ = review(responses)

    for name in (
        "default_branch_ruleset",
        "ruleset_deletion_protection",
        "ruleset_non_fast_forward_protection",
        "required_status_checks",
    ):
        assert by_name[name]["classification"] == "unavailable", name
        assert by_name[name]["observed_value"] == "unknown", name
        assert by_name[name]["proposed_operation"] is None, name
    assert {item["setting"] for item in report["unknowns"]} >= {
        "default_branch_ruleset"
    }


def test_ruleset_satisfaction_stands_even_when_classic_is_unreadable():
    responses = compliant_responses()
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    item = by_name["default_branch_ruleset"]
    assert item["classification"] == "no_change"
    assert item["observed_value"] == "active"
    assert item["enforced_by"] == ["ruleset:7"]
    assert item["sources_unavailable"] == ["classic_branch_protection"]


def test_unsatisfied_family_is_unavailable_when_the_other_source_is_unreadable():
    responses = compliant_responses()
    ruleset_rules(responses, {"type": "deletion"})
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    assert by_name["ruleset_deletion_protection"]["classification"] == "no_change"
    assert (
        by_name["ruleset_non_fast_forward_protection"]["classification"]
        == "unavailable"
    )


def test_both_sources_unreadable_marks_every_branch_setting_unavailable():
    responses = compliant_responses()
    responses[f"repos/{REPO}/rulesets"] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    for name in BRANCH_SETTINGS:
        assert by_name[name]["classification"] == "unavailable", name


def test_rules_are_accumulated_across_rulesets_and_classic_protection():
    responses = compliant_responses()
    ruleset_rules(responses, {"type": "deletion"})
    responses[CLASSIC_ENDPOINT] = classic_protection(
        contexts=("test",),
        strict=True,
        approvals=1,
        conversation_resolution=True,
        allow_deletions=True,
    )

    _, by_name, _ = review(responses, "team_public")

    assert by_name["ruleset_deletion_protection"]["observed_value"] is True
    assert by_name["ruleset_deletion_protection"]["enforced_by"] == ["ruleset:7"]
    assert by_name["ruleset_non_fast_forward_protection"]["enforced_by"] == [
        "classic_branch_protection"
    ]
    assert by_name["required_approving_review_count"]["observed_value"] == 1
    assert by_name["required_approving_review_count"]["classification"] == "no_change"
    for name in (
        "ruleset_pull_request",
        "required_review_thread_resolution",
        "strict_required_status_checks_policy",
        "required_status_checks",
    ):
        assert by_name[name]["classification"] == "no_change", name


def test_required_contexts_from_both_sources_must_all_be_emitted():
    responses = compliant_responses()
    responses[CLASSIC_ENDPOINT] = classic_protection(contexts=("lint",))

    _, by_name, _ = review(responses)

    item = by_name["required_status_checks"]
    assert item["classification"] == "human_decision"
    assert item["observed_value"]["required"] == ["lint", "test"]


def test_strictest_value_wins_when_only_one_source_requires_up_to_date_branch():
    responses = compliant_responses()
    ruleset = dict(responses[f"repos/{REPO}/rulesets/7"])
    rules = [dict(rule) for rule in ruleset["rules"]]
    for rule in rules:
        if rule["type"] == "required_status_checks":
            rule["parameters"] = {
                **rule["parameters"],
                "strict_required_status_checks_policy": False,
            }
    ruleset["rules"] = rules
    responses[f"repos/{REPO}/rulesets/7"] = ruleset
    responses[CLASSIC_ENDPOINT] = classic_protection(strict=True)

    _, by_name, _ = review(responses)

    assert (
        by_name["strict_required_status_checks_policy"]["classification"] == "no_change"
    )


def test_classic_force_push_and_deletion_allowed_are_not_protection():
    responses = classic_only_responses(allow_force_pushes=True, allow_deletions=True)

    _, by_name, _ = review(responses)

    assert by_name["ruleset_deletion_protection"]["classification"] == "human_decision"
    assert (
        by_name["ruleset_non_fast_forward_protection"]["classification"]
        == "human_decision"
    )
    assert by_name["default_branch_ruleset"]["classification"] == "no_change"


def test_classic_missing_flag_is_unavailable_not_unprotected():
    responses = classic_only_responses()
    protection = classic_protection()
    protection.pop("allow_deletions")
    responses[CLASSIC_ENDPOINT] = protection

    _, by_name, _ = review(responses)

    assert by_name["ruleset_deletion_protection"]["classification"] == "unavailable"


def test_classic_admin_bypass_is_reported_as_bypass_actor():
    responses = classic_only_responses(enforce_admins=False)

    _, solo, _ = review(responses, "solo_public")
    responses = classic_only_responses(enforce_admins=False)
    _, risky, _ = review(responses, "high_risk_public")

    assert solo["ruleset_bypass_actors"]["tier"] == "recommended"
    assert solo["ruleset_bypass_actors"]["classification"] == ("recommended_change")
    assert solo["ruleset_bypass_actors"]["observed_value"] == [
        {"source": "classic_branch_protection", "actor": "repository_administrators"}
    ]
    assert risky["ruleset_bypass_actors"]["classification"] == ("human_decision")
    assert risky["ruleset_bypass_actors"]["blocks_intent"] is True


def test_classic_pull_request_bypass_allowances_are_reported():
    responses = classic_only_responses()
    protection = classic_protection()
    protection["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {
        "users": [{"login": "someone"}],
        "teams": [{"slug": "maintainers"}],
        "apps": [],
    }
    responses[CLASSIC_ENDPOINT] = protection

    _, by_name, _ = review(responses, "high_risk_public")

    observed = by_name["ruleset_bypass_actors"]["observed_value"]
    assert {"source": "classic_branch_protection", "actor": "user:someone"} in observed
    assert {
        "source": "classic_branch_protection",
        "actor": "team:maintainers",
    } in observed
    assert by_name["ruleset_bypass_actors"]["blocks_intent"] is True


def test_missing_rule_proposal_lists_classic_endpoint_without_a_fixed_body():
    responses = classic_only_responses(allow_force_pushes=True)

    _, by_name, _ = review(responses)

    operation = by_name["ruleset_non_fast_forward_protection"]["proposed_operation"]
    assert operation["method"] == "REVIEW_THEN_PUT"
    assert operation["candidate_endpoints"] == [CLASSIC_ENDPOINT]
    assert "body" not in operation
    assert operation["change"] == {"ruleset_non_fast_forward_protection": True}


def test_required_checks_are_matched_on_recent_merged_pr_head_not_main_head():
    responses = compliant_responses()
    responses[CLASSIC_ENDPOINT] = classic_protection(contexts=("pr-body-hygiene",))
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {
        "check_runs": [{"name": "test"}, {"name": "pr-body-hygiene"}]
    }

    report, by_name, calls = review(responses)

    item = by_name["required_status_checks"]
    assert item["classification"] == "no_change"
    assert item["observed_value"]["evidence_pull_requests"] == [
        {"number": 12, "head_sha": PR_HEAD_SHA}
    ]
    assert not any("commits/main" in call for call in calls)
    assert report["status"] == "pass"


def test_check_only_emitted_on_push_to_main_does_not_satisfy_requirement():
    responses = compliant_responses()
    responses[CLASSIC_ENDPOINT] = classic_protection(contexts=("deploy-on-push",))
    responses[f"repos/{REPO}/commits/main/check-runs?per_page=100"] = {
        "check_runs": [{"name": "deploy-on-push"}]
    }

    _, by_name, _ = review(responses)

    assert by_name["required_status_checks"]["classification"] == "human_decision"
    assert by_name["required_status_checks"]["blocks_intent"] is True


def test_commit_status_contexts_on_pr_head_also_count_as_emitted():
    responses = compliant_responses()
    responses[CLASSIC_ENDPOINT] = classic_protection(contexts=("legacy/ci",))
    responses[status_endpoint(PR_HEAD_SHA)] = {"statuses": [{"context": "legacy/ci"}]}

    _, by_name, _ = review(responses)

    assert by_name["required_status_checks"]["classification"] == "no_change"


def test_no_merged_pull_request_makes_check_evidence_unavailable():
    responses = compliant_responses()
    responses[PULLS_ENDPOINT] = [
        {"number": 3, "merged_at": None, "head": {"sha": PR_HEAD_SHA}}
    ]

    _, by_name, _ = review(responses)

    item = by_name["required_status_checks"]
    assert item["classification"] == "unavailable"
    assert item["unavailable_reason"] == "no_recent_merged_pull_request"


def test_pull_list_failure_makes_check_evidence_unavailable():
    responses = compliant_responses()
    responses[PULLS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    item = by_name["required_status_checks"]
    assert item["classification"] == "unavailable"
    assert item["unavailable_reason"] == "forbidden_or_plan_unavailable"


def test_older_merged_pr_is_consulted_when_latest_skipped_a_path_filtered_check():
    older_sha = "b" * 40
    responses = compliant_responses()
    responses[PULLS_ENDPOINT] = [
        merged_pull(14, older_sha, "2026-10-03T00:00:00Z"),
        merged_pull(12, PR_HEAD_SHA, "2026-10-01T00:00:00Z"),
        {"number": 13, "merged_at": None, "head": {"sha": "c" * 40}},
    ]
    responses[check_runs_endpoint(older_sha)] = {"check_runs": [{"name": "docs"}]}
    responses[status_endpoint(older_sha)] = {"statuses": []}

    _, by_name, calls = review(responses)

    item = by_name["required_status_checks"]
    assert item["classification"] == "no_change"
    assert [
        pr["number"] for pr in item["observed_value"]["evidence_pull_requests"]
    ] == [
        14,
        12,
    ]
    assert check_runs_endpoint("c" * 40) not in calls


def test_malformed_pr_head_sha_is_never_put_into_an_endpoint():
    responses = compliant_responses()
    responses[PULLS_ENDPOINT] = [
        merged_pull(12, "main/../../admin", "2026-10-01T00:00:00Z")
    ]

    _, by_name, calls = review(responses)

    assert by_name["required_status_checks"]["classification"] == "unavailable"
    assert not any("admin" in call for call in calls)


def fake_gh(monkeypatch, *, returncode, stdout="", stderr=""):
    def run(command, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(MODULE.subprocess, "run", run)


def reason_of(monkeypatch, **kwargs):
    fake_gh(monkeypatch, **kwargs)
    try:
        MODULE.gh_api_get("repos/example/repo/branches/main/protection")
    except MODULE.ApiUnavailable as exc:
        return exc.status_code, exc.reason
    raise AssertionError("ApiUnavailable was not raised")


def test_gh_api_get_separates_branch_not_protected_from_permission_404(monkeypatch):
    assert reason_of(
        monkeypatch,
        returncode=1,
        stdout='{"message":"Branch not protected","status":"404"}',
        stderr="gh: Branch not protected (HTTP 404)\n",
    ) == (404, "branch_not_protected")
    assert reason_of(
        monkeypatch,
        returncode=1,
        stdout='{"message":"Not Found","status":"404"}',
        stderr="gh: Not Found (HTTP 404)\n",
    ) == (404, "not_found_or_plan_unavailable")


def test_gh_api_get_reads_message_from_stderr_when_body_is_not_json(monkeypatch):
    assert reason_of(
        monkeypatch,
        returncode=1,
        stdout="",
        stderr="gh: Branch not protected (HTTP 404)\n",
    ) == (404, "branch_not_protected")


def test_gh_api_get_does_not_treat_other_messages_as_not_protected(monkeypatch):
    assert reason_of(
        monkeypatch,
        returncode=1,
        stdout='{"message":"Branch not found"}',
        stderr="gh: Branch not found (HTTP 404)\n",
    ) == (404, "branch_not_found")
    assert reason_of(
        monkeypatch,
        returncode=1,
        stdout='{"message":"Branch not protected but rate limited"}',
        stderr="gh: Branch not protected but rate limited (HTTP 404)\n",
    ) == (404, "not_found_or_plan_unavailable")
    assert reason_of(
        monkeypatch,
        returncode=1,
        stdout='{"message":"Branch not protected"}',
        stderr="gh: Branch not protected (HTTP 403)\n",
    ) == (403, "forbidden_or_plan_unavailable")


def test_gh_api_get_error_reason_never_copies_response_text(monkeypatch):
    status, reason = reason_of(
        monkeypatch,
        returncode=1,
        stdout='{"message":"response text LEAK-MARKER must not be copied"}',
        stderr="gh: response text LEAK-MARKER must not be copied (HTTP 401)\n",
    )

    assert (status, reason) == (401, "authentication_required")
    assert "LEAK-MARKER" not in reason


def test_gh_api_get_runs_get_only(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert MODULE.gh_api_get("repos/example/repo") == {}
    assert seen["command"][:4] == ["gh", "api", "--method", "GET"]
