"""旧履歴のregex役割と、明示v3完全束縛の回帰検証。"""

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_identity_normalization import ENGINE, DENIED, regex_source, regex_home_root
from test_readiness_scan import MODULE
from synthetic_paths import synthetic_home_path

ROOT = Path(__file__).resolve().parents[1]
FIXED_BLOBS = (
    (
        "scripts/identity_gate.py",
        "83b0888ec7631a95afdcbb1385ca6f3b69d341a1",
        "5d220ec4291a23f3c6ca1c7215cfee24c2122edb364095b50e276723da50a39f",
    ),
    (
        "tests/test_commit_hook_templates.py",
        "d44827608ea4de4c1761aa3c8e76cd249e734037",
        "d430d8e0f9e91d27f637fecf1fb5afc3d1e66732bdd0f6a73202531f169c36d7",
    ),
    (
        "tests/test_identity_fixture_exceptions.py",
        "39b3c6cc4832951ffa974778a7f7ff3d90fe642f",
        "3e27047375feb36208d0b271ca03a6cbebe0db391d4111fcd6d9d4f60fcbe470",
    ),
    (
        "tests/test_public_identity_policy.py",
        "0446121a7a6d7b3a3b66fc5bd52a9d540725db9a",
        "0149b93fec4b1c582479254eb33cdb93a389b2b93f3ffabc138f35082fd15526",
    ),
    (
        "tests/test_identity_fixture_exceptions.py",
        "fe39b142a0c5d7cb24d6756d883dad4bc93e0596",
        "6c481d6f11c02b57afad1a1bd831dcb95ff95e160760ede32d23bc6d233216d7",
    ),
    (
        "scripts/identity_gate.py",
        "939c619b3c2eb2b10c83885842e45e646e81cbab",
        "1be5657828a6a4f555a9afe2e708d54672641f26a719deea72d7816c4a9a1eed",
    ),
)


def historical_bytes(row):
    result = subprocess.run(
        ["git", "--no-replace-objects", "cat-file", "blob", row[1]],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("固定した旧blobはこのcheckoutに存在しない")
    assert hashlib.sha256(result.stdout).hexdigest() == row[2]
    return result.stdout


def reviewed_policy(tmp_path, row, data, monkeypatch, **changes):
    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(
            gh_api_get=lambda _: {
                "full_name": "example/repo",
                "private": False,
                "visibility": "public",
            }
        ),
    )
    matches = MODULE.normalized_personal_path_matches(data)
    entries = []
    for index, ((rule, digest), count) in enumerate(matches.items()):
        entry = dict(
            id=f"historical_fixture_{index}",
            repo="example/repo",
            path=row[0],
            content_sha256=hashlib.sha256(data).hexdigest(),
            rule=rule,
            match_sha256=digest,
            occurrence_count=count,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            review_reason="合成負例の固定blobを独立レビュー",
            review_reference="review:ADR-0017",
        )
        entry.update(changes)
        entries.append(entry)
    config = tmp_path / "reviewed.json"
    config.write_text(json.dumps(dict(version=3, entries=entries)), encoding="utf-8")
    return MODULE.ReviewedSecretExceptions(
        config, "https://github.com/example/repo.git"
    )


def checker(tmp_path, monkeypatch):
    policy = tmp_path / "private-terms.txt"
    policy.write_text("release-fixture-test-denied-marker\n", encoding="utf-8")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    repo = tmp_path / "consumer"
    repo.mkdir(exist_ok=True)
    scan = MODULE.PrivateIdentityScan(repo, True)
    assert scan.report["status"] == "pass"
    return scan


@pytest.mark.parametrize("row", FIXED_BLOBS, ids=lambda row: row[1][:8])
def test_fixed_history_regex_or_explicit_v3_only(tmp_path, monkeypatch, row):
    data = historical_bytes(row)
    before = checker(tmp_path, monkeypatch)
    before.check(data)
    if row[0].startswith("scripts/"):
        assert before.findings == 0
        assert not MODULE.normalized_personal_path_matches(data)
        return
    assert before.findings == 1
    assert not MODULE.personal_path_matches(data)
    reviewed = reviewed_policy(tmp_path, row, data, monkeypatch)
    approved = reviewed.all_paths_reviewed(row[0], data, normalized=True)
    assert approved
    after = checker(tmp_path, monkeypatch)
    after.check(data, reviewed_raw_paths=approved)
    assert after.findings == 0
    assert reviewed.normalized_path_applied


FIXTURES = tuple(row for row in FIXED_BLOBS if row[0].startswith("tests/"))


@pytest.mark.parametrize("row", FIXTURES, ids=lambda row: row[1][:8])
@pytest.mark.parametrize(
    "change", ["bytes", "path", "count", "additional-path", "secret", "private-literal"]
)
def test_history_review_never_waives_changed_or_unreviewed_content(
    tmp_path, monkeypatch, row, change
):
    data = historical_bytes(row)
    reviewed = reviewed_policy(tmp_path, row, data, monkeypatch)
    path = row[0]
    if change == "path":
        path = "tests/unreviewed_alias.py"
    elif change == "count":
        for entry in reviewed.entries:
            entry["occurrence_count"] += 1
    elif change == "additional-path":
        data += (
            "\n# " + synthetic_home_path("linux", "unreviewed-operator/work")
        ).encode()
    elif change == "secret":
        data += ("\n# AKIA" + "F" * 16).encode()
    elif change == "private-literal":
        data += b"\n# release-fixture-test-denied-marker"
    else:
        data += b"\n"
    approved = reviewed.all_paths_reviewed(path, data, normalized=True)
    assert not approved
    scan = checker(tmp_path, monkeypatch)
    scan.check(data, reviewed_raw_paths=approved)
    assert scan.findings
    if change == "secret":
        assert MODULE.secret_matches(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"review_reference": ""},
        {"repo": "other/repo"},
        {"rule": "openai_key"},
        {"match_sha256": "0" * 64},
    ],
)
def test_new_fixture_signatures_preserve_policy_validation(
    tmp_path, monkeypatch, changes
):
    row = FIXTURES[0]
    data = historical_bytes(row)
    with pytest.raises(RuntimeError):
        reviewed_policy(tmp_path, row, data, monkeypatch, **changes)


@pytest.mark.parametrize("flags", ["re.I", "re.M"])
@pytest.mark.parametrize("capture", [False, True])
def test_generic_named_capture_with_constant_flags_has_regex_role(flags, capture):
    atom = r"(?P<user>[a-z0-9_-]+)" if capture else r"[^/\s]+"
    pattern = synthetic_home_path("linux", atom)
    source = regex_source(repr(pattern)).replace(")\n", ", " + flags + ")\n")
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"


@pytest.mark.parametrize(
    "context",
    ["text", "concrete", "suffix", "rebind", "dynamic-flags", "private-literal"],
)
def test_named_capture_is_not_a_name_or_path_exemption(context):
    atom = r"(?P<user>[a-z0-9_-]+)"
    pattern = synthetic_home_path("linux", atom)
    if context == "concrete":
        pattern = synthetic_home_path("linux", r"(?P<user>real-operator[a-z0-9_-]+)")
    elif context == "suffix":
        pattern += "actual-operator"
    source = regex_source(repr(pattern))
    if context == "text":
        source = "TEXT = " + repr(pattern)
    elif context == "rebind":
        source = regex_source(repr(pattern), before="re = other\n")
    elif context == "dynamic-flags":
        source = source.replace(")\n", ", flags)\n")
    elif context == "private-literal":
        source += "TEXT = " + repr(DENIED)
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"


@pytest.mark.parametrize("kind", ["private-literal", "secret"])
def test_exact_path_approval_still_checks_other_finding_families(
    tmp_path, monkeypatch, kind
):
    row = FIXTURES[0]
    data = historical_bytes(row)
    if kind == "private-literal":
        data += b"\n# release-fixture-test-denied-marker"
    else:
        data += ("\n# AKIA" + "F" * 16).encode()
    reviewed = reviewed_policy(tmp_path, row, data, monkeypatch)
    approved = reviewed.all_paths_reviewed(row[0], data, normalized=True)
    assert approved
    scan = checker(tmp_path, monkeypatch)
    scan.check(data, reviewed_raw_paths=approved)
    if kind == "private-literal":
        assert scan.findings
    else:
        assert MODULE.secret_matches(data)


@pytest.mark.parametrize(
    "suffix", ["", "/../unreviewed-operator/work", "actual-operator"]
)
def test_shared_system_regex_boundary_does_not_waive_concrete_paths(suffix):
    assertion = "(?![^/\\\\\\s'\\\"])"
    pattern = ENGINE.MACOS_SHARED_DIRECTORY + assertion + suffix
    result = ENGINE.scan_private_text(regex_source(repr(pattern)), [DENIED])
    assert result["status"] == ("pass" if not suffix else "fail")


@pytest.mark.parametrize("flags", ["re.X", "unknown()", "flags=re.I"])
def test_unknown_or_unsupported_regex_flags_are_not_a_role(flags):
    pattern = synthetic_home_path("linux", r"(?P<user>[a-z0-9_-]+)")
    source = regex_source(repr(pattern)).replace(")\n", ", " + flags + ")\n")
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"


@pytest.mark.parametrize("atom", [r"(?P<user>[A-Za-z0-9._-]+)", r"[^/\s]+"])
@pytest.mark.parametrize("flags", ["", "re.I", "re.M"])
@pytest.mark.parametrize(
    "parent",
    [
        r"..",
        r"\.\.",
        r"\x2e\x2e",
        r"\u002e\u002e",
        r"\056\056",
        r"[.][.]",
        r"(?:\.){2}",
        r"\.{2}",
    ],
)
@pytest.mark.parametrize("style", ["repr", "json-quote", "static-concat"])
def test_regex_parent_traversal_never_loses_concrete_home(
    atom, flags, parent, style, tmp_path, monkeypatch
):
    import posixpath
    import re

    # Private values exist only during this call, never in test IDs or artifacts.
    home = Path.home()
    root = str(home.parent) + "/"
    tail = "/" + parent + "/" + re.escape(home.name) + "/runtime-sentinel"
    pattern = root + atom + tail
    sample = root + "generic-runtime-component/../" + home.name + "/runtime-sentinel"
    matches_parent_input = (
        re.fullmatch(
            pattern, sample, re.I if flags == "re.I" else re.M if flags == "re.M" else 0
        )
        is not None
    )
    returns_to_home = posixpath.normpath(sample) == str(home / "runtime-sentinel")
    assert matches_parent_input and returns_to_home
    if style == "static-concat":
        expression = " + ".join(map(repr, (root, atom, tail)))
    elif style == "json-quote":
        expression = json.dumps(pattern)
    else:
        expression = repr(pattern)
    source = (
        "import re\nPATTERN = re.compile("
        + expression
        + (", " + flags if flags else "")
        + ")\n"
    )
    result = ENGINE.scan_private_text(source, [DENIED])
    assert result["status"] == "fail"
    has_normalized_match = bool(
        MODULE.normalized_personal_path_matches(source.encode())
    )
    assert has_normalized_match
    scan = checker(tmp_path, monkeypatch)
    scan.check(source.encode())
    assert scan.report["status"] != "tool_error"
    assert scan.findings >= 1


@pytest.mark.parametrize("atom", [r"(?P<user>[A-Za-z0-9._-]+)", r"[^/\s]+"])
@pytest.mark.parametrize("flags", ["", "re.I", "re.M"])
@pytest.mark.parametrize("style", ["repr", "json-quote", "static-concat"])
def test_plain_generic_path_role_without_traversal_is_preserved(atom, flags, style):
    root = synthetic_home_path("linux", "")
    tail = "/work"
    pattern = root + atom + tail
    expression = (
        " + ".join(map(repr, (root, atom, tail)))
        if style == "static-concat"
        else json.dumps(pattern) if style == "json-quote" else repr(pattern)
    )
    source = (
        "import re\nPATTERN = re.compile("
        + expression
        + (", " + flags if flags else "")
        + ")\n"
    )
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"


@pytest.mark.parametrize("flags", ["", "re.I", "re.M"])
@pytest.mark.parametrize("atom", [r"(?P<user>[A-Za-z0-9._-]+)", r"[^\\\s]+"])
def test_windows_regex_parent_component_keeps_normalized_detection(
    flags, atom, tmp_path, monkeypatch
):
    import ntpath
    import re
    from pathlib import PureWindowsPath

    username = Path.home().name
    root = str(PureWindowsPath("C:/", "Users")) + "\\"
    pattern = (
        re.escape(root)
        + atom
        + r"\\\.\.\\"
        + re.escape(username)
        + r"\\runtime-sentinel"
    )
    sample = root + "generic-runtime-component\\..\\" + username + "\\runtime-sentinel"
    matches_parent_input = (
        re.fullmatch(
            pattern, sample, re.I if flags == "re.I" else re.M if flags == "re.M" else 0
        )
        is not None
    )
    returns_to_home = ntpath.normpath(sample) == str(
        PureWindowsPath(root) / username / "runtime-sentinel"
    )
    assert matches_parent_input and returns_to_home
    source = (
        "import re\nPATTERN = re.compile("
        + repr(pattern)
        + (", " + flags if flags else "")
        + ")\n"
    )
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"
    normalized_detected = bool(MODULE.normalized_personal_path_matches(source.encode()))
    assert normalized_detected
    scan = checker(tmp_path, monkeypatch)
    scan.check(source.encode())
    assert scan.findings >= 1
