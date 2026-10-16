"""公開fixtureは予約した合成値と完全なblob束縛だけを受け付ける。"""

import hashlib
import json
from types import SimpleNamespace

import pytest

from path_input_fixture import fixture_path
from test_readiness_scan import MODULE, git, make_repo, set_remote_base
from test_reviewed_path_exceptions import policy as private_policy


def sample():
    return fixture_path("windows", "My Name", "example", forward=True)


def policy(tmp_path, data, **changes):
    path = private_policy(tmp_path, data, **changes)
    doc = json.loads(path.read_text())
    doc["version"] = 3
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(
            gh_api_get=lambda endpoint: {
                "full_name": "example/repo",
                "private": False,
                "visibility": "public",
            }
        ),
    )


def scan(repo, config):
    return MODULE.scan(repo, reviewed_secret_exceptions=config)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("url_encoded", [False, True])
def test_reviewed_public_fixture_in_deleted_history(tmp_path, encoding, url_encoded):
    repo = make_repo(tmp_path)
    text = sample().replace("/", "%2F") if url_encoded else sample()
    data = text.encode(encoding)
    f = repo / "reviewed.txt"
    f.write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic public fixture")
    config = policy(tmp_path, data)
    assert scan(repo, config)["checks"]["personal_path_scan"]["status"] == "pass"
    f.unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-m", "remove fixture")
    assert MODULE.scan(repo)["checks"]["personal_path_scan"]["status"] == "fail"
    check = scan(repo, config)["checks"]["personal_path_scan"]
    assert check["status"] == "pass"
    assert check["reviewed_exceptions"]
    assert text not in json.dumps(check)


@pytest.mark.parametrize("name", ["actual-owner", "MyActualName", "me-private"])
def test_arbitrary_home_cannot_be_registered_as_public_fixture(tmp_path, name):
    repo = make_repo(tmp_path)
    data = ("/ho" + "me/" + name + "/notes").encode()
    (repo / "reviewed.txt").write_bytes(data)
    assert scan(repo, policy(tmp_path, data))["status"] == "tool_error"


@pytest.mark.parametrize(
    "change", ["bytes", "count", "path", "additional_path", "secret"]
)
def test_public_fixture_does_not_hide_unreviewed_content(tmp_path, change):
    repo = make_repo(tmp_path)
    original = sample().encode()
    config = policy(tmp_path, original)
    data = original
    path = "elsewhere.txt" if change == "path" else "reviewed.txt"
    if change == "bytes":
        data += b"\n"
    if change == "count":
        data += b"\n" + original
    if change == "additional_path":
        data += (("\n" + fixture_path("linux", "actual-owner", "notes"))).encode()
    if change == "secret":
        data += ("\nAKIA" + "F" * 16).encode()
    (repo / path).write_bytes(data)
    r = scan(repo, config)
    assert r["checks"]["personal_path_scan"]["status"] == "fail"
    if change == "secret":
        assert r["checks"]["secret_scan"]["status"] == "fail"


def test_public_policy_cannot_register_secret_rule(tmp_path):
    repo = make_repo(tmp_path)
    config = policy(tmp_path, sample().encode(), rule="openai_key")
    assert scan(repo, config)["status"] == "tool_error"


@pytest.mark.parametrize(
    "live",
    [
        {"full_name": "example/repo", "private": True, "visibility": "private"},
        {"full_name": "other/repo", "private": False, "visibility": "public"},
        {"full_name": "example/repo", "private": 0, "visibility": "public"},
        {"full_name": "example/repo", "private": False, "visibility": "internal"},
        {},
        "offline",
    ],
)
def test_public_visibility_must_be_verified_live(tmp_path, monkeypatch, live):
    repo = make_repo(tmp_path)

    def get(_):
        if live == "offline":
            raise RuntimeError("unavailable")
        return live

    monkeypatch.setattr(
        MODULE, "load_github_settings_module", lambda: SimpleNamespace(gh_api_get=get)
    )
    assert scan(repo, policy(tmp_path, sample().encode()))["status"] == "tool_error"


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"review_reference": ""},
        {"occurrence_count": True},
        {"match_sha256": "0" * 64},
        {"repo": "other/repo"},
    ],
)
def test_public_policy_validation_is_fail_closed(tmp_path, changes):
    repo = make_repo(tmp_path)
    assert (
        scan(repo, policy(tmp_path, sample().encode(), **changes))["status"]
        == "tool_error"
    )


def test_alias_requires_its_own_review(tmp_path):
    repo = make_repo(tmp_path)
    data = sample().encode()
    (repo / "reviewed.txt").write_bytes(data)
    (repo / "unreviewed.txt").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "same blob under two paths")
    result = scan(repo, policy(tmp_path, data))
    assert result["checks"]["personal_path_scan"]["status"] == "fail"


def test_target_diff_preserves_unreviewed_path_detection(tmp_path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    data = sample().encode()
    (repo / "reviewed.txt").write_bytes(data)
    (repo / "private.txt").write_text((fixture_path("linux", "actual-owner", "notes")))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add reviewed and unreviewed material")
    result = MODULE.scan(
        repo, base_ref=base, reviewed_secret_exceptions=policy(tmp_path, data)
    )
    assert result["checks"]["personal_path_scan"]["status"] == "fail"


@pytest.mark.parametrize("extra_secret", [False, True])
def test_reviewed_new_blob_only_exempts_the_exact_fixture(tmp_path, extra_secret):
    repo = make_repo(tmp_path)
    original = sample().encode()
    addition = (
        ("\nAKIA" + "G" * 16)
        if extra_secret
        else (("\n" + fixture_path("linux", "real-owner", "data")))
    )
    data = original + addition.encode()
    (repo / "reviewed.txt").write_bytes(data)
    config = policy(tmp_path, original)
    doc = json.loads(config.read_text())
    for entry in doc["entries"]:
        entry["content_sha256"] = hashlib.sha256(data).hexdigest()
    config.write_text(json.dumps(doc))
    result = scan(repo, config)
    if extra_secret:
        assert result["checks"]["personal_path_scan"]["status"] == "pass"
        assert result["checks"]["secret_scan"]["status"] == "fail"
    else:
        assert result["checks"]["personal_path_scan"]["status"] == "fail"
