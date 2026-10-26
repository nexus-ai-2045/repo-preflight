"""許可名義方式（--public）と形の検査の試験。カナリアも兼ねる。

値はすべて架空。利用者名・ID・鍵は 0 の並びか .invalid にし、型の文字列は分割して組み立てる。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from path_input_fixture import fixture_path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import identity_gate as g  # noqa: E402
import preflight_ratchet as ratchet  # noqa: E402

NEXUS = "nexus_ai <273569186+nexus-ai-2045@users.noreply.github.com>"
FAKE_HOME = fixture_path("linux", "zz0000", "work")
FAKE_MAIL = "zz0000" + "@" + "gmail.com"
FAKE_SESSION = (
    "Claude-" + "Session: " + "https://" + "claude.ai/code/" + "session_" + "0" * 24
)
FAKE_KEY = "gh" + "p_" + "0" * 36
OUTSIDER = "Kasou Hito <kasou@example.invalid>"


def policy(**extra):
    data = {"schema": "repo-preflight.identity-policy/v1", "identities": [NEXUS]}
    data.update(extra)
    return g.parse_public_policy(json.dumps(data))


def test_bundled_policy_is_nexus_and_github_bots_only():
    pol = g.load_public_policy(g.DEFAULT_PUBLIC_POLICY)
    assert NEXUS in pol
    assert all(
        "nexus-ai-2045" in i or "[bot]" in i or i == "GitHub <noreply@github.com>"
        for i in pol.identities
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"identities": []},
        {"identities": ["名前だけ"]},
        {"identities": [NEXUS], "extra": 1},
        {"identities": [NEXUS], "schema": "other/v1"},
        {"identities": [NEXUS], "allow_home_users": ["a/b"]},
        {"identities": [NEXUS], "allow_home_users": ["a b"]},
        {"identities": ["Kasou <" + FAKE_MAIL + ">"]},
    ],
)
def test_policy_rejects_bad_shapes(raw):
    with pytest.raises(ValueError):
        g.parse_public_policy(json.dumps(raw))


@pytest.mark.parametrize(
    "text,kind",
    [
        ("場所 " + FAKE_HOME, "実ホームパスの形"),
        ("連絡 " + FAKE_MAIL, "個人メールの形"),
        ("fix\n\n" + FAKE_SESSION + "\n", "セッション行あり"),
        ("token " + FAKE_KEY, "鍵の形"),
    ],
)
def test_shape_findings_block(text, kind):
    assert kind in g.shape_findings(text)


@pytest.mark.parametrize(
    "text",
    [
        fixture_path("linux", "runner", "work", "x"),
        "a@example.invalid",
        "Claude と相談した",
        "README の更新",
    ],
)
def test_shape_findings_pass(text):
    assert g.shape_findings(text) == []


def test_allow_home_users_is_per_repo():
    pol = policy(allow_home_users=["zz0000"])
    assert g.check_public_text(pol, FAKE_HOME, "本文") == []
    assert g.check_public_text(policy(), FAKE_HOME, "本文")


def test_unlisted_coauthor_blocks_and_values_are_not_echoed():
    issues = g.check_public_text(policy(), "fix\n\nCo-authored-by: " + OUTSIDER, "本文")
    assert issues and not any("kasou" in i.casefold() for i in issues)
    assert (
        g.check_public_text(policy(), "fix\n\nCo-authored-by: " + NEXUS, "本文") == []
    )


def _git(repo, *args, ident=NEXUS):
    name, email = ident[:-1].split(" <")
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME=name,
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME=name,
        GIT_COMMITTER_EMAIL=email,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path, message="初回", ident=NEXUS):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(tmp_path, "init", "-q", "--bare", "remote.git")
    _git(repo, "remote", "add", "origin", str(tmp_path / "remote.git"))
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", message, ident=ident)
    return repo


# カナリア：ダミーのセッション行、ダミーのホームパス、一覧に無い名義は必ず赤。
CANARY = [
    pytest.param("更新\n\n" + FAKE_SESSION, NEXUS, id="セッション行"),
    pytest.param("更新 " + FAKE_HOME, NEXUS, id="ホームパス"),
    pytest.param("更新", OUTSIDER, id="一覧に無い名義"),
]


@pytest.mark.parametrize("message,ident", CANARY)
def test_canary_turns_red(tmp_path, message, ident):
    repo = _repo(tmp_path, message, ident)
    assert g.check_public_range(policy(), str(repo), "HEAD")


def test_canary_control_is_green(tmp_path):
    repo = _repo(tmp_path)
    assert g.check_public_range(policy(), str(repo), "HEAD") == []


def _cli(repo, *args, stdin=""):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "identity_gate.py"),
            "--repo",
            str(repo),
            *args,
        ],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize("message,ident", CANARY)
def test_canary_pre_push_entry_turns_red(tmp_path, message, ident):
    repo = _repo(tmp_path, message, ident)
    head = _git(repo, "rev-parse", "HEAD")
    line = f"refs/heads/main {head} refs/heads/main {'0' * 40}\n"
    proc = _cli(repo, "--public", "--pre-push", "--remote", "origin", stdin=line)
    assert proc.returncode == 1, proc.stderr
    assert '"status": "fail"' in proc.stdout
    assert "zz0000" not in proc.stderr + proc.stdout
    assert "kasou" not in (proc.stderr + proc.stdout).casefold()


def test_pre_push_entry_passes_clean_and_skips_delete(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    ok = _cli(
        repo,
        "--public",
        "--pre-push",
        "--remote",
        "origin",
        stdin=f"refs/heads/main {head} refs/heads/main {'0' * 40}\n",
    )
    assert ok.returncode == 0, ok.stderr
    gone = _cli(
        repo,
        "--public",
        "--pre-push",
        "--remote",
        "origin",
        stdin=f"(delete) {'0' * 40} refs/heads/x {head}\n",
    )
    assert gone.returncode == 0


def test_pr_event_entry_turns_red(tmp_path):
    event = tmp_path / "e.json"
    event.write_text(
        json.dumps({"pull_request": {"title": "t", "body": "本文 " + FAKE_HOME}}),
        encoding="utf-8",
    )
    proc = _cli(tmp_path, "--public", "--pr-event", str(event))
    assert proc.returncode == 1


def test_repo_policy_file_cannot_widen_identities(tmp_path):
    # 敵対的レビュー C2: 名簿はリポに置かない。リポの一覧を置いても赤のまま
    repo = _repo(tmp_path, ident=OUTSIDER)
    assert _cli(repo, "--public", "--range", "HEAD").returncode == 1
    (repo / ".repo-preflight").mkdir()
    (repo / ".repo-preflight" / "identity-policy.json").write_text(
        json.dumps({"identities": [OUTSIDER]}), encoding="utf-8"
    )
    assert _cli(repo, "--public", "--range", "HEAD").returncode == 1


def test_pr_cannot_widen_its_own_policy(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "branch", "base")
    (repo / ".repo-preflight").mkdir()
    (repo / ".repo-preflight" / "identity-policy.json").write_text(
        json.dumps({"identities": [OUTSIDER]}), encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "一覧を広げる")
    chosen = ratchet.resolve_identity_policy(repo, "base", tmp_path)
    assert chosen == ratchet.DEFAULT_IDENTITY_POLICY
    chosen = ratchet.resolve_identity_policy(repo, "HEAD", tmp_path)
    assert chosen == ratchet.DEFAULT_IDENTITY_POLICY
    assert OUTSIDER not in g.load_public_policy(chosen)


def test_unreachable_remote_is_tool_error_not_pass(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    proc = _cli(
        repo,
        "--public",
        "--pre-push",
        "--remote",
        "nowhere",
        stdin=f"refs/heads/main {head} refs/heads/main {'0' * 40}\n",
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


def test_private_mode_rejects_public_only_flags(tmp_path):
    proc = _cli(tmp_path, "--pre-push")
    assert proc.returncode == 2


def test_canary_home_path_in_diff_turns_red(tmp_path):
    """本文がきれいでも、差分に足した実ホームパスの形で赤になる（pre-push 入口でも）。"""
    repo = _repo(tmp_path)
    (repo / "b.txt").write_text(FAKE_HOME + "\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "変更")
    issues = g.check_public_range(policy(), str(repo), "HEAD", diffs=True)
    assert any("差分に実ホームパスの形" in i for i in issues)
    assert not any(FAKE_HOME in i for i in issues)
    head = _git(repo, "rev-parse", "HEAD")
    r = _cli(
        repo,
        "--public",
        "--pre-push",
        "--remote",
        "origin",
        stdin=f"refs/heads/main {head} refs/heads/main {'0' * 40}\n",
    )
    assert r.returncode == 1 and '"status": "fail"' in r.stdout
    assert FAKE_HOME not in r.stdout + r.stderr


GOOD_BODY = "## 変更点\n\n- 説明を直した\n"


@pytest.mark.parametrize(
    "secret",
    [FAKE_SESSION, FAKE_HOME, FAKE_MAIL, FAKE_KEY],
    ids=["セッション行", "ホームパス", "個人メール", "鍵"],
)
def test_pr_body_shapes_turn_red_on_edited(tmp_path, secret):
    """edited の事象でも、本文の形を同じ入口で赤にする。値は出さない。"""
    event = tmp_path / "e.json"
    pr = {"title": "説明を直す", "body": GOOD_BODY + secret}
    event.write_text(
        json.dumps({"action": "edited", "pull_request": pr}), encoding="utf-8"
    )
    proc = _cli(tmp_path, "--public", "--pr-event", str(event))
    assert proc.returncode == 1 and '"status": "fail"' in proc.stdout
    assert secret not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "title,body,ok",
    [
        ("説明を直す", GOOD_BODY, True),
        ("Fix docs", GOOD_BODY, False),
        ("説明を直す", "## Summary\n\n説明\n", False),
        ("説明を直す", "本文だけ\n", False),
        ("説明を直す", GOOD_BODY + "```\n## English only\n```\n", True),
    ],
    ids=["日本語", "英語題名", "英語見出し", "見出しなし", "囲みの中は見ない"],
)
def test_pr_japanese_gate_is_absorbed(title, body, ok):
    assert (g.japanese_findings(title, body) == []) is ok


# Dependabot の PR だけ日本語の検査を外す（人の決定 2026-10-26）。作者はイベントの
# pull_request.user で見る。login・type・id が全部合うときだけ外し、他の検査は外さない。
DEPENDABOT = {"login": "dependabot[bot]", "type": "Bot", "id": 49699333}
BOT_TITLE = "Bump actions/checkout from 4.1.0 to 4.2.0"
BOT_BODY = (
    "Bumps [actions/checkout](https://github.com/actions/checkout) from 4.1.0 to 4.2.0.\n"
    "<details>\n<summary>Release notes</summary>\n"
    "<blockquote>\n<h2>What's Changed</h2>\n</blockquote>\n</details>\n"
)


def _pr_event_file(tmp_path, pr):
    event = tmp_path / "e.json"
    event.write_text(
        json.dumps({"action": "opened", "pull_request": pr}), encoding="utf-8"
    )
    return str(event)


def test_dependabot_pr_is_exempt_from_japanese(tmp_path):
    pr = {"title": BOT_TITLE, "body": BOT_BODY, "user": dict(DEPENDABOT)}
    proc = _cli(tmp_path, "--public", "--pr-event", _pr_event_file(tmp_path, pr))
    assert proc.returncode == 0 and '"status": "pass"' in proc.stdout
    body_with_heading = "## Summary\n\n" + BOT_BODY
    assert g.japanese_findings(BOT_TITLE, body_with_heading, language_exempt=True) == []


@pytest.mark.parametrize(
    "secret",
    [FAKE_SESSION, FAKE_HOME, FAKE_MAIL, FAKE_KEY],
    ids=["セッション行", "ホームパス", "個人メール", "鍵"],
)
def test_dependabot_pr_still_runs_other_checks(tmp_path, secret):
    """日本語だけを外す。名義・実ホームパス・個人メール・鍵形などは Dependabot でも赤。"""
    pr = {"title": BOT_TITLE, "body": BOT_BODY + secret, "user": dict(DEPENDABOT)}
    proc = _cli(tmp_path, "--public", "--pr-event", _pr_event_file(tmp_path, pr))
    assert proc.returncode == 1 and '"status": "fail"' in proc.stdout
    assert secret not in proc.stdout + proc.stderr


def test_dependabot_pr_keeps_markdown_link_checks():
    """Markdown リンクの検査は日本語の検査ではないので残す。"""
    errors = g.japanese_findings(
        "[x](https://example.com) を上げる",
        "## [x](https://example.com)\n",
        language_exempt=True,
    )
    assert errors == ["PR題名にMarkdownリンクがある", "見出しにMarkdownリンクがある"]


@pytest.mark.parametrize(
    "user",
    [
        {"login": "nexus-ai-2045", "type": "User", "id": 273569186},
        {"login": "dependabot[bot]", "type": "User", "id": 49699333},
        {"login": "dependabot[bot]", "type": "Bot", "id": 1},
        {"login": "dependabot[bot]", "type": "Bot", "id": "49699333"},
        {"login": "dependabot[bot]", "type": "Bot"},
        {"login": "dependabot[bot]"},
        {"login": "dependabot", "type": "User", "id": 49699333},
        {"login": "github-actions[bot]", "type": "Bot", "id": 41898282},
        None,
        "dependabot[bot]",
    ],
    ids=[
        "人の作者",
        "loginだけ偽装_type違い",
        "id違い",
        "idが文字列",
        "idなし",
        "loginだけ",
        "botなしlogin",
        "別のボット",
        "作者なし",
        "作者が文字列",
    ],
)
def test_non_dependabot_author_still_needs_japanese(tmp_path, user):
    """人の PR は従来どおり日本語が要る。login を似せただけ・別ボットでは外さない。"""
    pr = {"title": BOT_TITLE, "body": BOT_BODY}
    if user is not None:
        pr["user"] = user
    assert not g.is_dependabot_author(pr)
    proc = _cli(tmp_path, "--public", "--pr-event", _pr_event_file(tmp_path, pr))
    assert proc.returncode == 1 and '"status": "fail"' in proc.stdout


def test_dependabot_words_in_title_or_branch_do_not_exempt(tmp_path):
    """題名や枝名に dependabot とあっても、作者が人なら外さない。"""
    pr = {
        "title": "Bump deps (dependabot)",
        "body": BOT_BODY,
        "head": {"ref": "dependabot/github_actions/actions/checkout-4.2.0"},
        "user": {"login": "nexus-ai-2045", "type": "User", "id": 273569186},
    }
    proc = _cli(tmp_path, "--public", "--pr-event", _pr_event_file(tmp_path, pr))
    assert proc.returncode == 1 and '"status": "fail"' in proc.stdout
