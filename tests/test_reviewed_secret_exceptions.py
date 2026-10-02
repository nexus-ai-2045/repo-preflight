"""限定例外と通常secret検出の境界を実Gitで検証する。"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_readiness_scan import MODULE, SCRIPT, git, make_repo, set_remote_base


def digest(value):
    return hashlib.sha256(value).hexdigest()


def secret():
    return "sk-" + "a" * 24


def policy(tmp_path, data, path="reviewed.txt", **changes):
    text = secret()
    entry = {
        "id": "historical_slug",
        "repo": "example/repo",
        "path": path,
        "content_sha256": digest(data),
        "rule": "openai_key",
        "match_sha256": digest(text.encode()),
        "occurrence_count": 1,
        "review_reason": "独立レビューで旧識別子と確認した",
    }
    entry.update(changes)
    target = tmp_path / "exceptions.json"
    target.write_text(json.dumps({"version": 1, "entries": [entry]}), encoding="utf-8")
    return target


def report(repo, config=None, **kwargs):
    if config is not None:
        kwargs["reviewed_secret_exceptions"] = config
    return MODULE.scan(repo, **kwargs)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_exact_reviewed_match_is_removed_in_worktree_and_deleted_history(
    tmp_path, encoding
):
    repo = make_repo(tmp_path)
    data = secret().encode(encoding)
    path = repo / "reviewed.txt"
    path.write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "historical")
    config = policy(tmp_path, data)
    check = report(repo, config)["checks"]["secret_scan"]
    assert check["status"] == "pass"
    assert check["reviewed_exceptions"] == [
        {"id": "historical_slug", "occurrence_count": 2}
    ]
    path.unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-m", "remove")
    assert report(repo)["checks"]["secret_scan"]["status"] == "fail"
    assert report(repo, config)["checks"]["secret_scan"]["status"] == "pass"


@pytest.mark.parametrize("kind", ["normal", "embedded", "url", "mixed"])
def test_real_keys_stay_detected_and_redacted(tmp_path, kind):
    repo = make_repo(tmp_path)
    text = secret()
    encoded = text.replace("-", "%2D")
    value = {
        "normal": text,
        # openai_key は前側の区切りを要求するので、区切り文字の後に埋め込む。
        "embedded": "x=" + text,
        "url": encoded,
        "mixed": text + " " + encoded,
    }[kind]
    data = value.encode()
    (repo / "reviewed.txt").write_bytes(data)
    config = policy(tmp_path, b"different")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "candidate")
    check = report(repo, config)["checks"]["secret_scan"]
    assert check["status"] == "fail"
    assert check["finding_count"] == 2
    assert text not in json.dumps(check)
    counts = MODULE.secret_matches(data)
    assert counts[("openai_key", digest(text.encode()))] == (
        2 if kind == "mixed" else 1
    )


@pytest.mark.parametrize("change", ["bytes", "count", "path", "additional_key"])
def test_binding_changes_and_unrelated_key_cannot_pass(tmp_path, change):
    repo = make_repo(tmp_path)
    data = secret().encode()
    config = policy(tmp_path, data)
    path = repo / ("elsewhere.txt" if change == "path" else "reviewed.txt")
    if change == "bytes":
        data += b"\n"
    if change == "count":
        data += b" " + data
    if change == "additional_key":
        data += (" ghp_" + "b" * 24).encode()
        config = policy(tmp_path, data)
    path.write_bytes(data)
    assert report(repo, config)["checks"]["secret_scan"]["status"] == "fail"


def test_identical_blob_at_second_path_is_not_exempted_in_deleted_history(tmp_path):
    repo = make_repo(tmp_path)
    data = secret().encode()
    for name in ("reviewed.txt", "other.txt"):
        (repo / name).write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "two aliases")
    for name in ("reviewed.txt", "other.txt"):
        (repo / name).unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-m", "deleted")
    check = report(repo, policy(tmp_path, data))["checks"]["secret_scan"]
    assert check["status"] == "fail"
    assert check["finding_count"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "duplicate_id",
        "duplicate_binding",
        "duplicate_json",
        "traversal",
        "boolean_count",
        "repo",
        "hash",
        "rule",
        "reason",
        "id",
    ],
)
def test_invalid_policy_is_fail_closed_and_does_not_echo_values(tmp_path, mutation):
    repo = make_repo(tmp_path)
    config = policy(tmp_path, secret().encode())
    payload = json.loads(config.read_text())
    entry = payload["entries"][0]
    if mutation == "unknown":
        entry["unexpected"] = secret()
    elif mutation in {"duplicate_id", "duplicate_binding"}:
        second = dict(entry)
        if mutation == "duplicate_binding":
            second["id"] = "second"
        payload["entries"].append(second)
    elif mutation == "traversal":
        entry["path"] = "../reviewed.txt"
    elif mutation == "boolean_count":
        entry["occurrence_count"] = True
    elif mutation == "repo":
        entry["repo"] = "other/repo"
    elif mutation == "hash":
        entry["content_sha256"] = "invalid"
    elif mutation == "rule":
        entry["rule"] = "unknown"
    elif mutation == "reason":
        entry["review_reason"] = secret()
    elif mutation == "id":
        entry["id"] = secret()
    serialized = json.dumps(payload)
    if mutation == "duplicate_json":
        serialized = serialized.replace('"version": 1', '"version": 1, "version": 1')
    config.write_text(serialized)
    result = report(repo, config)
    assert result == {
        "status": "tool_error",
        "issues": ["reviewed_secret_exceptions_invalid"],
    }
    assert secret() not in json.dumps(result)


def test_no_automatic_policy_loading_and_cli_intent_forwarding(tmp_path):
    repo = make_repo(tmp_path)
    data = secret().encode()
    (repo / "reviewed.txt").write_bytes(data)
    config = policy(tmp_path, data)
    (repo / "reviewed-secret-exceptions.json").write_bytes(config.read_bytes())
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    assert report(repo)["checks"]["secret_scan"]["status"] == "fail"
    for extra in ([], ["--intent", "open_pr"]):
        run = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                str(repo),
                "--reviewed-secret-exceptions",
                str(config),
                *extra,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(run.stdout)
        if extra:
            payload = payload["scan"]
        assert payload["checks"]["secret_scan"]["status"] == "pass"
        assert secret() not in run.stdout + run.stderr


def test_interactive_and_target_diff_preserve_option(tmp_path):
    repo = make_repo(tmp_path)
    base = set_remote_base(repo)
    data = secret().encode()
    (repo / "reviewed.txt").write_bytes(data)
    config = policy(tmp_path, data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "candidate")
    args = MODULE.build_parser().parse_args(
        [
            "--repo",
            str(repo),
            "--interactive",
            "--reviewed-secret-exceptions",
            str(config),
        ]
    )
    answers = iter(["local", "standard", str(repo), "n", "y", "y"])
    options = MODULE.resolve_options(
        args,
        stdin_is_tty=False,
        input_fn=lambda _: next(answers),
        output_fn=lambda _: None,
    )
    assert options.reviewed_secret_exceptions == config
    assert (
        report(repo, config, base_ref=base)["checks"]["secret_scan"]["status"] == "pass"
    )


# openai_key は単語の途中の "sk-" ("task-" など) を拾わないよう前側の区切りを
# 要求するので、埋め込みは区切り文字の後で確かめる。英数字に直結した形を
# 検出しないことは test_readiness_scan.py の境界テストで固定している。
@pytest.mark.parametrize(
    "index,value,embed",
    [
        (0, "sk-" + "c" * 24, "x="),
        (1, "ghp_" + "c" * 24, "x"),
        (2, "github_pat_" + "c" * 24, "x"),
        (3, "AKIA" + "C" * 16, "x"),
        (4, "xoxb-" + "c" * 24, "x"),
        (5, "BEGIN " + "PRIVATE KEY", "x"),
    ],
)
def test_all_existing_rules_keep_raw_embedded_url_and_utf16_detection(
    index, value, embed
):
    for representation in (
        value,
        embed + value,
        "%" + format(ord(value[0]), "02X") + value[1:],
    ):
        for encoding in ("utf-8", "utf-16"):
            counts = MODULE.secret_matches(representation.encode(encoding))
            assert counts[(MODULE.SECRET_RULE_IDS[index], digest(value.encode()))] == 1


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/example/repo.git",
        "git@github.com:example/repo.git",
        "ssh://git@github.com/example/repo.git",
    ],
)
def test_standard_origin_forms_are_bound_to_exact_repository(tmp_path, remote):
    config = policy(tmp_path, secret().encode())
    assert (
        MODULE.ReviewedSecretExceptions(config, remote).has_unreviewed(
            "reviewed.txt", secret().encode()
        )
        is False
    )


def test_policy_size_limit(tmp_path):
    config = tmp_path / "oversized.json"
    config.write_bytes(b" " * 1_000_001)
    with pytest.raises(RuntimeError, match="^reviewed_secret_exceptions_invalid$"):
        MODULE.ReviewedSecretExceptions(config, "https://github.com/example/repo.git")


def test_four_matches_are_bound_without_representation_double_count(tmp_path):
    # 以前は旧識別子 "task-orchestra-..." の部分文字列を使っていたが、openai_key が
    # 単語の途中の "sk-" を拾わなくなったので、区切りの後の合成値で同じ形を作る。
    slug = "sk-" + "h" * 24
    reference = "references/sk-" + "i" * 24
    data = json.dumps([slug, slug, slug, reference]).encode()
    matches = MODULE.secret_matches(data)
    assert sorted(matches.values()) == [1, 3]
    entries = []
    for index, ((rule, match_hash), count) in enumerate(matches.items()):
        entries.append(
            {
                "id": "reviewed_slug_" + str(index),
                "repo": "example/repo",
                "path": "archive.json",
                "content_sha256": digest(data),
                "rule": rule,
                "match_sha256": match_hash,
                "occurrence_count": count,
                "review_reason": "旧識別子を確認済み",
            }
        )
    config = tmp_path / "real-slugs.json"
    config.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    reviewed = MODULE.ReviewedSecretExceptions(
        config, "https://github.com/example/repo.git"
    )
    assert reviewed.has_unreviewed("archive.json", data) is False
    assert sum(item["occurrence_count"] for item in reviewed.report()) == 4
    assert reviewed.has_unreviewed("copied.json", data) is True
    assert reviewed.has_unreviewed("archive.json", data + b" ") is True


@pytest.mark.parametrize("field", ["path", "repo"])
@pytest.mark.parametrize("encoded", [False, True])
def test_credentials_in_path_and_repo_bindings_are_rejected(tmp_path, field, encoded):
    value = secret()
    if encoded:
        value = value.replace("-", "%2D")
    entry_value = "example/" + value if field == "repo" else value + ".txt"
    config = policy(tmp_path, b"fixture", **{field: entry_value})
    remote = (
        "https://github.com/"
        + (entry_value if field == "repo" else "example/repo")
        + ".git"
    )
    with pytest.raises(RuntimeError, match="^reviewed_secret_exceptions_invalid$"):
        MODULE.ReviewedSecretExceptions(config, remote)
