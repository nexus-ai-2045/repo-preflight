import pytest
from github_settings_fixtures import (
    ACTIONS_ENDPOINT,
    CLASSIC_ENDPOINT,
    MODULE,
    PINNED_SHA,
    PR_HEAD_SHA,
    RELEASE_WORKFLOW,
    RULESETS_ENDPOINT,
    SELECTED_ENDPOINT,
    WORKFLOWS_ENDPOINT,
    check_runs_endpoint,
    classic_only_responses,
    classic_protection,
    compliant_responses,
    review,
    workflow_responses,
)

DOCKER_SHA = "c" * 40
OTHER_DOCKER_SHA = "d" * 40
MAPPING_LIKE_JOB_IDS = (
    "container",
    "env",
    "secrets",
    "services",
    "outputs",
    "strategy",
    "with",
    "concurrency",
    "defaults",
    "permissions",
    "inputs",
)
UNREADABLE_WORKFLOW = (
    "jobs:\n  container:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - uses: >-\n          googleapis/release-please-action@v5\n"
)


def drifted_all_responses():
    responses = compliant_responses()
    responses[ACTIONS_ENDPOINT] = {
        "enabled": True,
        "allowed_actions": "all",
        "sha_pinning_required": True,
    }
    return responses


def selected_with(responses, patterns, **fields):
    responses[SELECTED_ENDPOINT] = {
        "github_owned_allowed": True,
        "verified_allowed": False,
        "patterns_allowed": list(patterns),
        **fields,
    }
    return responses


def docker_and_release_workflow(docker_sha=DOCKER_SHA):
    return (
        "jobs:\n  publish:\n    runs-on: ubuntu-latest\n    steps:\n"
        f"      - uses: docker/login-action@{docker_sha}\n"
        f"      - uses: googleapis/release-please-action@{PINNED_SHA}\n"
    )


def jobs_text(*step_lines):
    steps = "".join(f"      {line}\n" for line in step_lines)
    return f"jobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"


@pytest.mark.parametrize("job_id", MAPPING_LIKE_JOB_IDS)
def test_job_id_that_looks_like_a_mapping_key_is_still_scanned(job_id):
    text = (
        f"jobs:\n  {job_id}:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
    )

    assert MODULE.workflow_action_references(text) == ["actions/checkout@v4"]


def test_mapping_valued_job_and_step_keys_are_skipped_only_at_their_own_depth():
    text = (
        "jobs:\n"
        "  build:\n"
        "    container:\n"
        "      image: node:20\n"
        "    env:\n"
        "      A: b\n"
        "    services:\n"
        "      db:\n"
        "        image: postgres\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        env:\n"
        "          container: x\n"
        "        with:\n"
        "          uses: not/an-action@v1\n"
        "      - uses: owner/after-with@v2\n"
        "  call:\n"
        "    uses: octo-org/shared/.github/workflows/deploy.yml@v1\n"
        "    with:\n"
        "      uses: not/either@v1\n"
        "    secrets: inherit\n"
    )

    assert MODULE.workflow_action_references(text) == [
        "actions/checkout@v4",
        "owner/after-with@v2",
        "octo-org/shared/.github/workflows/deploy.yml@v1",
    ]


@pytest.mark.parametrize("indicator", [">-", "|", ">", "|+", ">2"])
def test_uses_value_given_as_a_block_scalar_is_refused(indicator):
    text = jobs_text(f"- uses: {indicator}", "    googleapis/release-please-action@v5")

    assert MODULE.workflow_action_references(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "jobs: {b: {steps: [{uses: x/y@v1}]}}\n",
        "jobs: [a, b]\n",
        '"jobs":\n  b:\n    steps:\n      - uses: x/y@v1\n',
        "'jobs':\n  b:\n    steps:\n      - uses: x/y@v1\n",
        "? jobs\n: {}\n",
        "jobs:\n  b: {steps: [{uses: x/y@v1}]}\n",
        "jobs:\n  b:\n    steps:\n      -\n        uses: x/y@v1\n",
        "jobs:\n  b:\n    steps:\n      - &s\n        uses: x/y@v1\n",
    ],
)
def test_top_level_and_nested_constructs_it_cannot_follow_are_refused(text):
    assert MODULE.workflow_action_references(text) is None


@pytest.mark.parametrize(
    "run_line",
    [
        '- run: "echo one',
        "- run: 'echo one",
        '- run: "echo \\"',
    ],
)
@pytest.mark.parametrize("continuation_indent", ["  ", "    "])
def test_multiline_quoted_scalar_hiding_a_uses_line_is_refused(
    run_line, continuation_indent
):
    text = jobs_text(run_line, f'{continuation_indent}uses: x/y@v1"')

    assert MODULE.workflow_action_references(text) is None


def test_empty_block_scalar_uses_is_refused_not_recorded_as_an_empty_reference():
    text = jobs_text("- uses: >-", "- uses: actions/checkout@v4")

    assert MODULE.workflow_action_references(text) is None


def test_tab_indentation_inside_a_skipped_mapping_is_refused():
    text = "jobs:\n  a:\n    env:\n      \tA: b\n    steps:\n" "      - uses: x/y@v1\n"

    assert MODULE.workflow_action_references(text) is None


def test_closed_quoted_scalars_with_escapes_are_read_normally():
    text = jobs_text(
        '- run: "echo \\"hi\\""',
        "- run: 'it''s fine'",
        "- uses: actions/checkout@v4",
    )

    assert MODULE.workflow_action_references(text) == ["actions/checkout@v4"]


@pytest.mark.parametrize(
    "step",
    [
        "- uses: &pin actions/checkout@v4",
        "- uses: !!str actions/checkout@v4",
    ],
)
def test_anchor_or_tag_on_a_uses_value_is_refused(step):
    assert MODULE.workflow_action_references(jobs_text(step)) is None


def test_document_markers_are_accepted_only_once_at_the_start():
    body = jobs_text("- uses: actions/checkout@v4")

    assert MODULE.workflow_action_references("---\n" + body) == ["actions/checkout@v4"]
    assert MODULE.workflow_action_references(body + "---\n" + body) is None
    assert MODULE.workflow_action_references("%YAML 1.2\n---\n" + body) is None


def test_unreadable_workflow_makes_the_switch_not_ready_instead_of_empty_list():
    responses = drifted_all_responses()
    responses.update(workflow_responses({"w.yml": UNREADABLE_WORKFLOW}))

    _, by_name, _ = review(responses)

    operation = by_name["allowed_actions"]["proposed_operation"]
    assert operation["ready"] is False
    assert operation["blocked_reason"] == (
        "allow_list_derivation_unavailable:"
        "workflow_scan_refused:.github/workflows/w.yml"
    )
    assert operation["steps"][1]["body"] is None


def test_unreadable_workflow_never_produces_a_clearing_proposal():
    responses = selected_with(
        compliant_responses(), ["googleapis/release-please-action@*"]
    )
    responses.update(workflow_responses({"w.yml": UNREADABLE_WORKFLOW}))

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "unavailable"
    assert item["proposed_operation"] is None
    assert item["rollback"] is None


def test_selected_policy_is_unavailable_when_workflows_cannot_be_read_even_if_empty():
    responses = compliant_responses()
    responses[WORKFLOWS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "unavailable"
    assert item["unavailable_reason"] == "forbidden_or_plan_unavailable"


def test_pattern_matching_is_conservative_and_honours_negation():
    covers = MODULE.allow_list_covers

    assert covers(["googleapis/*"], "googleapis/release-please-action@v5") == (
        True,
        [],
    )
    assert covers(["googleapis/release-please-action@*"], "googleapis/x@v1") == (
        False,
        [],
    )
    assert covers(["octocat/*"], "octocat/repo/sub@v1") == (False, [])
    assert covers(["octocat/**"], "octocat/repo/sub@v1") == (True, [])
    assert covers(
        [f"docker/login-action@{DOCKER_SHA}"], f"docker/login-action@{DOCKER_SHA}"
    )[0]
    assert not covers(
        [f"docker/login-action@{DOCKER_SHA}"], f"docker/login-action@{OTHER_DOCKER_SHA}"
    )[0]
    assert covers(["a/*", "!a/b@*"], "a/b@v1") == (False, ["!a/b@*"])
    assert covers(["a/b@v1", "a/b@v1"], "a/b@v1") == (True, [])
    assert covers([], "a/b@v1") == (False, [])


def test_existing_patterns_that_cover_used_references_are_satisfied():
    responses = selected_with(compliant_responses(), ["googleapis/*"])
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "no_change"
    assert item["proposed_operation"] is None


def test_sha_pinned_pattern_that_covers_the_reference_is_never_loosened():
    responses = selected_with(
        compliant_responses(), [f"googleapis/release-please-action@{PINNED_SHA}"]
    )
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "no_change"
    assert item["proposed_operation"] is None


def test_missing_reference_adds_only_the_missing_pattern_and_keeps_existing_ones():
    pinned = f"docker/login-action@{DOCKER_SHA}"
    responses = selected_with(compliant_responses(), [pinned])
    responses.update(
        workflow_responses(
            {"publish.yml": docker_and_release_workflow(OTHER_DOCKER_SHA)}
        )
    )

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "recommended_change"
    assert item["proposed_operation"]["overlay"] == {
        "patterns_allowed": [
            pinned,
            f"docker/login-action@{OTHER_DOCKER_SHA}",
            "googleapis/release-please-action@*",
        ]
    }
    assert (
        "docker/login-action@*"
        not in item["proposed_operation"]["overlay"]["patterns_allowed"]
    )
    assert item["rollback"]["overlay"] == {"patterns_allowed": [pinned]}


def test_proposal_never_removes_an_existing_pattern_even_when_it_is_unused():
    responses = selected_with(compliant_responses(), ["unused/org@*"])
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))

    _, by_name, _ = review(responses)

    overlay = by_name["selected_actions_patterns"]["proposed_operation"]["overlay"]
    assert overlay["patterns_allowed"] == [
        "unused/org@*",
        "googleapis/release-please-action@*",
    ]


def test_negated_pattern_blocking_a_used_action_is_left_to_a_human():
    responses = selected_with(
        compliant_responses(),
        ["googleapis/*", "!googleapis/release-please-action@*"],
    )
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "recommended_change"
    assert item["proposed_operation"] is None
    assert item["conflicting_negated_patterns"] == [
        "!googleapis/release-please-action@*"
    ]


def test_local_only_policy_is_stricter_than_selected_and_never_loosened():
    responses = compliant_responses()
    responses[ACTIONS_ENDPOINT] = {
        "enabled": True,
        "allowed_actions": "local_only",
        "sha_pinning_required": True,
    }

    _, by_name, calls = review(responses, "high_risk_public")

    item = by_name["allowed_actions"]
    assert item["classification"] == "no_change"
    assert item["proposed_operation"] is None
    assert item["blocks_intent"] is False
    assert "selected_actions_patterns" not in by_name
    assert SELECTED_ENDPOINT not in calls


def test_all_policy_still_gets_the_two_step_switch():
    responses = drifted_all_responses()

    _, by_name, _ = review(responses)

    operation = by_name["allowed_actions"]["proposed_operation"]
    assert operation["method"] == "SEQUENCE"
    assert operation["ready"] is True
    assert "blocked_reason" not in operation


@pytest.mark.parametrize(
    "step, expected",
    [
        ("- uses: docker://alpine:3", "unmapped_references:1"),
        ("- uses: ./.github/actions/local", "local_actions_not_scanned:1"),
    ],
)
def test_switch_is_not_ready_with_unmapped_or_unscanned_local_references(
    step, expected
):
    responses = drifted_all_responses()
    responses.update(
        workflow_responses({"w.yml": jobs_text(step, "- uses: actions/checkout@v4")})
    )

    _, by_name, _ = review(responses)

    operation = by_name["allowed_actions"]["proposed_operation"]
    assert operation["ready"] is False
    assert operation["blocked_reason"] == f"allow_list_needs_review:{expected}"
    assert operation["steps"][1]["body"]["patterns_allowed"] == []


def test_both_review_reasons_are_reported_together():
    responses = drifted_all_responses()
    responses.update(
        workflow_responses(
            {
                "w.yml": jobs_text(
                    "- uses: docker://alpine:3",
                    "- uses: ./a",
                    "- uses: $/b",
                )
            }
        )
    )

    _, by_name, _ = review(responses)

    assert by_name["allowed_actions"]["proposed_operation"]["blocked_reason"] == (
        "allow_list_needs_review:unmapped_references:1,local_actions_not_scanned:2"
    )


def test_dollar_slash_references_are_counted_as_unscanned_local_actions():
    result = MODULE.derive_selected_actions(["./a", "$/b", "owner/repo@v1"])

    assert result["local_references"] == 2
    assert result["unmapped_references"] == []


def test_ruleset_without_bypass_actors_key_is_unavailable_not_empty():
    responses = compliant_responses()
    ruleset = dict(responses["repos/example/repo/rulesets/7"])
    ruleset.pop("bypass_actors")
    responses["repos/example/repo/rulesets/7"] = ruleset

    _, by_name, _ = review(responses, "high_risk_public")

    item = by_name["ruleset_bypass_actors"]
    assert item["classification"] == "unavailable"
    assert item["unavailable_reason"] == "rulesets:bypass_actors_not_returned"
    assert item["blocks_intent"] is True


def test_empty_bypass_actors_list_is_still_no_bypass():
    _, by_name, _ = review(compliant_responses(), "high_risk_public")

    assert by_name["ruleset_bypass_actors"]["classification"] == "no_change"


def test_known_bypass_actor_stays_false_even_if_another_ruleset_hides_its_list():
    responses = compliant_responses()
    first = dict(responses["repos/example/repo/rulesets/7"])
    first["bypass_actors"] = [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]
    second = {**first, "id": 8}
    second.pop("bypass_actors")
    responses[RULESETS_ENDPOINT] = [
        {"id": 7, "enforcement": "active"},
        {"id": 8, "enforcement": "active"},
    ]
    responses["repos/example/repo/rulesets/7"] = first
    responses["repos/example/repo/rulesets/8"] = second

    _, by_name, _ = review(responses, "high_risk_public")

    assert by_name["ruleset_bypass_actors"]["classification"] == "human_decision"


def test_required_checks_are_not_confirmed_while_a_source_is_unreadable():
    responses = compliant_responses()
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )

    _, by_name, _ = review(responses)

    item = by_name["required_status_checks"]
    assert item["classification"] == "unavailable"
    assert item["blocks_intent"] is True
    assert item["unavailable_reason"] == (
        "classic_branch_protection:forbidden_or_plan_unavailable"
    )


def test_missing_required_check_is_false_even_when_the_other_source_is_unreadable():
    responses = compliant_responses()
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {"check_runs": [{"name": "lint"}]}

    _, by_name, _ = review(responses)

    assert by_name["required_status_checks"]["classification"] == "human_decision"


def test_rulesets_are_listed_with_per_page_100():
    _, _, calls = review(compliant_responses())

    assert RULESETS_ENDPOINT in calls
    assert "repos/example/repo/rulesets" not in calls


def test_a_full_ruleset_listing_is_unavailable_not_complete():
    responses = compliant_responses()
    responses[RULESETS_ENDPOINT] = [
        {"id": number, "enforcement": "active"} for number in range(1, 101)
    ]

    _, by_name, calls = review(responses)

    item = by_name["ruleset_deletion_protection"]
    assert item["classification"] == "unavailable"
    assert "rulesets:rulesets_listing_may_be_truncated" in item["unavailable_reason"]
    assert not any("rulesets/" in call for call in calls)


AMBIGUOUS_CONDITIONS = [
    {"include": ["~ALL"], "exclude": []},
    {"include": ["refs/heads/main"], "exclude": []},
    {"include": ["refs/heads/ma*"], "exclude": []},
    {"include": ["~DEFAULT_BRANCH", "refs/heads/other"], "exclude": []},
    {"include": ["~DEFAULT_BRANCH"], "exclude": ["refs/heads/main"]},
]


@pytest.mark.parametrize("ref_name", AMBIGUOUS_CONDITIONS)
def test_ruleset_with_scope_the_gate_cannot_interpret_is_not_counted(ref_name):
    responses = compliant_responses()
    ruleset = dict(responses["repos/example/repo/rulesets/7"])
    ruleset["conditions"] = {"ref_name": ref_name}
    responses["repos/example/repo/rulesets/7"] = ruleset

    _, by_name, _ = review(responses)

    base = by_name["default_branch_ruleset"]
    assert base["classification"] == "unavailable"
    assert base["unavailable_reason"] == "rulesets:ruleset_scope_not_interpreted:7"
    assert by_name["ruleset_deletion_protection"]["classification"] == "unavailable"


def test_ambiguous_ruleset_does_not_hide_protection_that_classic_provides():
    responses = classic_only_responses()
    responses[RULESETS_ENDPOINT] = [{"id": 7, "enforcement": "active"}]
    responses["repos/example/repo/rulesets/7"] = {
        "id": 7,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
        "rules": [{"type": "deletion"}],
    }
    responses[CLASSIC_ENDPOINT] = classic_protection(allow_deletions=False)

    _, by_name, _ = review(responses)

    item = by_name["ruleset_deletion_protection"]
    assert item["classification"] == "no_change"
    assert item["enforced_by"] == ["classic_branch_protection"]
    assert item["sources_unavailable"] == ["rulesets"]


def test_inactive_or_non_branch_rulesets_with_odd_scope_are_ignored():
    responses = compliant_responses()
    responses[RULESETS_ENDPOINT] = [
        {"id": 7, "enforcement": "active"},
        {"id": 8, "enforcement": "disabled"},
        {"id": 9, "enforcement": "active"},
    ]
    base = responses["repos/example/repo/rulesets/7"]
    responses["repos/example/repo/rulesets/8"] = {
        **base,
        "id": 8,
        "enforcement": "disabled",
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
    }
    responses["repos/example/repo/rulesets/9"] = {
        **base,
        "id": 9,
        "target": "tag",
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
    }

    _, by_name, _ = review(responses)

    assert by_name["default_branch_ruleset"]["classification"] == "no_change"
    assert "sources_unavailable" in by_name["default_branch_ruleset"]
    assert by_name["default_branch_ruleset"]["sources_unavailable"] == []


def test_missing_classic_flag_names_the_source_in_its_reason():
    responses = classic_only_responses()
    protection = classic_protection()
    protection.pop("allow_deletions")
    responses[CLASSIC_ENDPOINT] = protection

    _, by_name, _ = review(responses)

    item = by_name["ruleset_deletion_protection"]
    assert item["classification"] == "unavailable"
    assert item["unavailable_reason"] == "classic_branch_protection:field_not_returned"


def test_missing_enforce_admins_is_unavailable_with_a_reason():
    responses = classic_only_responses()
    protection = classic_protection()
    protection.pop("enforce_admins")
    responses[CLASSIC_ENDPOINT] = protection

    _, by_name, _ = review(responses)

    item = by_name["ruleset_bypass_actors"]
    assert item["classification"] == "unavailable"
    assert item["unavailable_reason"] == "classic_branch_protection:field_not_returned"


def broken_scenarios():
    scenarios = {}
    responses = compliant_responses()
    responses[RULESETS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=404, reason="not_found_or_plan_unavailable"
    )
    scenarios["both_sources_unreadable"] = responses

    responses = classic_only_responses()
    protection = classic_protection()
    for key in ("allow_deletions", "allow_force_pushes", "enforce_admins"):
        protection.pop(key)
    protection.pop("required_conversation_resolution")
    responses[CLASSIC_ENDPOINT] = protection
    scenarios["classic_flags_missing"] = responses

    responses = compliant_responses()
    responses[RULESETS_ENDPOINT] = [
        {"id": number, "enforcement": "active"} for number in range(1, 101)
    ]
    scenarios["rulesets_truncated"] = responses

    responses = compliant_responses()
    responses.update(workflow_responses({"w.yml": UNREADABLE_WORKFLOW}))
    scenarios["workflow_refused"] = responses

    responses = compliant_responses()
    responses[WORKFLOWS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )
    scenarios["workflows_unreadable"] = responses

    responses = compliant_responses()
    ruleset = dict(responses["repos/example/repo/rulesets/7"])
    ruleset.pop("bypass_actors")
    ruleset["conditions"] = {"ref_name": {"include": ["~ALL"], "exclude": []}}
    responses["repos/example/repo/rulesets/7"] = ruleset
    scenarios["ruleset_scope_and_bypass_hidden"] = responses
    return scenarios


@pytest.mark.parametrize("name", sorted(broken_scenarios()))
def test_every_unavailable_setting_carries_a_reason(name):
    responses = broken_scenarios()[name]

    for profile in ("solo_public", "team_public", "high_risk_public"):
        report, by_name, _ = review(dict(responses), profile)
        unavailable = [
            item
            for item in report["settings"]
            if item["classification"] == "unavailable"
        ]
        assert unavailable, name
        for item in unavailable:
            assert item["unavailable_reason"], (name, profile, item["name"])
        assert all(unknown["reason"] for unknown in report["unknowns"]), name


def indented(prefix, lines):
    return "".join(prefix + line + "\n" for line in lines)


WHOLE_DOCUMENT = (
    "on: push",
    "jobs:",
    "  b:",
    "    steps:",
    "      - uses: third/party@v1",
)


@pytest.mark.parametrize("prefix", [" ", "  ", "    "])
def test_document_indented_as_a_whole_is_refused_not_empty(prefix):
    text = indented(prefix, WHOLE_DOCUMENT)

    assert MODULE.workflow_action_references(text) is None


def test_document_indented_after_a_document_marker_is_refused():
    text = "---\n" + indented("  ", WHOLE_DOCUMENT)

    assert MODULE.workflow_action_references(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",
        "# only a comment\n",
        "name: x\non: [push]\n",
        "env:\n  jobs: x\n",
        "on:\n  push:\n",
    ],
)
def test_document_without_a_top_level_jobs_key_is_refused(text):
    assert MODULE.workflow_action_references(text) is None


def test_top_level_jobs_with_no_job_is_a_scan_that_found_nothing():
    assert MODULE.workflow_action_references("jobs:\n") == []


def test_indented_workflow_blocks_the_switch_instead_of_deriving_an_empty_list():
    responses = drifted_all_responses()
    responses.update(workflow_responses({"w.yml": indented("  ", WHOLE_DOCUMENT)}))

    _, by_name, _ = review(responses)

    operation = by_name["allowed_actions"]["proposed_operation"]
    assert operation["ready"] is False
    assert operation["blocked_reason"] == (
        "allow_list_derivation_unavailable:"
        "workflow_scan_refused:.github/workflows/w.yml"
    )
    assert operation["steps"][1]["body"] is None


NON_LF_BREAKS = [
    "\r",
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
]
HIDING_PLACES = {
    "comment": "      # note{c}      - uses: third/party@v1\n",
    "quoted": '      - run: "a{c}      - uses: third/party@v1"\n',
    "plain": "      - run: a{c}      - uses: third/party@v1\n",
    "block": "      - run: |\n          a{c}      - uses: third/party@v1\n",
}


@pytest.mark.parametrize("place", sorted(HIDING_PLACES))
@pytest.mark.parametrize("char", NON_LF_BREAKS)
def test_characters_that_split_lines_in_other_readers_are_refused(char, place):
    text = (
        "jobs:\n  b:\n    steps:\n"
        + HIDING_PLACES[place].replace("{c}", char)
        + "      - uses: actions/checkout@v4\n"
    )

    assert MODULE.workflow_action_references(text) is None


def test_crlf_documents_are_read_like_lf_documents():
    body = jobs_text("- uses: actions/checkout@v4", "- uses: owner/tool@v2")

    assert MODULE.workflow_action_references(body.replace("\n", "\r\n")) == [
        "actions/checkout@v4",
        "owner/tool@v2",
    ]


def test_lone_carriage_return_mixed_with_crlf_is_refused():
    body = jobs_text("- uses: actions/checkout@v4").replace("\n", "\r\n")

    assert MODULE.workflow_action_references(body + "# x\ry: 1\r\n") is None


def item_for(patterns, profile="solo_public", workflow=RELEASE_WORKFLOW):
    responses = selected_with(compliant_responses(), patterns)
    responses.update(workflow_responses({"release.yml": workflow}))
    _, by_name, _ = review(responses, profile)
    return by_name["selected_actions_patterns"]


def test_unused_pattern_next_to_a_covering_pattern_is_not_satisfied():
    solo = item_for(["googleapis/*", "unused/org@*"])
    risky = item_for(["googleapis/*", "unused/org@*"], "high_risk_public")

    assert solo["classification"] == "recommended_change"
    assert solo["unused_patterns"] == ["unused/org@*"]
    assert solo["overly_broad_patterns"] == []
    assert solo["proposed_operation"] is None
    assert solo["rollback"] is None
    assert risky["classification"] == "human_decision"
    assert risky["blocks_intent"] is True


@pytest.mark.parametrize(
    "patterns, unused, broad",
    [
        (["googleapis/*", "evil/backdoor@*"], ["evil/backdoor@*"], []),
        (["*/*"], [], ["*/*"]),
        (["**"], [], ["**"]),
        (["goo*/*"], [], ["goo*/*"]),
        (["googleapis/**"], [], ["googleapis/**"]),
        (["*/*", "evil/backdoor@*"], ["evil/backdoor@*"], ["*/*"]),
    ],
)
def test_overly_wide_allow_lists_are_never_no_change(patterns, unused, broad):
    for profile, classification in (
        ("solo_public", "recommended_change"),
        ("team_public", "recommended_change"),
        ("high_risk_public", "human_decision"),
    ):
        item = item_for(patterns, profile)

        assert item["classification"] == classification, (patterns, profile)
        assert item["unused_patterns"] == unused
        assert item["overly_broad_patterns"] == broad
        assert item["proposed_operation"] is None
        assert item["rollback"] is None
        assert item["blocks_intent"] is (profile == "high_risk_public")


def test_patterns_with_no_third_party_use_are_all_unused_and_block_high_risk():
    responses = selected_with(compliant_responses(), ["third-party/*"])
    _, by_name, _ = review(responses, "high_risk_public")

    item = by_name["selected_actions_patterns"]
    assert item["tier"] == "required"
    assert item["classification"] == "human_decision"
    assert item["blocks_intent"] is True
    assert item["unused_patterns"] == ["third-party/*"]
    assert item["proposed_operation"] is None


def test_explicit_pattern_for_a_github_owned_action_is_not_reported_as_unused():
    responses = selected_with(
        compliant_responses(), ["actions/checkout@*", "actions/setup-python@*"]
    )

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "no_change"
    assert item["unused_patterns"] == []


def test_negated_patterns_are_not_reported_as_unused_or_broad():
    item = item_for(["googleapis/*", "!evil/*", "!unused/*"])

    assert item["classification"] == "no_change"
    assert item["unused_patterns"] == []
    assert item["overly_broad_patterns"] == []


def test_missing_reference_is_added_while_unused_patterns_stay_flagged_not_removed():
    item = item_for(["third-party/*"])

    assert item["classification"] == "recommended_change"
    assert item["unused_patterns"] == ["third-party/*"]
    assert item["proposed_operation"]["overlay"] == {
        "patterns_allowed": ["third-party/*", "googleapis/release-please-action@*"]
    }


def test_exact_and_wildcard_patterns_that_match_a_used_reference_stay_satisfied():
    for patterns in (
        [f"googleapis/release-please-action@{PINNED_SHA}"],
        ["googleapis/release-please-action@*"],
        ["googleapis/*"],
    ):
        item = item_for(patterns)

        assert item["classification"] == "no_change", patterns
        assert item["unused_patterns"] == []
        assert item["overly_broad_patterns"] == []


@pytest.mark.parametrize(
    "patterns, reference, conflicts",
    [
        (["**", "!bad-org/*"], "bad-org/tool/sub@v1", ["!bad-org/*"]),
        (["**", "!Bad-Org/*"], "bad-org/tool@v1", ["!Bad-Org/*"]),
        (["**", "!bad-org/too?l@*"], "bad-org/tool@v1", ["!bad-org/too?l@*"]),
        (["**", "!bad-org/to+l@*"], "bad-org/tool@v1", ["!bad-org/to+l@*"]),
        (["**", "!bad-org/to[o]l@*"], "bad-org/tool@v1", ["!bad-org/to[o]l@*"]),
        (["!bad-org/*", "**"], "bad-org/tool/sub@v1", ["!bad-org/*"]),
        (["**", "!BAD-ORG/**"], "Bad-Org/tool/sub@v1", ["!BAD-ORG/**"]),
    ],
)
def test_negated_pattern_is_read_broadly_so_it_never_slips_through(
    patterns, reference, conflicts
):
    assert MODULE.allow_list_covers(patterns, reference) == (False, conflicts)


def test_negated_pattern_that_cannot_match_does_not_block():
    covers = MODULE.allow_list_covers

    assert covers(["**", "!bad-org/*"], "good/tool@v1") == (True, [])
    assert covers(["**", "!bad-org/tool@v1"], "bad-org/tool@v2") == (True, [])


def test_positive_patterns_stay_narrow_and_case_sensitive():
    covers = MODULE.allow_list_covers

    assert covers(["Owner/*"], "owner/x@v1")[0] is False
    assert covers(["owner/*"], "owner/x/y@v1")[0] is False
    assert covers(["owner/x@v?"], "owner/x@v1")[0] is False


def test_patterns_with_many_wildcards_are_answered_exactly_and_quickly():
    covers = MODULE.allow_list_covers
    heavy = "*a" * 40 + "b"

    matching = "*a" * 40

    assert covers([heavy], "a" * 60) == (False, [])
    assert covers(["**", "!" + heavy], "a" * 60) == (True, [])
    assert covers([matching], "a" * 60) == (True, [])
    assert covers(["**", "!" + matching], "a" * 60) == (False, ["!" + matching])


def test_negation_hidden_behind_a_broad_pattern_is_reported_for_the_used_reference():
    responses = selected_with(compliant_responses(), ["**", "!bad-org/*"])
    responses.update(
        workflow_responses({"w.yml": jobs_text("- uses: bad-org/tool/sub@v1")})
    )

    _, by_name, _ = review(responses, "high_risk_public")

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "human_decision"
    assert item["conflicting_negated_patterns"] == ["!bad-org/*"]
    assert item["overly_broad_patterns"] == ["**"]
    assert item["proposed_operation"] is None


def test_packet_keys_the_changelog_names_really_exist_in_the_packet():
    produced = {}
    responses = selected_with(compliant_responses(), ["**", "!googleapis/*"])
    responses.update(workflow_responses({"release.yml": RELEASE_WORKFLOW}))
    _, by_name, _ = review(responses)
    produced["selected_actions_patterns"] = set(by_name["selected_actions_patterns"])

    responses = compliant_responses()
    responses[WORKFLOWS_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )
    _, by_name, _ = review(responses)
    produced["selected_actions_patterns"] |= set(by_name["selected_actions_patterns"])

    responses = compliant_responses()
    responses[CLASSIC_ENDPOINT] = MODULE.ApiUnavailable(
        status_code=403, reason="forbidden_or_plan_unavailable"
    )
    responses[check_runs_endpoint(PR_HEAD_SHA)] = {"check_runs": [{"name": "lint"}]}
    _, by_name, _ = review(responses)
    produced["required_status_checks"] = set(
        by_name["required_status_checks"]["observed_value"]
    )

    assert {
        "conflicting_negated_patterns",
        "derivation_unavailable_reason",
        "unused_patterns",
        "overly_broad_patterns",
        "missing_references",
        "derived_patterns",
    } <= produced["selected_actions_patterns"]
    assert "sources_unavailable" in produced["required_status_checks"]


def test_negated_pattern_hitting_a_github_owned_used_reference_is_reported():
    responses = selected_with(compliant_responses(), ["!actions/checkout@*"])

    _, by_name, _ = review(responses)

    item = by_name["selected_actions_patterns"]
    assert item["classification"] == "recommended_change"
    assert item["conflicting_negated_patterns"] == ["!actions/checkout@*"]
    assert item["proposed_operation"] is None


@pytest.mark.parametrize(
    "text",
    [
        '{"jobs": {"b": {"steps": [{"uses": "x/y@v1"}]}}}\n',
        "- jobs:\n    b:\n      steps:\n        - uses: x/y@v1\n",
        "jobs :\n  b:\n    steps:\n      - uses: x/y@v1\n",
    ],
)
def test_documents_whose_top_level_is_not_a_plain_jobs_mapping_are_refused(text):
    assert MODULE.workflow_action_references(text) is None
