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
from identity_test_inputs import fixture_email

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
CLAUDE_NOREPLY = fixture_email("noreply", "anthropic.com")
CLAUDE_COAUTHOR = f"Co-Authored-By: Claude Sonnet 5.5 <{CLAUDE_NOREPLY}>"
FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
OTHER_URLS = [
    "https://" + "chatgpt.com/codex/tasks/task_0000",
    "https://" + "cursor.com/agents/bc-0000",
    "https://" + "claude.ai/project/0000",
]
PERSONAL_MAIL = fixture_email("someone", "gmail.com")
PERSONAL_COAUTHOR = f"Co-authored-by: Someone <{PERSONAL_MAIL}>"
IDENTITY = "Test Author <test-author@example.invalid>"

ALLOWED_TOOL_TRACES = [
    pytest.param(SESSION_LINE, id="セッション行"),
    pytest.param(FAKE_URL, id="セッションURL"),
    pytest.param(CLAUDE_COAUTHOR, id="Claudeの共著者行"),
    pytest.param(FOOTER, id="Generated with の署名行"),
    pytest.param("\n".join(OTHER_URLS), id="他の道具の作業URL"),
    pytest.param("\n".join([SESSION_LINE, CLAUDE_COAUTHOR, FOOTER]), id="全部まとめて"),
    pytest.param(
        f"Co-authored-by: Cursor Agent <{fixture_email('cursoragent', 'cursor.com')}>",
        id="Cursorの明示許可アドレス",
    ),
    pytest.param(
        f"Co-authored-by: bot <{fixture_email('1+bot', 'users.noreply.github.com')}>",
        id="GitHubのnoreply",
    ),
    pytest.param(
        f"Co-authored-by: dependabot[bot] <{fixture_email('49699333+dependabot[bot]', 'users.noreply.github.com')}>",
        id="GitHubのbotの角括弧つきnoreply",
    ),
    pytest.param(
        f"Co-authored-by: Bot <{fixture_email('no-reply', 'tool.example.org')}>",
        id="no-reply形式",
    ),
]


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps(
            {"allow_emails": [fixture_email("1+nexus-bot", "users.noreply.github.com")]}
        ),
    )
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    return g.load_policy()


@pytest.mark.parametrize("trace", ALLOWED_TOOL_TRACES)
def test_非公開policyの本文検査は道具の痕跡を通す(policy, trace):
    assert g.scan_text(policy, "fix\n\n" + trace + "\n", "t") == []


@pytest.mark.parametrize("trace", ALLOWED_TOOL_TRACES)
def test_公開の本文検査は道具の痕跡を通す(trace):
    pol = g.parse_public_policy(
        json.dumps(
            {"schema": "repo-preflight.identity-policy/v1", "identities": [IDENTITY]}
        )
    )
    assert g.check_public_text(pol, "fix\n\n" + trace + "\n", "t") == []
    assert g.shape_findings("fix\n\n" + trace + "\n") == []


@pytest.mark.parametrize("trace", ALLOWED_TOOL_TRACES)
def test_秘密の型は道具の痕跡を数えない(trace):
    data = trace.encode("utf-8")
    assert not SCAN.text_has(SCAN.SECRET_PATTERNS, data)
    assert SCAN.secret_matches(data) == {}


def test_個人メールの共著者行は個人名義として止める(policy):
    out = g.scan_text(policy, "fix\n\n" + PERSONAL_COAUTHOR, "t")
    assert out == ["t: 個人メールの形"]
    assert "someone" not in json.dumps(out)


def test_照合リストの個人メールの共著者行は止める(monkeypatch):
    email = fixture_email("private-person", "corp.example.invalid")
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


# ---- 共著者のメールは、名簿・道具のnoreply・道具の明示一覧・予約ドメインだけ通す ----
UNLISTED_COMPANY_MAIL = fixture_email("real.name", "bigcorp.co.jp")
UNLISTED_COMPANY_COAUTHOR = f"Co-authored-by: Real Name <{UNLISTED_COMPANY_MAIL}>"
BAD_SHAPE = ["Co-authored-by: Real Name <realname>", "Co-authored-by: Real Name <>"]
MASKED = f"Co-authored-by: REDACTED <{fixture_email('a', 'example.invalid')}>"
ROSTER_MAIL = f"Roster Person <{fixture_email('roster', 'bigcorp.co.jp')}>"


def _public_policy(*identities):
    return g.parse_public_policy(
        json.dumps(
            {
                "schema": "repo-preflight.identity-policy/v1",
                "identities": [IDENTITY, *identities],
            }
        )
    )


def test_名簿外の会社メールの共著者行は公開検査で止まる():
    out = g.check_public_text(
        _public_policy(), "fix\n\n" + UNLISTED_COMPANY_COAUTHOR, "t"
    )
    assert out == ["t: 名簿にないメールの形"]
    assert "bigcorp" not in json.dumps(out)


def test_名簿にある会社メールの共著者行は通る():
    pol = _public_policy(ROSTER_MAIL)
    assert g.check_public_text(pol, "fix\n\nCo-authored-by: " + ROSTER_MAIL, "t") == []


def test_名簿外の会社メールの共著者行は非公開policyの検査で止まる(policy):
    out = g.scan_text(policy, "fix\n\n" + UNLISTED_COMPANY_COAUTHOR, "t")
    assert out == ["t: 名簿にないメールの形"]
    assert "bigcorp" not in json.dumps(out)


def test_許可した会社メールの共著者行は非公開policyの検査で通る(monkeypatch):
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps({"allow_emails": [UNLISTED_COMPANY_MAIL]}),
    )
    monkeypatch.delenv("PREFLIGHT_PRIVATE_PATTERNS_FILE", raising=False)
    message = "fix\n\n" + UNLISTED_COMPANY_COAUTHOR
    assert g.scan_text(g.load_policy(), message, "t") == []


@pytest.mark.parametrize("line", BAD_SHAPE)
def test_メールの形でない共著者行は三つの経路で止まる(policy, line):
    text = "fix\n\n" + line
    assert g.scan_text(policy, text, "t") == ["t: 共同作者行の形が不正"]
    assert g.check_public_text(_public_policy(), text, "t") == [
        "t: 共同作者行の形が不正"
    ]
    assert SCAN._coauthor_line_blocked(line.split(": ", 1)[1])


def test_伏せ字の共著者行は専用の文言で止まる(policy):
    assert g.scan_text(policy, "fix\n\n" + MASKED, "t") == ["t: 共同作者行が伏せ字"]
    assert g.check_public_text(_public_policy(), "fix\n\n" + MASKED, "t") == [
        "t: 共同作者行が伏せ字"
    ]
    assert SCAN._coauthor_line_blocked(MASKED.split(": ", 1)[1])


def test_道具のnoreplyと明示一覧は名簿外でも通る():
    for email in (
        CLAUDE_NOREPLY,
        fixture_email("No-Reply", "tool.example.org"),
        fixture_email("12+bot", "users.noreply.github.com"),
        fixture_email("cursoragent", "cursor.com"),
    ):
        assert not g.coauthor_email_problem(email, set()), email
    assert g.coauthor_email_problem(UNLISTED_COMPANY_MAIL, set()) == "unlisted"
    assert g.coauthor_email_problem(PERSONAL_MAIL, set()) == "personal"
    assert (
        g.coauthor_email_problem(fixture_email("noreply", "gmail.com"), set())
        == "personal"
    )
    assert g.coauthor_email_problem("realname", set()) == "shape"
    assert fixture_email("cursoragent", "cursor.com") in g.TOOL_COAUTHOR_EMAILS


def test_履歴の共著者行は名簿外の会社メールで止まりnoreplyは通る():
    policy_ = _public_policy(ROSTER_MAIL)
    assert SCAN._coauthor_line_blocked(
        UNLISTED_COMPANY_COAUTHOR.split(": ", 1)[1], policy_
    )
    for ok in (
        f"Claude <{CLAUDE_NOREPLY}>",
        f"bot <{fixture_email('1+bot', 'users.noreply.github.com')}>",
        ROSTER_MAIL,
    ):
        assert not SCAN._coauthor_line_blocked(ok, policy_), ok
    assert not SCAN._identity_line_allowed(
        policy_, UNLISTED_COMPANY_COAUTHOR.split(": ", 1)[1]
    )
    assert SCAN._identity_line_allowed(policy_, f"Claude <{CLAUDE_NOREPLY}>")


def test_会社メールの共著者行つきのコミットは全履歴の検査で止まる(tmp_path):
    repo, allowed = _make_repo(tmp_path)
    (repo / "note.txt").write_text("直し\n", encoding="utf-8")
    _run_git(repo, "add", "note.txt")
    _run_git(repo, "commit", "-q", "-m", "直し\n\n" + UNLISTED_COMPANY_COAUTHOR)

    report = SCAN.scan(
        repo,
        base_ref="origin/main",
        allowed_identities=allowed,
        expected_identity=IDENTITY,
    )

    assert report["checks"]["public_attribution"]["status"] == "fail"
    assert report["checks"]["commit_identity"]["status"] == "fail"
    assert "bigcorp" not in json.dumps(report, ensure_ascii=False)
