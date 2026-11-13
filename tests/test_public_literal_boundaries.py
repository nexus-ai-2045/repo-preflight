"""引用境界を保った公開検査の回帰。値は実行時に合成する。"""

import sys
from pathlib import Path
from unittest.mock import create_autospec
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import identity_gate as gate

POLICY = gate.PublicPolicy(frozenset())


def contact(domain):
    return "fixture-negative" + chr(64) + domain


@pytest.mark.parametrize("suffix", [".ljust(260, b' ')", ".strip()", ".decode()"])
def test_nested_reserved_literal_does_not_absorb_source_suffix(suffix):
    address = contact("example.invalid")
    source = 'caption = b\'"fixture-" + "' + address + "\"'" + suffix
    assert gate.unlisted_mail_found(source, POLICY) is False


@pytest.mark.parametrize(
    "form", ["plus", "adjacent", "mixed", "encoded", "nested-bytes"]
)
def test_nonreserved_literal_reconstruction_still_rejects(form):
    address = contact("mail.fixturecorporate")
    left, right = address.split(chr(64))
    chunks = [repr(left + chr(64)), repr(right)]
    if form == "adjacent":
        source = " ".join(chunks)
    elif form == "mixed":
        source = chunks[0] + ' + "' + right + '"'
    elif form == "encoded":
        source = quote(quote(" + ".join(chunks), safe=""), safe="")
    elif form == "nested-bytes":
        source = (
            "caption = "
            + repr(('"' + left + chr(64) + '" + "' + right + '"').encode())
            + ".ljust(260, b' ')"
        )
    else:
        source = " + ".join(chunks)
    assert gate.unlisted_mail_found(source, POLICY) is True


@pytest.mark.parametrize("context", ["plain", "comment"])
def test_shared_github_ssh_login_is_transport_in_body(context):
    endpoint = "git" + chr(64) + "github.com"
    body = endpoint if context == "plain" else "# SSH clone (" + endpoint + ":…)"
    assert gate.check_public_text(POLICY, body, "body") == []


@pytest.mark.parametrize(
    "local,host",
    [
        ("another", "github.com"),
        ("git", "mail.fixturecorporate"),
        ("git", "github.com.fixturecorporate"),
    ],
)
def test_shared_transport_does_not_allow_other_bare_addresses(local, host):
    address = local + chr(64) + host
    assert gate.unlisted_mail_found(address, POLICY) is True


@pytest.mark.parametrize("role", ["author", "committer", "trailer"])
def test_shared_transport_does_not_authorize_identity_roles(monkeypatch, role):
    endpoint = "git" + chr(64) + "github.com"
    allowed_address = "fixture" + chr(64) + "example.invalid"
    allowed = gate.PublicPolicy(frozenset({"Fixture <" + allowed_address + ">"}))
    if role == "trailer":
        issues = gate.check_public_text(
            allowed, "Co-authored-by: Fixture <" + endpoint + ">", "body"
        )
    else:
        author = endpoint if role == "author" else allowed_address
        committer = endpoint if role == "committer" else allowed_address
        monkeypatch.setattr(
            gate,
            "raw_commits",
            create_autospec(
                gate.raw_commits,
                return_value=[
                    ("0" * 40, "Fixture", author, "Fixture", committer, endpoint)
                ],
            ),
        )
        issues = gate.check_public_range(allowed, ".", "HEAD")
    assert any("公開許可名義と不一致" in issue for issue in issues), issues


def test_unrelated_fstring_does_not_hide_later_bytes_literal():
    address = contact("mail.fixturecorporate")
    left, right = address.split(chr(64))
    expression = '"' + left + chr(64) + '" + "' + right + '"'
    source = 'label = f"{unknown}"\ncaption = ' + repr(expression.encode())
    assert gate.unlisted_mail_found(source, POLICY) is True


def test_adjacent_literals_in_prose_still_detect_unknown_email():
    source = 'Contact is "fixture' + chr(64) + '" "company.examplecorp"'
    assert gate.unlisted_mail_found(source, POLICY)


def test_incomplete_tail_keeps_already_proven_bytes_boundary():
    expression = '"fixture-" + "sample' + chr(64) + 'example.invalid"'
    source = "caption = " + repr(expression.encode()) + ".ljust(260, b' ')\n("
    assert not gate.unlisted_mail_found(source, POLICY)


def test_tokenize_failure_on_the_same_line_keeps_detection():
    address = contact("mail.fixturecorporate")
    left, right = address.split(chr(64))
    expression = '"' + left + chr(64) + '" + "' + right + '"'
    source = "caption = " + repr(expression.encode()) + " + ("
    assert gate.unlisted_mail_found(source, POLICY)


def test_crlf_lines_keep_bytes_boundaries():
    address = contact("mail.fixturecorporate")
    left, right = address.split(chr(64))
    expression = '"' + left + chr(64) + '" + "' + right + '"'
    literal = repr(expression.encode())
    source = "first = " + literal + "\r\nsecond = " + literal + "\r\n"
    assert gate.unlisted_mail_found(source, POLICY)
