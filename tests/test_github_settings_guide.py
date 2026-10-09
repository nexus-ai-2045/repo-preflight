import re
from pathlib import Path

from github_settings_fixtures import compliant_responses, review

GUIDE = (
    Path(__file__).resolve().parents[1] / "references" / "github-settings.md"
).read_text(encoding="utf-8")


def test_ruleset_inspection_fetches_each_ruleset_detail():
    assert "repos/OWNER/REPO/rulesets/{RULESET_ID}" in GUIDE
    assert "conditions、bypass actors、required checks" in GUIDE


def test_code_scanning_inspection_distinguishes_setup_from_empty_alerts():
    assert "code-scanning/default-setup" in GUIDE
    assert "code-scanning/analyses?per_page=100" in GUIDE
    assert "alertが0件でもCodeQL設定済みとは判定しない" in GUIDE


def test_selected_actions_inspection_fetches_allowlist_details():
    assert "actions/permissions/selected-actions" in GUIDE
    assert "allowed_actions`が`selected`の場合" in GUIDE


GUIDE_ROW_TO_SETTINGS = {
    "default branchの保護": ("default_branch_ruleset",),
    "default branchの削除禁止": ("ruleset_deletion_protection",),
    "force push禁止": ("ruleset_non_fast_forward_protection",),
    "PR経由の変更": ("ruleset_pull_request",),
    "必須status checks": ("required_status_checks",),
    "branchを最新にしてからmerge": ("strict_required_status_checks_policy",),
    "review thread解決": ("required_review_thread_resolution",),
    "承認review数": ("required_approving_review_count",),
    "bypass actor": ("ruleset_bypass_actors",),
    "merge方式": (
        "allow_squash_merge",
        "allow_merge_commit",
        "allow_rebase_merge",
    ),
    "auto-merge": ("allow_auto_merge",),
    "Automatically delete head branches": ("delete_branch_on_merge",),
    "Actions実行": ("actions_enabled",),
    "Workflow permissions": ("default_workflow_permissions",),
    "PR作成・承認権限": ("can_approve_pull_request_reviews",),
    "Require actions to be pinned to a full-length commit SHA": (
        "sha_pinning_required",
    ),
    "Allowed actions": ("allowed_actions",),
    "GitHub製actionの許可": ("selected_actions_github_owned_allowed",),
    "verified creator全体の許可": ("selected_actions_verified_allowed",),
    "許可listのpattern": ("selected_actions_patterns",),
    "Dependabot security updates": ("dependabot_security_updates",),
    "Code scanning / CodeQL": ("code_scanning_default_setup",),
    "Secret scanning": ("secret_scanning",),
    "Push protection": ("secret_scanning_push_protection",),
    "Private vulnerability reporting": ("private_vulnerability_reporting",),
}

NOT_A_GUIDE_ROW = {"authenticated_account"}

TIER_ROW = re.compile(
    r"^\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<tier>必須|推奨|条件付き推奨|任意)\s*\|"
)


def guide_tiers() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in GUIDE.splitlines():
        match = TIER_ROW.match(line)
        if match:
            rows.setdefault(match["name"], []).append(match["tier"])
    return rows


def test_every_mapped_guide_row_exists_exactly_once():
    rows = guide_tiers()

    for name in GUIDE_ROW_TO_SETTINGS:
        assert len(rows.get(name, [])) == 1, name


def test_packet_tier_matches_guide_tier_for_the_solo_profile():
    _, by_name, _ = review(compliant_responses(), "solo_public")
    rows = guide_tiers()

    for guide_name, settings in GUIDE_ROW_TO_SETTINGS.items():
        guide_tier = rows[guide_name][0]
        expected = "required" if guide_tier == "必須" else "recommended"
        for setting in settings:
            assert (
                by_name[setting]["tier"] == expected
            ), f"{setting}: guide={guide_tier} packet={by_name[setting]['tier']}"


def test_every_packet_setting_is_covered_by_a_guide_row():
    _, by_name, _ = review(compliant_responses(), "solo_public")
    covered = {name for names in GUIDE_ROW_TO_SETTINGS.values() for name in names}

    assert set(by_name) - covered - NOT_A_GUIDE_ROW == set()


OFFICIAL_URL = re.compile(
    r"https://(?:github\.blog/changelog/|docs\.github\.com/)[^\s)>`]+"
)

VERIFIED_SOURCES = {
    "immutable_releases_ga": "https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available",
    "classic_to_ruleset": "https://github.blog/changelog/2026-08-11-automatically-migrate-branch-protection-rules-to-repository-rulesets",
    "copilot_approval": "https://github.blog/changelog/2026-09-01-copilot-code-review-can-now-approve-pull-requests",
    "workflow_execution_protections": "https://github.blog/changelog/2026-09-17-workflow-execution-protections-in-github-actions-generally-available",
    "pvr_rate_limits": "https://github.blog/changelog/2026-10-01-rate-limits-for-private-vulnerability-reports",
    "pvr_structured_forms": "https://github.blog/changelog/2026-10-01-structured-forms-for-private-vulnerability-reports",
    "code_scanning_ruleset": "https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets",
    "code_scanning_merge_protection": "https://docs.github.com/en/code-security/code-scanning/managing-your-code-scanning-configuration/set-code-scanning-merge-protection",
    "about_rulesets": "https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets",
    "actions_settings": "https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository",
    "dependabot_actions": "https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/auto-update-actions",
    "immutable_releases_docs": "https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases",
}


def test_baseline_marker_records_the_2026_10_09_review():
    assert "last_reviewed: 2026-10-09" in GUIDE


def test_every_new_topic_cites_an_official_source():
    for topic, url in VERIFIED_SOURCES.items():
        assert url in GUIDE, topic


def test_guide_cites_only_official_hosts_for_new_dated_claims():
    dated = [
        line
        for line in GUIDE.splitlines()
        if re.search(r"2025-10-28|2026-0[89]-\d\d|2026-10-01", line) and "http" in line
    ]

    assert dated
    for line in dated:
        for url in re.findall(r"https?://[^\s)>`]+", line):
            assert OFFICIAL_URL.match(url), url


def test_sha_pinning_order_and_security_check_guidance_are_stated():
    assert "全ての`uses:`をfull commit SHAへ固定" in GUIDE
    assert "`github-actions`を入れ" in GUIDE
    assert "先に有効にすると" in GUIDE
    assert "CodeQLのcheckを必須checkにする" in GUIDE
    assert "Require code scanning results" in GUIDE


def test_actions_api_guidance_requires_fresh_overlay_and_two_step_selected():
    assert (
        "実行直前にGETで取り直した現在値へ、承認された項目の変更だけを重ねて1回PUT"
        in GUIDE
    )
    assert "409" in GUIDE
    assert "`OWNER/REPO@*`" in GUIDE
    assert "`github_owned_allowed`" in GUIDE


def test_can_approve_row_names_the_github_token_pr_creation_exception():
    row = next(
        line for line in GUIDE.splitlines() if line.startswith("| PR作成・承認権限")
    )

    assert "`GITHUB_TOKEN`" in row
    assert "作成" in row
    assert "GitHub App token" in row
    assert "理由を記録" in row


def test_guide_explains_that_ruleset_named_settings_also_cover_classic():
    assert "`enforced_by`" in GUIDE
    assert "classic_branch_protection" in GUIDE


def test_classic_conversion_is_optional_when_classic_already_satisfies():
    assert "移行は任意" in GUIDE
    assert "併存して全て強制" in GUIDE


ENFORCED_BY_SETTINGS = (
    "default_branch_ruleset",
    "ruleset_deletion_protection",
    "ruleset_non_fast_forward_protection",
    "ruleset_pull_request",
    "required_review_thread_resolution",
    "strict_required_status_checks_policy",
)
CHANGELOG = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(
    encoding="utf-8"
)


def test_guide_names_exactly_the_settings_that_report_enforced_by():
    _, by_name, _ = review(compliant_responses())
    reporting = {name for name, item in by_name.items() if "enforced_by" in item}
    sentence = next(
        line
        for line in GUIDE.splitlines()
        if "`enforced_by`" in line and "`ruleset_" in line
    )

    assert reporting == set(ENFORCED_BY_SETTINGS)
    for name in ENFORCED_BY_SETTINGS:
        assert f"`{name}`" in sentence
    for name in ("ruleset_bypass_actors", "required_status_checks"):
        assert f"`{name}`" in sentence
        assert "enforced_by" not in by_name[name]


def test_changelog_records_the_packet_shape_changes():
    section = re.search(r"## Unreleased\n(.*?)\n## ", CHANGELOG, re.S)[1]

    assert "### packet の形の変化" in section
    shape = section.split("### packet の形の変化", 1)[1]
    for needle in (
        "{source, actor}",
        "default_branch_ruleset",
        "required_status_checks",
        "recommended_value",
        "`body`",
        "`SEQUENCE`",
        "`body_basis`",
        "`body: null`",
        "`conflicting_negated_patterns`",
        "`derivation_unavailable_reason`",
        "`unused_patterns`",
        "`overly_broad_patterns`",
        "`sources_unavailable`",
    ):
        assert needle in shape, needle


def test_guide_states_that_unused_and_overly_broad_patterns_are_not_satisfied():
    assert "`unused_patterns`" in GUIDE
    assert "`overly_broad_patterns`" in GUIDE
    assert "消す案は出さない" in GUIDE
    assert "広く読む" in GUIDE
