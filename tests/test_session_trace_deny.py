"""開発道具のセッション行・セッションURLを止める回帰試験。

値はすべて架空。本物に似せないよう、IDは 0 だけの並びにする。
型の文字列はリポジトリ自身の検査で一致しないよう、分割して組み立てる。
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import identity_gate as g  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "readiness_scan_session_trace", ROOT / "scripts" / "readiness_scan.py"
)
SCAN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(SCAN)

# 架空の値（本物のセッションIDではない）
FAKE_URL = "https://" + "claude.ai/code/" + "session_" + "0" * 24
FAKE_LINE = "Claude-" + "Session: " + FAKE_URL
LINE_ONLY = "Claude-" + "Session: " + "0" * 8

POL = {
    "allow_emails": ["1+nexus-bot@users.noreply.github.com"],
    "deny_patterns": ["kasou-kojin"],
}
IDENTITY = "Test Author <test-author@example.invalid>"

BLOCK = [
    pytest.param("fix\n\n" + FAKE_LINE + "\n", id="本文末尾の行"),
    pytest.param("fix\n\n  " + LINE_ONLY.lower() + "\n", id="小文字と字下げ"),
    pytest.param(
        "fix\n\n" + LINE_ONLY.replace(": ", " :\t") + "\n", id="コロン前後の空白"
    ),
    pytest.param("見てください " + FAKE_URL, id="文中のURL"),
    pytest.param("参照: " + quote(FAKE_URL, safe=""), id="URL符号化"),
    pytest.param("[作業記録](" + FAKE_URL.upper() + ")", id="大文字のURL"),
]
# 品質判定 H1: 行頭でなくても、新しく足すものでは止める（全履歴のラチェットの型は広げない）
GATE_ONLY_BLOCK = [
    # レビュー3C 高7: 「:」の後ろが ID の形のときだけ止める（英語の文や「は書かない」は通す）
    pytest.param("".join(["note: Claude-", "Session: zz-0000 は書かない"]), id="行頭でない"),
]
PASS = [
    pytest.param("Claude Code で作業しました", id="道具名だけ"),
    pytest.param("Claude-" + "Session 行を消す手順", id="コロンなし"),
    pytest.param("https://" + "claude.ai/code", id="セッションでないURL"),
    pytest.param("session_" + "0" * 24, id="URLでないsession"),
]


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_PRIVATE_PATTERNS", json.dumps(POL))
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    return g.load_policy()


@pytest.mark.parametrize("text", BLOCK + GATE_ONLY_BLOCK)
def test_本文検査はセッション行とURLを止める(policy, text):
    out = g.scan_text(policy, text, "t")
    assert out == ["t: セッション行あり"]
    assert "0" * 24 not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("text", PASS)
def test_本文検査は似ているだけの文を通す(policy, text):
    assert g.scan_text(policy, text, "t") == []


@pytest.mark.parametrize("text", BLOCK)
def test_秘密型もセッション行とURLを数える(text):
    data = text.encode("utf-8")
    assert SCAN.text_has(SCAN.SECRET_PATTERNS, data)
    assert {rule for rule, _ in SCAN.secret_matches(data)} == {"claude_session"}


@pytest.mark.parametrize("text", PASS)
def test_秘密型は似ているだけの文を数えない(text):
    data = text.encode("utf-8")
    assert not SCAN.text_has(SCAN.SECRET_PATTERNS, data)
    assert SCAN.secret_matches(data) == {}


def test_UTF16の写しも数える():
    data = FAKE_URL.encode("utf-16")
    assert SCAN.text_has(SCAN.SECRET_PATTERNS, data)


def test_二つの検査器の型がそろっている():
    # 全履歴のラチェット（readiness_scan）は基準値と数を比べるので型を広げない。
    # 新しく足すものを見る identity_gate は、その型を必ず含む（狭くはならない）。
    for case in BLOCK + PASS:
        text = case.values[0]
        if SCAN.text_has(SCAN.SECRET_PATTERNS, text.encode("utf-8")):
            assert g.session_trace_found(text)
    for case in PASS:
        assert not g.session_trace_found(case.values[0])


def _pr_event(tmp_path, body, comment=None):
    event = {"pull_request": {"title": "直し", "body": body}}
    if comment is not None:
        event["comment"] = {"body": comment}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    return str(path)


def test_PR本文のセッションURLで止まる(tmp_path, policy, capsys):
    code = g.main(["--pr-event", _pr_event(tmp_path, "概要\n\n" + FAKE_URL)])
    captured = capsys.readouterr()
    assert code == 1
    assert "PR本文: セッション行あり" in captured.err
    assert "0" * 24 not in captured.out + captured.err


def test_PRコメントのセッション行で止まる(tmp_path, policy, capsys):
    code = g.main(["--pr-event", _pr_event(tmp_path, "概要", FAKE_LINE)])
    assert code == 1
    assert "コメント: セッション行あり" in capsys.readouterr().err


def test_きれいなPR本文は通る(tmp_path, policy, capsys):
    assert g.main(["--pr-event", _pr_event(tmp_path, "概要\n\n直しました")]) == 0


def test_commit_msgフックの入力で止まる(tmp_path, policy, capsys):
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("直し\n\n" + FAKE_LINE + "\n", encoding="utf-8")
    assert g.main(["--message-file", str(message)]) == 1
    assert "コミット文: セッション行あり" in capsys.readouterr().err


def _run_git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    empty = tmp_path / "empty-excludes"
    empty.write_text("", encoding="utf-8")
    _run_git(repo, "config", "core.excludesFile", str(empty))
    _run_git(repo, "config", "user.name", "Test Author")
    _run_git(repo, "config", "user.email", "test-author@example.invalid")
    for name in SCAN.REQUIRED:
        body = f"# {name}\n"
        if name == SCAN.REVIEW_RECORD:
            body += SCAN.REVIEW_RECORD_MARKER + "\n"
        (repo / name).write_text(body, encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-q", "-m", "初期")
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _run_git(repo, "update-ref", "refs/remotes/origin/main", oid)
    allowed = tmp_path / "allowed.json"
    allowed.write_text(json.dumps({"identities": [IDENTITY]}), encoding="utf-8")
    return repo, allowed


def test_公開名義検査はコミット本文のセッション行で止まる(tmp_path):
    repo, allowed = _make_repo(tmp_path)
    (repo / "note.txt").write_text("直し\n", encoding="utf-8")
    _run_git(repo, "add", "note.txt")
    _run_git(repo, "commit", "-q", "-m", "直し\n\n" + FAKE_LINE)

    report = SCAN.scan(repo, base_ref="origin/main", allowed_identities=allowed)

    assert report["checks"]["public_attribution"]["status"] == "fail"
    assert report["checks"]["public_attribution"]["issue_count"] == 1
    assert "0" * 24 not in json.dumps(report, ensure_ascii=False)


def test_差分に足したセッションURLを秘密検査で止める(tmp_path):
    repo, allowed = _make_repo(tmp_path)
    (repo / "memo.md").write_text("記録: " + FAKE_URL + "\n", encoding="utf-8")
    _run_git(repo, "add", "memo.md")
    _run_git(repo, "commit", "-q", "-m", "メモ")

    report = SCAN.scan(repo, base_ref="origin/main", allowed_identities=allowed)

    assert report["checks"]["public_attribution"]["status"] == "pass"
    assert report["checks"]["secret_scan"]["status"] == "fail"
    assert report["status"] == "blocked"
    assert "0" * 24 not in json.dumps(report, ensure_ascii=False)


def test_セッション行のないコミットと差分は通る(tmp_path):
    repo, allowed = _make_repo(tmp_path)
    (repo / "memo.md").write_text("Claude Code で直した\n", encoding="utf-8")
    _run_git(repo, "add", "memo.md")
    _run_git(repo, "commit", "-q", "-m", "直し\n\nClaude Code で作業")

    report = SCAN.scan(repo, base_ref="origin/main", allowed_identities=allowed)

    assert report["checks"]["public_attribution"]["status"] == "pass"
    assert report["checks"]["secret_scan"]["status"] == "pass"
