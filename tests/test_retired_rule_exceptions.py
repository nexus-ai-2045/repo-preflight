"""廃止した規則 ID（claude_session）のレビュー済み例外は、読み捨てて読み込みを壊さない。"""

import json

import pytest
from test_readiness_scan import MODULE, make_repo
from test_reviewed_secret_exceptions import (  # noqa: F401
    digest,
    policy,
    private_origin,
    report,
    secret,
)


def _entry(rule):
    return {
        "id": "retired_entry",
        "repo": "example/repo",
        "path": "memo.txt",
        "content_sha256": digest(b"x"),
        "rule": rule,
        "match_sha256": digest(b"y"),
        "occurrence_count": 1,
        "review_reason": "廃止した規則の昔の例外",
    }


def _write(tmp_path, entries):
    target = tmp_path / "exceptions.json"
    target.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    return target


def test_廃止した規則のIDの一覧():
    assert MODULE.RETIRED_RULE_IDS == {"claude_session"}
    assert not set(MODULE.RETIRED_RULE_IDS) & set(MODULE.SECRET_RULE_IDS)


def test_廃止した規則の例外だけのファイルも読めて例外は出ない(tmp_path):
    repo = make_repo(tmp_path)
    check = report(repo, _write(tmp_path, [_entry("claude_session")]))
    assert check["status"] != "tool_error"
    assert check["checks"]["secret_scan"]["status"] == "pass"
    assert not check["checks"]["secret_scan"].get("reviewed_exceptions")


def test_廃止した規則の例外があっても生きている例外は効く(tmp_path):
    repo = make_repo(tmp_path)
    data = secret().encode()
    (repo / "reviewed.txt").write_bytes(data)
    from test_readiness_scan import git

    git(repo, "add", ".")
    git(repo, "commit", "-m", "historical")
    live = json.loads(policy(tmp_path, data).read_text(encoding="utf-8"))["entries"]
    config = _write(tmp_path, [_entry("claude_session")] + live)
    check = report(repo, config)["checks"]["secret_scan"]
    assert check["status"] == "pass"
    assert check["reviewed_exceptions"] == [
        {"id": "historical_slug", "occurrence_count": 2}
    ]


@pytest.mark.parametrize("rule", ["no_such_rule", "claude-session"])
def test_未知の規則IDは従来どおり読めない(tmp_path, rule):
    repo = make_repo(tmp_path)
    result = report(repo, _write(tmp_path, [_entry(rule)]))
    assert result["status"] == "tool_error"
