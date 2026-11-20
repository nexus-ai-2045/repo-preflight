"""開発道具が自動で付ける痕跡を禁止しないことの回帰試験（2026-11-20 の所有者の決定）。

セッション行、作業・セッションURL、`Co-Authored-By` の共著者行、「Generated with …」の署名行は通す。
止めるのは、個人メールの共著者行（個人名義）と、個人名・ホームのパス・秘密情報など。
値はすべて架空。
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import identity_gate as g  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "readiness_scan_tool_traces", ROOT / "scripts" / "readiness_scan.py"
)
SCAN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(SCAN)

FAKE_URL = "https://" + "claude.ai/code/" + "session_" + "0" * 24
SESSION_LINE = "Claude-" + "Session: " + FAKE_URL
CLAUDE_COAUTHOR = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
OTHER_URLS = [
    "https://" + "chatgpt.com/codex/tasks/task_0000",
    "https://" + "cursor.com/agents/bc-0000",
    "https://" + "claude.ai/project/0000",
]
PERSONAL_COAUTHOR = "Co-authored-by: Someone <someone@" + "gmail.com>"
IDENTITY = "Test Author <test-author@example.invalid>"

TRACES = [
    pytest.param(SESSION_LINE, id="セッション行"),
    pytest.param(FAKE_URL, id="セッションURL"),
    pytest.param(CLAUDE_COAUTHOR, id="Claudeの共著者行"),
    pytest.param(FOOTER, id="Generated with の署名行"),
    pytest.param("\n".join(OTHER_URLS), id="他の道具の作業URL"),
    pytest.param("\n".join([SESSION_LINE, CLAUDE_COAUTHOR, FOOTER]), id="全部まとめて"),
]


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps({"allow_emails": ["1+nexus-bot@users.noreply.github.com"]}),
    )
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    return g.load_policy()


@pytest.mark.parametrize("trace", TRACES)
def test_非公開policyの本文検査は道具の痕跡を通す(policy, trace):
    assert g.scan_text(policy, "fix\n\n" + trace + "\n", "t") == []


@pytest.mark.parametrize("trace", TRACES)
def test_公開の本文検査は道具の痕跡を通す(trace):
    pol = g.parse_public_policy(
        json.dumps(
            {"schema": "repo-preflight.identity-policy/v1", "identities": [IDENTITY]}
        )
    )
    assert g.check_public_text(pol, "fix\n\n" + trace + "\n", "t") == []
    assert g.shape_findings("fix\n\n" + trace + "\n") == []


@pytest.mark.parametrize("trace", TRACES)
def test_秘密の型は道具の痕跡を数えない(trace):
    data = trace.encode("utf-8")
    assert not SCAN.text_has(SCAN.SECRET_PATTERNS, data)
    assert SCAN.secret_matches(data) == {}


def test_個人メールの共著者行は個人名義として止める(policy):
    out = g.scan_text(policy, "fix\n\n" + PERSONAL_COAUTHOR, "t")
    assert out == ["t: 個人メールの形"]
    assert "someone" not in json.dumps(out)


def test_照合リストの個人メールの共著者行は止める(monkeypatch):
    email = "private-person@corp.example.invalid"
    import hashlib

    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps(
            {"allow_emails": ["a@example.invalid"], "deny_email_sha256s": [digest]}
        ),
    )
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    pol = g.load_policy()
    out = g.scan_text(pol, f"fix\n\nCo-authored-by: Other Name <{email}>", "t")
    assert out == ["t: 共同作者行が個人名義"]
    assert email not in json.dumps(out)


def _pr_event(tmp_path, body, comment=None):
    event = {"pull_request": {"title": "直し", "body": body}}
    if comment is not None:
        event["comment"] = {"body": comment}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    return str(path)


def test_PR本文とコメントの道具の痕跡は通る(tmp_path, policy):
    body = "概要\n\n" + FOOTER + "\n\n" + FAKE_URL
    assert g.main(["--pr-event", _pr_event(tmp_path, body, SESSION_LINE)]) == 0


def test_commit_msgフックの入力で痕跡は通る(tmp_path, policy):
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("直し\n\n" + SESSION_LINE + "\n" + CLAUDE_COAUTHOR + "\n")
    assert g.main(["--message-file", str(message)]) == 0


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


def test_道具の痕跡つきのコミットと差分は全履歴の検査も通る(tmp_path):
    repo, allowed = _make_repo(tmp_path)
    (repo / "memo.md").write_text("記録: " + FAKE_URL + "\n", encoding="utf-8")
    _run_git(repo, "add", "memo.md")
    message = "直し\n\n" + SESSION_LINE + "\n" + CLAUDE_COAUTHOR + "\n" + FOOTER
    _run_git(repo, "commit", "-q", "-m", message)

    report = SCAN.scan(
        repo,
        base_ref="origin/main",
        allowed_identities=allowed,
        expected_identity=IDENTITY,
    )

    assert report["checks"]["public_attribution"]["status"] == "pass"
    assert report["checks"]["commit_identity"]["status"] == "pass"
    assert report["checks"]["secret_scan"]["status"] == "pass"


def test_個人メールの共著者行つきのコミットは公開名義検査で止まる(tmp_path):
    repo, allowed = _make_repo(tmp_path)
    (repo / "note.txt").write_text("直し\n", encoding="utf-8")
    _run_git(repo, "add", "note.txt")
    _run_git(repo, "commit", "-q", "-m", "直し\n\n" + PERSONAL_COAUTHOR)

    report = SCAN.scan(
        repo,
        base_ref="origin/main",
        allowed_identities=allowed,
        expected_identity=IDENTITY,
    )

    assert report["checks"]["public_attribution"]["status"] == "fail"
    assert report["checks"]["commit_identity"]["status"] == "fail"
    assert "someone" not in json.dumps(report, ensure_ascii=False)


def test_gitleaksの設定は道具の痕跡の規則を持たない():
    text = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "[[rules]]" not in text
    assert "session" not in text.casefold()
