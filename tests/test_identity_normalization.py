"""公開generic正本をprivate workspaceなしで検査する合成ケース。"""

import importlib.util
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/identity_normalization.py"
SPEC = importlib.util.spec_from_file_location("identity_normalization_tested", SOURCE)
ENGINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENGINE)
DENIED = "fixture-private-identity@example.invalid"


@pytest.mark.parametrize(
    "text",
    [
        'CONTACT = ("fixture-private-" "identity@example.invalid")',
        'CONTACT = "fixture-private-identity\\x40example.invalid"',
        'CONTACT = "fixture-private-" + "identity@example.invalid"',
        '```python\nCONTACT = "fixture-private-" + "identity@example.invalid"\n```',
        '~~~python\nCONTACT = "fixture-private-" + "identity@example.invalid"\n~~~',
        '```python\nCONTACT = "fixture-private-" + "identity@example.invalid"',
    ],
)
def test_static_literal_and_fence_cases_reject_without_executing(text):
    result = ENGINE.scan_private_text(text, [DENIED])
    assert result["status"] == "fail"
    assert result["literal_match_count"] == 1


def test_reconstructed_path_has_value_free_evidence():
    result = ENGINE.scan_private_text(
        'PATH = "/" + "Users/operator-fixture/work"', [DENIED]
    )
    assert result == {
        "status": "fail",
        "literal_match_count": 0,
        "local_path_match": True,
        "reconstructed_path_match": True,
    }


def test_public_brand_and_synthetic_contact_pass():
    assert (
        ENGINE.scan_private_text("Public Brand / synthetic@example.invalid", [DENIED])[
            "status"
        ]
        == "pass"
    )


def test_target_text_is_never_executed(tmp_path):
    marker = tmp_path / "must-not-exist"
    text = "__import__('pathlib').Path(" + repr(str(marker)) + ").write_text('bad')"
    assert ENGINE.scan_private_text(text, [DENIED])["literal_match_count"] == 0
    assert not marker.exists()


def test_policy_input_is_explicit_nonempty_snapshot():
    assert ENGINE.parse_denylist_text(
        "# comment\nfixture-private\nfixture-private\n"
    ) == ["fixture-private"]
    with pytest.raises(ValueError, match="private_identity_policy_empty"):
        ENGINE.parse_denylist_text("# empty\n")


@pytest.mark.parametrize(
    "text",
    [
        '["fixture-", "private-", "operator"]',
        'const privateMarkers = [["fixture-", "private-", "operator"], ["other"]].map(parts => parts.join(""));',
        '"".join(["fixture-", "private-", "operator"])',
        '["fixture-", "private-", "operator"].join("")',
        '_join_chars("f i x t u r e - p r i v a t e - o p e r a t o r")',
    ],
)
def test_reconstructable_known_identity_array_and_spaced_literal(text):
    result = ENGINE.scan_private_text(text, ["fixture-private-operator"])
    assert result["status"] == "fail"
    assert result["literal_match_count"] == 1


def test_unrelated_arrays_and_spaced_brand_do_not_match_policy():
    result = ENGINE.scan_private_text(
        '["public-", "brand"] + _join_chars("p u b l i c")',
        ["fixture-private-operator"],
    )
    assert result["status"] == "pass"
