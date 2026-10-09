from github_settings_fixtures import (
    ACTIONS_ENDPOINT,
    CI_WORKFLOW,
    MODULE,
    RELEASE_WORKFLOW,
    SELECTED_ENDPOINT,
    WORKFLOW_PERMISSIONS_ENDPOINT,
    WORKFLOWS_ENDPOINT,
    compliant_responses,
    review,
    workflow_file_endpoint,
    workflow_responses,
)

ACTIONS_NAMES = (
    "actions_enabled",
    "sha_pinning_required",
    "allowed_actions",
    "default_workflow_permissions",
    "can_approve_pull_request_reviews",
    "selected_actions_github_owned_allowed",
    "selected_actions_verified_allowed",
    "selected_actions_patterns",
)


def drifted_actions_responses():
    responses = compliant_responses()
    responses[ACTIONS_ENDPOINT] = {
        "enabled": True,
        "allowed_actions": "all",
        "sha_pinning_required": False,
    }
    responses[WORKFLOW_PERMISSIONS_ENDPOINT] = {
        "default_workflow_permissions": "write",
        "can_approve_pull_request_reviews": True,
    }
    return responses


def apply_operation(server, operation):
    """承認済みの 1 操作を、実行直前に取り直した現在値へ重ねて 1 回 PUT する。"""
    if operation["method"] == "SEQUENCE":
        for step in operation["steps"]:
            apply_operation(server, step)
        return
    endpoint = operation["endpoint"]
    if (
        endpoint == SELECTED_ENDPOINT
        and server[ACTIONS_ENDPOINT]["allowed_actions"] != "selected"
    ):
        raise AssertionError("409: selected-actions は selected でないと設定できない")
    if "overlay" in operation:
        fresh = server[operation["fresh_read"]["endpoint"]]
        body = {
            field: fresh[field]
            for field in operation["copy_from_fresh_read"]
            if field in fresh
        }
        body.update(operation["overlay"])
    else:
        body = dict(operation["body"])
    server[endpoint] = {**server.get(endpoint, {}), **body}


def test_sequential_execution_of_every_preview_keeps_earlier_changes():
    responses = drifted_actions_responses()
    _, by_name, _ = review(responses)
    server = {
        ACTIONS_ENDPOINT: dict(responses[ACTIONS_ENDPOINT]),
        WORKFLOW_PERMISSIONS_ENDPOINT: dict(responses[WORKFLOW_PERMISSIONS_ENDPOINT]),
    }

    for name in (
        "sha_pinning_required",
        "allowed_actions",
        "default_workflow_permissions",
        "can_approve_pull_request_reviews",
    ):
        apply_operation(server, by_name[name]["proposed_operation"])

    assert server[ACTIONS_ENDPOINT] == {
        "enabled": True,
        "allowed_actions": "selected",
        "sha_pinning_required": True,
    }
    assert server[WORKFLOW_PERMISSIONS_ENDPOINT] == {
        "default_workflow_permissions": "read",
        "can_approve_pull_request_reviews": False,
    }


def test_overlay_changes_only_the_approved_item_and_copies_the_rest_fresh():
    _, by_name, _ = review(drifted_actions_responses())

    item = by_name["default_workflow_permissions"]
    operation = item["proposed_operation"]

    assert "body" not in operation
    assert operation["overlay"] == {"default_workflow_permissions": "read"}
    assert operation["copy_from_fresh_read"] == ["can_approve_pull_request_reviews"]
    assert operation["fresh_read"]["method"] == "GET"
    assert operation["fresh_read"]["endpoint"] == WORKFLOW_PERMISSIONS_ENDPOINT
    assert operation["put_once"] is True
    assert item["rollback"]["overlay"] == {"default_workflow_permissions": "write"}


def test_no_actions_preview_embeds_observed_values_as_a_fixed_put_body():
    _, by_name, _ = review(drifted_actions_responses())

    for name in ACTIONS_NAMES:
        operation = by_name[name]["proposed_operation"] if name in by_name else None
        if operation is None:
            continue
        steps = operation["steps"] if operation["method"] == "SEQUENCE" else [operation]
        for step in steps:
            if step["endpoint"] == SELECTED_ENDPOINT:
                continue
            assert "body" not in step, name


def test_fresh_overlay_is_used_for_selected_actions_endpoint_too():
    responses = compliant_responses()
    responses[SELECTED_ENDPOINT] = {
        "github_owned_allowed": False,
        "verified_allowed": True,
        "patterns_allowed": [],
    }

    _, by_name, _ = review(responses)

    owned = by_name["selected_actions_github_owned_allowed"]["proposed_operation"]
    verified = by_name["selected_actions_verified_allowed"]["proposed_operation"]
    assert owned["overlay"] == {"github_owned_allowed": True}
    assert owned["copy_from_fresh_read"] == ["verified_allowed", "patterns_allowed"]
    assert verified["overlay"] == {"verified_allowed": False}
    assert "body" not in owned and "body" not in verified


def test_switching_to_selected_is_two_steps_with_a_derived_allow_list():
    responses = drifted_actions_responses()
    responses.update(
        workflow_responses({"ci.yml": CI_WORKFLOW, "release.yml": RELEASE_WORKFLOW})
    )

    _, by_name, calls = review(responses)

    item = by_name["allowed_actions"]
    operation = item["proposed_operation"]
    assert operation["method"] == "SEQUENCE"
    first, second = operation["steps"]
    assert first["endpoint"] == ACTIONS_ENDPOINT
    assert first["overlay"] == {"allowed_actions": "selected"}
    assert second["method"] == "PUT"
    assert second["endpoint"] == SELECTED_ENDPOINT
    assert second["body"] == {
        "github_owned_allowed": True,
        "verified_allowed": False,
        "patterns_allowed": ["googleapis/release-please-action@*"],
    }
    assert second["body_basis"] == "derived_from_default_branch_workflows"
    assert second["derivation"]["workflow_files"] == [
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
    ]
    assert "409" in operation["order_reason"]
    assert workflow_file_endpoint("ci.yml") in calls
    assert SELECTED_ENDPOINT not in calls


def test_derived_allow_list_is_executable_in_order_on_a_server_that_returns_409():
    responses = drifted_actions_responses()
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))
    _, by_name, _ = review(responses)
    server = {ACTIONS_ENDPOINT: dict(responses[ACTIONS_ENDPOINT])}

    apply_operation(server, by_name["allowed_actions"]["proposed_operation"])

    assert server[ACTIONS_ENDPOINT]["allowed_actions"] == "selected"
    assert server[SELECTED_ENDPOINT]["patterns_allowed"] == [
        "googleapis/release-please-action@*"
    ]


def test_allow_list_derivation_failure_blocks_the_second_step_without_guessing():
    responses = drifted_actions_responses()
    responses[WORKFLOWS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    operation = by_name["allowed_actions"]["proposed_operation"]
    assert operation["ready"] is False
    assert operation["blocked_reason"] == (
        "allow_list_derivation_unavailable:forbidden_or_plan_unavailable"
    )
    second = operation["steps"][1]
    assert second["endpoint"] == SELECTED_ENDPOINT
    assert second["body"] is None


def test_workflow_file_the_scanner_cannot_read_safely_blocks_derivation():
    responses = drifted_actions_responses()
    responses.update(
        workflow_responses({"x.yml": "jobs:\n  a:\n    steps: [ {uses: evil/x@v1} ]\n"})
    )

    _, by_name, _ = review(responses)

    operation = by_name["allowed_actions"]["proposed_operation"]
    assert operation["ready"] is False
    assert operation["blocked_reason"] == (
        "allow_list_derivation_unavailable:"
        "workflow_scan_refused:.github/workflows/x.yml"
    )


def test_repository_without_workflows_derives_an_empty_allow_list():
    responses = drifted_actions_responses()
    responses[WORKFLOWS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=404, reason="not_found_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    operation = by_name["allowed_actions"]["proposed_operation"]
    assert operation["steps"][1]["body"]["patterns_allowed"] == []
    assert operation["steps"][1]["derivation"]["workflow_files"] == []


def test_workflow_action_references_ignore_scalars_comments_and_with_blocks():
    text = (
        "name: demo\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      # - uses: commented/out@v1\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: run\n"
        "        run: |\n"
        "          echo 'uses: in/shell@v1'\n"
        "      - name: quoted\n"
        '        uses: "owner/quoted@v2" # trailing\n'
        "        with:\n"
        "          uses: not/an-action@v1\n"
        "  call:\n"
        "    uses: octo-org/shared/.github/workflows/deploy.yml@v1\n"
        "    secrets: inherit\n"
    )

    assert MODULE.workflow_action_references(text) == [
        "actions/checkout@v4",
        "owner/quoted@v2",
        "octo-org/shared/.github/workflows/deploy.yml@v1",
    ]


def test_workflow_action_references_fail_closed_on_constructs_it_cannot_follow():
    assert MODULE.workflow_action_references("jobs:\n\ta:\n\t\tsteps: []\n") is None
    assert (
        MODULE.workflow_action_references(
            "jobs:\n  a:\n    steps: [ {uses: x/y@v1} ]\n"
        )
        is None
    )
    assert (
        MODULE.workflow_action_references(
            "jobs:\n  a:\n    steps:\n      - *shared_step\n"
        )
        is None
    )
    assert (
        MODULE.workflow_action_references(
            "jobs:\n  a:\n    steps:\n      - uses: x/y@v1\n    <<: *defaults\n"
        )
        is None
    )
    assert MODULE.workflow_action_references("name: x\non: [push]\n") == []


def test_allow_list_derivation_classifies_each_reference_kind():
    result = MODULE.derive_selected_actions(
        [
            "actions/checkout@v4",
            "GitHub/codeql-action/analyze@v3",
            "googleapis/release-please-action@abc",
            "googleapis/release-please-action@def",
            "owner/repo/sub/dir@v1",
            "octo-org/shared/.github/workflows/deploy.yml@v1",
            "./.github/actions/local",
            "docker://alpine:3",
            "${{ matrix.action }}",
            "not-a-reference",
        ]
    )

    assert result["github_owned_used"] is True
    assert result["patterns_allowed"] == [
        "googleapis/release-please-action@*",
        "octo-org/shared/.github/workflows/deploy.yml@*",
        "owner/repo/sub/dir@*",
    ]
    assert result["local_references"] == 1
    assert result["unmapped_references"] == [
        "${{ matrix.action }}",
        "docker://alpine:3",
        "not-a-reference",
    ]


def test_already_selected_patterns_equal_to_derived_are_no_change():
    responses = compliant_responses()
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))
    responses[SELECTED_ENDPOINT] = {
        "github_owned_allowed": True,
        "verified_allowed": False,
        "patterns_allowed": ["googleapis/release-please-action@*"],
    }

    _, by_name, _ = review(responses)

    assert by_name["selected_actions_patterns"]["classification"] == "no_change"


def test_already_selected_without_needed_pattern_proposes_overlay_with_derived_list():
    responses = compliant_responses()
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    operation = item["proposed_operation"]
    assert item["classification"] == "recommended_change"
    assert operation["overlay"] == {
        "patterns_allowed": ["googleapis/release-please-action@*"]
    }
    assert operation["copy_from_fresh_read"] == [
        "github_owned_allowed",
        "verified_allowed",
    ]
    assert "body" not in operation
    assert item["rollback"]["overlay"] == {"patterns_allowed": []}


def test_patterns_without_derivation_are_unavailable_and_never_cleared_blindly():
    responses = compliant_responses()
    responses[SELECTED_ENDPOINT] = {
        "github_owned_allowed": True,
        "verified_allowed": False,
        "patterns_allowed": ["third-party/*"],
    }
    responses[WORKFLOWS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "unavailable"
    assert item["observed_value"] == "unknown"
    assert item["unavailable_reason"] == "forbidden_or_plan_unavailable"
    assert item["proposed_operation"] is None


def test_can_approve_is_recommended_not_required_and_never_blocks():
    _, by_name, _ = review(drifted_actions_responses())

    item = by_name["can_approve_pull_request_reviews"]
    assert item["tier"] == "recommended"
    assert item["classification"] == "recommended_change"
    assert item["blocks_intent"] is False
    assert by_name["default_workflow_permissions"]["tier"] == "required"


def test_can_approve_exception_covers_pull_request_creation_by_github_token():
    responses = drifted_actions_responses()
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))

    _, by_name, _ = review(responses)

    item = by_name["can_approve_pull_request_reviews"]
    assert "GITHUB_TOKEN" in item["external_effect"]
    assert "作成" in item["external_effect"]
    exception = item["exception"]
    assert exception["id"] == "github_token_pull_request_creation"
    assert [choice["id"] for choice in exception["choices"]] == [
        "keep_on_and_record_reason",
        "migrate_to_github_app_token_then_off",
    ]
    assert exception["detection"]["state"] == "ok"
    assert exception["detection"]["detected_actions"] == [
        {
            "action": "googleapis/release-please-action",
            "workflow_files": [".github/workflows/release.yml"],
        }
    ]


def test_can_approve_exception_marks_detection_unavailable_instead_of_none_found():
    responses = drifted_actions_responses()
    responses[WORKFLOWS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    detection = by_name["can_approve_pull_request_reviews"]["exception"]["detection"]
    assert detection["state"] == "unavailable"
    assert detection["reason"] == "forbidden_or_plan_unavailable"
    assert "detected_actions" not in detection


def test_can_approve_exception_is_absent_when_the_setting_already_matches():
    _, by_name, _ = review(compliant_responses())

    assert "exception" not in by_name["can_approve_pull_request_reviews"]
