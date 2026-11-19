"""機種語と端末・名義の由来を、架空の値だけで区別する。"""

import pytest
from pathlib import Path

from test_local_identity_guard import GUARD, isolated_git_config


def model_word():
    return "Mac" + "Air"


def test_host_derived_model_word_is_not_an_individual_identity():
    identities = GUARD.collect_identities(sources={"host_name": [model_word()]})
    assert GUARD.find_hits("device model " + model_word(), identities) == []


@pytest.mark.parametrize(
    "source",
    [
        "os_account",
        "home_dir",
        "full_name",
        "git_global_name",
        "git_global_email",
        "private_file",
        "repo_history",
        "host_name_full",
    ],
)
@pytest.mark.parametrize("host_first", [True, False])
def test_same_word_from_independent_identity_source_is_still_detected(
    source, host_first
):
    host = ("host_name", [model_word()])
    personal = (source, [model_word()])
    sources = dict([host, personal] if host_first else [personal, host])
    identities = GUARD.collect_identities(sources=sources)
    assert GUARD.find_hits(model_word(), identities)
    assert any(item.source == source for item in identities)


@pytest.mark.parametrize(
    "host", ["Amber-MacBook-Air.local", "Amber's MacBook Pro", "MacBookPro-Amber-482"]
)
def test_complete_hostname_and_distinctive_part_are_both_detected(monkeypatch, host):
    monkeypatch.setattr(GUARD.getpass, "getuser", lambda: "runner")
    monkeypatch.setattr(GUARD, "_full_name_from_passwd", lambda: "")
    monkeypatch.setattr(GUARD, "_git_config", lambda *a, **k: [])
    monkeypatch.setattr(GUARD.socket, "gethostname", lambda: host)
    monkeypatch.setattr(GUARD, "_command_output", lambda _: "")
    monkeypatch.setattr(
        GUARD,
        "OPTIONAL_PRIVATE_FILE",
        Path("/nonexistent-fixture/private-identities.txt"),
    )
    identities = GUARD.collect_identities(environ={"HOME": "/runner"})
    assert any(item.value == host for item in identities)
    assert GUARD.find_hits(host, identities)
    assert GUARD.find_hits("Amber", identities)


def test_model_looking_distinctive_hostname_is_not_suppressed():
    value = model_word() + "482"
    identities = GUARD.collect_identities(sources={"host_name": [value]})
    assert GUARD.find_hits(value, identities)


@pytest.mark.parametrize("hostname", ["noreply-workstation", "mac", model_word()])
def test_complete_network_hostname_is_retained_even_if_it_is_a_model_word(
    monkeypatch, hostname
):
    monkeypatch.setattr(GUARD.getpass, "getuser", lambda: "runner")
    monkeypatch.setattr(GUARD, "_full_name_from_passwd", lambda: "")
    monkeypatch.setattr(GUARD, "_git_config", lambda *a, **k: [])
    monkeypatch.setattr(GUARD.socket, "gethostname", lambda: hostname)
    monkeypatch.setattr(GUARD, "_command_output", lambda _: "")
    monkeypatch.setattr(
        GUARD,
        "OPTIONAL_PRIVATE_FILE",
        Path("/nonexistent-fixture/private-identities.txt"),
    )
    identities = GUARD.collect_identities(environ={"HOME": "/runner"})
    assert GUARD.find_hits(hostname, identities)
    assert any(item.source == "host_name_full" for item in identities)


def _network_host_identities(monkeypatch, hostname):
    monkeypatch.setattr(GUARD.getpass, "getuser", lambda: "runner")
    monkeypatch.setattr(GUARD, "_full_name_from_passwd", lambda: "")
    monkeypatch.setattr(GUARD, "_git_config", lambda *a, **k: [])
    monkeypatch.setattr(GUARD.socket, "gethostname", lambda: hostname)
    monkeypatch.setattr(GUARD, "_command_output", lambda _: "")
    monkeypatch.setattr(
        GUARD,
        "OPTIONAL_PRIVATE_FILE",
        Path("/nonexistent-fixture/private-identities.txt"),
    )
    return GUARD.collect_identities(environ={"HOME": "/runner"})


# 架空の sha512 の integrity。"+xy/" と "/xyz+" が記号に挟まれて現れる (base64 では起こりうる並び)。
INTEGRITY_LINE = '"integrity": "sha512-c9h9dVab+xy/igMPc4bwTvC5dxq/xyz+tqJZwQPePsWjPlpSOnojbo=="'


@pytest.mark.parametrize("hostname", ["xy", "a1", "q"])
def test_network_hostname_shorter_than_three_ascii_characters_is_not_matched(
    monkeypatch, hostname
):
    identities = _network_host_identities(monkeypatch, hostname)
    assert not any(item.source == "host_name_full" for item in identities)
    assert GUARD.find_hits(INTEGRITY_LINE, identities) == []
    assert GUARD.find_hits(hostname, identities) == []


def test_three_character_network_hostname_still_matches_inside_a_hash_string(
    monkeypatch,
):
    identities = _network_host_identities(monkeypatch, "xyz")
    assert any(item.value == "xyz" for item in identities)
    assert GUARD.find_hits(INTEGRITY_LINE, identities)


def test_short_non_ascii_network_hostname_is_still_matched(monkeypatch):
    identities = _network_host_identities(monkeypatch, "架空")
    assert GUARD.find_hits("端末 架空 の記録", identities)


@pytest.mark.parametrize("separator", [" ", "-", "_", "\t"])
def test_model_only_friendly_name_does_not_create_a_private_identity(
    monkeypatch, separator
):
    friendly = "MacBook" + separator + "Air"
    monkeypatch.setattr(GUARD.getpass, "getuser", lambda: "runner")
    monkeypatch.setattr(GUARD, "_full_name_from_passwd", lambda: "")
    monkeypatch.setattr(GUARD, "_git_config", lambda *a, **k: [])
    monkeypatch.setattr(
        GUARD.socket, "gethostname", lambda: "independent-workstation-823"
    )
    monkeypatch.setattr(
        GUARD, "_command_output", lambda args: friendly if args[0] == "scutil" else ""
    )
    monkeypatch.setattr(
        GUARD,
        "OPTIONAL_PRIVATE_FILE",
        Path("/nonexistent-fixture/private-identities.txt"),
    )
    identities = GUARD.collect_identities(environ={"HOME": "/runner"})
    assert not GUARD.find_hits(friendly, identities)


def test_explicit_public_identity_allowance_is_not_changed_for_full_hostname():
    public = "nexus-ai-2045"
    assert not GUARD.collect_identities(
        sources={"host_name_full": [public]}, public_names={public}
    )


@pytest.mark.parametrize("separator", [" ", "-", "_", "\t"])
def test_separator_inside_model_components_remains_a_distinctive_name(
    monkeypatch, separator
):
    friendly = separator.join(["Ma", "cA", "ir"])
    monkeypatch.setattr(GUARD.getpass, "getuser", lambda: "runner")
    monkeypatch.setattr(GUARD, "_full_name_from_passwd", lambda: "")
    monkeypatch.setattr(GUARD, "_git_config", lambda *a, **k: [])
    monkeypatch.setattr(
        GUARD.socket, "gethostname", lambda: "independent-workstation-823"
    )
    monkeypatch.setattr(
        GUARD, "_command_output", lambda args: friendly if args[0] == "scutil" else ""
    )
    monkeypatch.setattr(
        GUARD,
        "OPTIONAL_PRIVATE_FILE",
        Path("/nonexistent-fixture/private-identities.txt"),
    )
    identities = GUARD.collect_identities(environ={"HOME": "/runner"})
    assert not GUARD._is_host_model_word(friendly)
    assert GUARD.find_hits(friendly, identities)
