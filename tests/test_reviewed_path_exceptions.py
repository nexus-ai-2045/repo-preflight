"""完全束縛されたprivate個人path例外の境界を、合成資料と実Gitで検査する。"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from test_readiness_scan import (
    MODULE,
    git,
    make_repo,
    set_remote_base,
    make_cmo3,
    cmo3_xml,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sample_path():
    return "/Us" + "ers/synthetic-owner"


def policy(tmp_path, data, path="reviewed.txt", *, scanned_data=None, **changes):
    entries = []
    for index, ((rule, match_hash), count) in enumerate(
        MODULE.personal_path_matches(
            data if scanned_data is None else scanned_data
        ).items()
    ):
        entry = {
            "id": f"reviewed_path_{index}",
            "repo": "example/repo",
            "path": path,
            "content_sha256": digest(data),
            "rule": rule,
            "match_sha256": match_hash,
            "occurrence_count": count,
            "review_reason": "同一operatorのprivate履歴としてレビュー済み",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "review_reference": "review-record:synthetic-approval",
        }
        entry.update(changes)
        entries.append(entry)
    assert entries
    config = tmp_path / "path-exceptions.json"
    config.write_text(json.dumps({"version": 2, "entries": entries}), encoding="utf-8")
    return config


def report(repo, config, **kwargs):
    return MODULE.scan(repo, reviewed_secret_exceptions=config, **kwargs)


@pytest.fixture(autouse=True)
def private_origin(monkeypatch):
    calls = []

    def api_get(endpoint):
        calls.append(endpoint)
        assert endpoint == "https://api.github.com/repos/example/repo"
        return {"full_name": "example/repo", "private": True, "visibility": "private"}

    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(gh_api_get=api_get),
    )
    return calls


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("url_encoded", [False, True])
def test_exact_reviewed_private_path_in_worktree_and_deleted_history(
    tmp_path, encoding, url_encoded, private_origin
):
    repo = make_repo(tmp_path)
    value = sample_path()
    if url_encoded:
        value = value.replace("/", "%2F")
    data = value.encode(encoding)
    (repo / "reviewed.txt").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "reviewed material")
    config = policy(tmp_path, data)
    assert report(repo, config)["checks"]["personal_path_scan"]["status"] == "pass"
    assert private_origin == ["https://api.github.com/repos/example/repo"]
    (repo / "reviewed.txt").unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-m", "delete reviewed material")
    assert MODULE.scan(repo)["checks"]["personal_path_scan"]["status"] == "fail"
    assert report(repo, config)["checks"]["personal_path_scan"]["status"] == "pass"
    assert sample_path() not in json.dumps(report(repo, config))


@pytest.mark.parametrize(
    "live",
    [
        {"full_name": "example/repo", "private": False, "visibility": "public"},
        {"full_name": "example/repo", "private": True, "visibility": "internal"},
        {"full_name": "other/repo", "private": True, "visibility": "private"},
        {"full_name": "example/repo", "private": "true", "visibility": "private"},
        {},
        None,
        [],
        "offline",
    ],
)
def test_live_private_origin_is_required(tmp_path, monkeypatch, live):
    repo = make_repo(tmp_path)
    data = sample_path().encode()
    (repo / "reviewed.txt").write_bytes(data)
    config = policy(tmp_path, data)

    def api_get(_):
        if live == "offline":
            raise RuntimeError("synthetic unavailable")
        return live

    monkeypatch.setattr(
        MODULE,
        "load_github_settings_module",
        lambda: SimpleNamespace(gh_api_get=api_get),
    )
    assert report(repo, config) == {
        "status": "tool_error",
        "issues": ["reviewed_secret_exceptions_invalid"],
    }


@pytest.mark.parametrize(
    "expiry",
    [
        "2000-01-01T00:00:00Z",
        "2099-01-01",
        "2099-01-01T00:00:00+00:00",
        "2099-02-30T00:00:00Z",
        "",
    ],
)
def test_expired_and_invalid_utc_expiry_fail_closed(tmp_path, expiry, private_origin):
    repo = make_repo(tmp_path)
    config = policy(tmp_path, sample_path().encode(), expires_at=expiry)
    assert report(repo, config)["status"] == "tool_error"
    assert not private_origin


@pytest.mark.parametrize("reference", ["", "  ", "sk-" + "a" * 24])
def test_human_review_reference_is_required_without_raw_credentials(
    tmp_path, reference
):
    repo = make_repo(tmp_path)
    config = policy(tmp_path, sample_path().encode(), review_reference=reference)
    result = report(repo, config)
    assert result["status"] == "tool_error"
    assert reference.strip() not in json.dumps(result) if reference.strip() else True


@pytest.mark.parametrize(
    "change", ["bytes", "count", "path", "new_path", "wrong_match", "wrong_count"]
)
def test_exact_bindings_do_not_suppress_changed_or_additional_paths(tmp_path, change):
    repo = make_repo(tmp_path)
    original = sample_path().encode()
    data = original
    path = "elsewhere.txt" if change == "path" else "reviewed.txt"
    changes = {}
    if change == "bytes":
        data += b"\n"
    if change == "count":
        data += b" " + original
    if change == "new_path":
        data += b" /ho" + b"me/new-owner"
    if change == "wrong_match":
        changes["match_sha256"] = "0" * 64
    if change == "wrong_count":
        changes["occurrence_count"] = 2
    config = policy(
        tmp_path, data if change in {"new_path", "count"} else original, **changes
    )
    if change in {"new_path", "count"}:
        payload = json.loads(config.read_text())
        if change == "new_path":
            payload["entries"] = payload["entries"][:1]
        else:
            payload["entries"][0]["occurrence_count"] = 1
        config.write_text(json.dumps(payload))
    (repo / path).write_bytes(data)
    assert report(repo, config)["checks"]["personal_path_scan"]["status"] == "fail"


def test_path_approval_keeps_unrelated_real_secret_blocked(tmp_path):
    repo = make_repo(tmp_path)
    candidate = "ghp_" + "b" * 24
    data = (sample_path() + " " + candidate).encode()
    (repo / "reviewed.txt").write_bytes(data)
    result = report(repo, policy(tmp_path, data))
    assert result["checks"]["personal_path_scan"]["status"] == "pass"
    assert result["checks"]["secret_scan"]["status"] == "fail"
    assert candidate not in json.dumps(result)


def test_historical_blob_alias_is_not_exempted(tmp_path):
    repo = make_repo(tmp_path)
    data = sample_path().encode()
    for name in ("reviewed.txt", "copy.txt"):
        (repo / name).write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "same blob at two paths")
    for name in ("reviewed.txt", "copy.txt"):
        (repo / name).unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-m", "delete both aliases")
    assert (
        report(repo, policy(tmp_path, data))["checks"]["personal_path_scan"]["status"]
        == "fail"
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "binary"])
def test_target_diff_checks_exact_intermediate_blob_even_when_later_deleted(
    tmp_path, encoding
):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    data = sample_path().encode("utf-8" if encoding == "binary" else encoding)
    if encoding == "binary":
        data = b"\xff\x00\x81" + data + b"\x00\xfe"
    (repo / "reviewed.txt").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "reviewed intermediate")
    (repo / "reviewed.txt").unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-m", "delete intermediate")
    config = policy(tmp_path, data)
    assert (
        report(repo, config, base_ref=base)["checks"]["personal_path_scan"]["status"]
        == "pass"
    )
    assert (
        report(
            repo,
            policy(tmp_path, data, content_sha256=digest(data + b" ")),
            base_ref=base,
        )["checks"]["personal_path_scan"]["status"]
        == "fail"
    )


@pytest.mark.parametrize("kind", ["untracked", "working", "commit"])
def test_target_diff_approval_is_per_file_and_all_path_matches(tmp_path, kind):
    repo = make_repo(tmp_path)
    if kind == "working":
        (repo / "reviewed.txt").write_text("baseline")
        (repo / "other.txt").write_text("baseline")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "baseline files")
    base = set_remote_base(repo)
    data = sample_path().encode()
    (repo / "reviewed.txt").write_bytes(data)
    config = policy(tmp_path, data)
    if kind == "commit":
        git(repo, "add", ".")
        git(repo, "commit", "-m", "approved path")
    assert (
        report(repo, config, base_ref=base)["checks"]["personal_path_scan"]["status"]
        == "pass"
    )
    (repo / "other.txt").write_bytes(data)
    if kind == "commit":
        git(repo, "add", ".")
        git(repo, "commit", "-m", "unreviewed copy")
    assert (
        report(repo, config, base_ref=base)["checks"]["personal_path_scan"]["status"]
        == "fail"
    )


def test_target_diff_new_unapproved_match_in_same_bound_file_is_not_suppressed(
    tmp_path,
):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    data = sample_path().encode() + b" /ho" + b"me/new-owner"
    config = policy(tmp_path, data)
    payload = json.loads(config.read_text())
    payload["entries"] = payload["entries"][:1]
    config.write_text(json.dumps(payload))
    (repo / "reviewed.txt").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "two paths one approved")
    assert (
        report(repo, config, base_ref=base)["checks"]["personal_path_scan"]["status"]
        == "fail"
    )


def test_cmo3_xml_uses_whole_file_binding_and_unreadable_stays_unknown(tmp_path):
    repo = make_repo(tmp_path)
    xml = cmo3_xml(sample_path() + "/avatar.psd")
    data = make_cmo3(xml)
    (repo / "model.cmo3").write_bytes(data)
    config = policy(tmp_path, data, path="model.cmo3", scanned_data=xml)
    assert report(repo, config)["checks"]["personal_path_scan"]["status"] == "pass"
    (repo / "model.cmo3").write_bytes(b"broken")
    check = report(repo, config)["checks"]["personal_path_scan"]
    assert check["status"] == "unknown"
    assert check["unscanned_files"] == ["model.cmo3"]


def test_version1_secret_only_never_calls_visibility_api(tmp_path, private_origin):
    from test_reviewed_secret_exceptions import policy as secret_policy, secret

    repo = make_repo(tmp_path)
    data = secret().encode()
    (repo / "reviewed.txt").write_bytes(data)
    assert (
        report(repo, secret_policy(tmp_path, data))["checks"]["secret_scan"]["status"]
        == "pass"
    )
    assert not private_origin


def test_target_diff_literal_pathspec_cannot_hide_unapproved_magic_filename(tmp_path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    data = sample_path().encode()
    (repo / "reviewed.txt").write_bytes(data)
    (repo / ":!reviewed.txt").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "literal filename")
    reviewed = MODULE.ReviewedSecretExceptions(
        policy(tmp_path, data), "https://github.com/example/repo.git"
    )
    assert (
        MODULE.diff_added_lines_have(
            repo, MODULE.PATH_PATTERNS, base, "HEAD", reviewed_exceptions=reviewed
        )
        is True
    )
    assert report(repo, policy(tmp_path, data), base_ref=base)["status"] == "tool_error"


def test_expiry_more_than_ninety_days_is_rejected(tmp_path):
    repo = make_repo(tmp_path)
    expires = (datetime.now(timezone.utc) + timedelta(days=91)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert (
        report(repo, policy(tmp_path, sample_path().encode(), expires_at=expires))[
            "status"
        ]
        == "tool_error"
    )


def test_target_diff_preserves_unchanged_baseline_path_with_policy(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "baseline.txt").write_text(sample_path() + "/existing\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "existing baseline path")
    base = set_remote_base(repo)
    (repo / "baseline.txt").write_text(
        sample_path() + "/existing\nunrelated addition\n"
    )
    data = ("reviewed new file " + sample_path()).encode()
    (repo / "reviewed.txt").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "unrelated baseline addition and reviewed path")
    assert (
        report(repo, policy(tmp_path, data), base_ref=base)["checks"][
            "personal_path_scan"
        ]["status"]
        == "pass"
    )
    assert (
        report(repo, policy(tmp_path, data))["checks"]["personal_path_scan"]["status"]
        == "fail"
    )


@pytest.mark.parametrize(
    "value,rule",
    [
        ("C:\\Us" + "ers\\synthetic-owner", "windows_user_path"),
        ("/Us" + "ers/synthetic-owner", "macos_user_path"),
        ("/ho" + "me/synthetic-owner", "linux_home_path"),
    ],
)
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_all_path_rules_use_exact_match_digest_and_counts(
    tmp_path, value, rule, encoding
):
    for text in (value, value.replace("/", "%2F").replace("\\", "%5C")):
        data = text.encode(encoding)
        assert MODULE.personal_path_matches(data) == {(rule, digest(value.encode())): 1}
        reviewed = MODULE.ReviewedSecretExceptions(
            policy(tmp_path, data), "https://github.com/example/repo.git"
        )
        assert reviewed.all_paths_reviewed("reviewed.txt", data)
        assert not reviewed.all_paths_reviewed("copy.txt", data)
