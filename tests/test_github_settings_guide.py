from pathlib import Path


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
