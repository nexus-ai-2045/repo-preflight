"""伏せ字への置換と禁止メールの再混入を、架空の名義で再現する。"""

import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import identity_gate as gate

EMAIL = "71+fixture-account@users.noreply.github.com"


def email_digest(email):
    return hashlib.sha256(email.strip().casefold().encode("utf-8")).hexdigest()


def policy(monkeypatch, **extra):
    value = {
        "allow_emails": [EMAIL],
        "allow_names": [],
        "reject_all_coauthors": False,
        **extra,
    }
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    monkeypatch.setenv("PREFLIGHT_PRIVATE_PATTERNS", json.dumps(value))
    return gate.load_policy()


@pytest.mark.parametrize(
    "name", ["REDACTED", "redacted", "[REDACTED]", "ＲＥＤＡＣＴＥＤ"]
)
@pytest.mark.parametrize("role", ["作者", "コミッター"])
def test_masked_identity_is_rejected_even_when_allowlisted(monkeypatch, name, role):
    pol = policy(monkeypatch, allow_names=[name])
    issues = gate.check_identity(pol, name, EMAIL, role, "fixture")
    assert issues
    assert EMAIL not in "\n".join(issues)


@pytest.mark.parametrize("trailer", ["Co-authored-by", "Signed-off-by"])
def test_masked_trailer_is_rejected_when_coauthors_are_allowed(monkeypatch, trailer):
    pol = policy(monkeypatch)
    assert gate.scan_text(pol, f"{trailer}: REDACTED <{EMAIL}>", "fixture")


@pytest.mark.parametrize("name", ["Fixture Author", "REDACTED"])
@pytest.mark.parametrize("role", ["作者", "コミッター"])
def test_forbidden_email_wins_over_allowlist(monkeypatch, name, role):
    pol = policy(monkeypatch, deny_email_sha256s=[email_digest(EMAIL)])
    assert gate.check_identity(pol, name, EMAIL, role, "fixture")


def test_forbidden_email_still_rejected_after_changing_display_name(monkeypatch):
    pol = policy(monkeypatch, deny_email_sha256s=[email_digest(EMAIL)])
    assert gate.scan_text(pol, f"Co-authored-by: Another Name <{EMAIL}>", "fixture")


def test_email_hash_comparison_normalizes_case_and_whitespace(monkeypatch):
    pol = policy(monkeypatch, deny_email_sha256s=[email_digest(EMAIL)])
    assert not gate.email_ok(pol, "  " + EMAIL.upper() + "  ")


@pytest.mark.parametrize("value", ["", "0" * 63, "z" * 64, 5, None])
def test_invalid_hash_configuration_fails_closed(monkeypatch, value):
    with pytest.raises(ValueError):
        policy(monkeypatch, deny_email_sha256s=[value])


def test_regular_allowed_identity_and_trailer_still_pass(monkeypatch):
    pol = policy(monkeypatch)
    assert gate.check_identity(pol, "Fixture Author", EMAIL, "作者", "fixture") == []
    assert (
        gate.scan_text(pol, f"Co-authored-by: Fixture Author <{EMAIL}>", "fixture")
        == []
    )


def test_documentation_can_explain_redacted_without_being_identity(monkeypatch):
    pol = policy(monkeypatch)
    assert (
        gate.scan_text(pol, "REDACTEDとは情報を伏せたという意味です。", "fixture") == []
    )


def test_pr_event_with_masked_coauthor_is_rejected(monkeypatch, tmp_path, capsys):
    policy(monkeypatch)
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "title": "fix",
                    "body": f"Co-authored-by: REDACTED <{EMAIL}>",
                }
            }
        )
    )
    assert gate.main(["--pr-event", str(event)]) == 1
    output = capsys.readouterr()
    assert EMAIL not in output.out + output.err
