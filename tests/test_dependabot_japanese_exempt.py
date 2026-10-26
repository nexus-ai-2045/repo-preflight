"""Dependabot の PR だけ本文の日本語の検査を外す試験（人の決定 2026-10-26、範囲を狭めた版）。

外すのは本文の日本語の2点だけ。題名の日本語・Markdown リンク・名義・実ホームパス・
個人メール・鍵の形などは外さない。作者・sender・action・比較範囲のコミットが
全部 Dependabot のときだけ外し、分からないときは外さない（fail-closed）。
"""

import json
import os
import subprocess

import pytest

from test_public_identity_policy import (
    FAKE_HOME,
    FAKE_KEY,
    FAKE_MAIL,
    FAKE_SESSION,
    NEXUS,
    _cli,
    g,
)

DEPENDABOT = {"login": "dependabot[bot]", "type": "Bot", "id": 49699333}
HUMAN = {"login": "kasou-hito", "type": "User", "id": 1000001}
BOT_AUTHOR = g.DEPENDABOT_COMMIT_AUTHOR
WEB_COMMITTER = next(
    c for c in g.DEPENDABOT_COMMIT_COMMITTERS if c != g.DEPENDABOT_COMMIT_AUTHOR
)
NEXUS_ID = tuple(NEXUS[:-1].split(" <"))
JA_TITLE = "依存を上げる: actions/checkout を 4.2.0 へ"
EN_TITLE = "Bump actions/checkout from 4.1.0 to 4.2.0"
BOT_BODY = (
    "## Summary\n\n"
    "Bumps actions/checkout from 4.1.0 to 4.2.0.\n"
    "<details>\n<summary>Release notes</summary>\n"
    "<blockquote>\n<h2>What's Changed</h2>\n</blockquote>\n</details>\n"
)
RANGE = "main..HEAD"
AUTO = object()


def _git(repo, *args, author, committer):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME=author[0],
        GIT_AUTHOR_EMAIL=author[1],
        GIT_COMMITTER_NAME=committer[0],
        GIT_COMMITTER_EMAIL=committer[1],
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    subprocess.run(
        ["git", "-C", str(repo), *args], env=env, check=True, capture_output=True
    )


def _commit(repo, name, author, committer):
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name, author=author, committer=committer)
    _git(repo, "commit", "-q", "-m", "Bump " + name, author=author, committer=committer)


def _rev(repo, spec):
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", spec + "^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def bot_repo(tmp_path):
    """main の上に Dependabot の作ったコミットが1つある枝。"""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", author=NEXUS_ID, committer=NEXUS_ID)
    _commit(repo, "a.txt", NEXUS_ID, NEXUS_ID)
    _git(repo, "checkout", "-q", "-b", "dep", author=NEXUS_ID, committer=NEXUS_ID)
    _commit(repo, "b.txt", BOT_AUTHOR, WEB_COMMITTER)
    return repo


def _event(
    tmp_path,
    *,
    title=JA_TITLE,
    body=BOT_BODY,
    user=DEPENDABOT,
    sender=DEPENDABOT,
    action="opened",
    commits=1,
    head_sha=AUTO,
):
    """head_sha は既定で tmp_path/r の HEAD（枝先）。None なら head を入れない。"""
    pr = {"title": title, "body": body, "commits": commits}
    if head_sha is AUTO:
        head_sha = _rev(tmp_path / "r", "HEAD")
    if head_sha is not None:
        pr["head"] = {"sha": head_sha, "ref": "dependabot/github_actions/x"}
    if user is not None:
        pr["user"] = user
    ev = {"action": action, "pull_request": pr}
    if sender is not None:
        ev["sender"] = sender
    path = tmp_path / "e.json"
    path.write_text(json.dumps(ev), encoding="utf-8")
    return str(path)


def _run(repo, event, rng=RANGE):
    args = ["--public", "--pr-event", event]
    if rng:
        args += ["--range", rng]
    return _cli(repo, *args)


def _assert_body_japanese_red(proc):
    assert proc.returncode == 1 and '"status": "fail"' in proc.stdout
    assert "PR本文に日本語の見出しがない" in proc.stderr


def test_dependabot_only_pr_is_exempt_from_body_japanese(tmp_path, bot_repo):
    proc = _run(bot_repo, _event(tmp_path))
    assert proc.returncode == 0 and '"status": "pass"' in proc.stdout, proc.stderr
    assert g.japanese_findings(JA_TITLE, BOT_BODY, body_language_exempt=True) == []


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "edited"])
def test_dependabot_own_events_stay_exempt(tmp_path, bot_repo, action):
    assert _run(bot_repo, _event(tmp_path, action=action)).returncode == 0


def test_english_title_is_red_even_for_dependabot(tmp_path, bot_repo):
    """題名の日本語は外さない（題名は A2 の接頭辞などで日本語にする）。"""
    proc = _run(bot_repo, _event(tmp_path, title=EN_TITLE))
    assert proc.returncode == 1 and "PR題名に日本語がない" in proc.stderr
    assert "PR本文に日本語の見出しがない" not in proc.stderr


@pytest.mark.parametrize(
    "who",
    [NEXUS_ID, ("Kasou Hito", "kasou@example.invalid")],
    ids=["係の名義", "人の名義"],
)
@pytest.mark.parametrize("sender", [DEPENDABOT, HUMAN], ids=["sender_bot", "sender_人"])
def test_added_commit_by_someone_else_is_red(tmp_path, bot_repo, who, sender):
    """人や係がコミットを足したら外れる（sender が Dependabot のままでも）。"""
    _commit(bot_repo, "c.txt", who, who)
    proc = _run(
        bot_repo, _event(tmp_path, sender=sender, action="synchronize", commits=2)
    )
    _assert_body_japanese_red(proc)


def test_web_edit_commit_by_human_is_red(tmp_path, bot_repo):
    """Web 画面での編集（作者が人・コミッターが GitHub）も外れる。"""
    _commit(bot_repo, "c.txt", NEXUS_ID, WEB_COMMITTER)
    _assert_body_japanese_red(_run(bot_repo, _event(tmp_path, commits=2)))


@pytest.mark.parametrize("action", ["edited", "synchronize", "reopened", "opened"])
def test_human_sender_is_red(tmp_path, bot_repo, action):
    """人や係が題名・本文を編集した（sender が人）ときは外さない。"""
    _assert_body_japanese_red(
        _run(bot_repo, _event(tmp_path, sender=HUMAN, action=action))
    )


@pytest.mark.parametrize(
    "kwargs,rng",
    [
        ({"sender": None}, RANGE),
        ({"action": "labeled"}, RANGE),
        ({"action": None}, RANGE),
        ({"commits": 2}, RANGE),
        ({"commits": 0}, RANGE),
        ({"commits": "1"}, RANGE),
        ({"commits": True}, RANGE),
        ({}, None),
        ({}, "HEAD"),
    ],
    ids=[
        "senderなし",
        "action違い",
        "actionなし",
        "コミット数違い",
        "コミット数0",
        "コミット数が文字列",
        "コミット数がTrue",
        "rangeなし",
        "range片側",
    ],
)
def test_unknown_event_info_is_fail_closed(tmp_path, bot_repo, kwargs, rng):
    """今のイベントの情報だけで決められないときは外さない。"""
    _assert_body_japanese_red(_run(bot_repo, _event(tmp_path, **kwargs), rng))


@pytest.mark.parametrize(
    "secret",
    [FAKE_SESSION, FAKE_HOME, FAKE_MAIL, FAKE_KEY],
    ids=["セッション行", "ホームパス", "個人メール", "鍵"],
)
def test_dependabot_pr_still_runs_other_checks(tmp_path, bot_repo, secret):
    """本文の日本語だけを外す。個人情報の4種は Dependabot でも赤。"""
    proc = _run(bot_repo, _event(tmp_path, body=BOT_BODY + secret))
    assert proc.returncode == 1 and '"status": "fail"' in proc.stdout
    assert secret not in proc.stdout + proc.stderr


def test_dependabot_pr_keeps_markdown_link_checks():
    """Markdown リンクの検査は日本語の検査ではないので残す。"""
    errors = g.japanese_findings(
        "[x](https://example.com) を上げる",
        "## [x](https://example.com)\n",
        body_language_exempt=True,
    )
    assert errors == ["PR題名にMarkdownリンクがある", "見出しにMarkdownリンクがある"]


SPOOFED = pytest.mark.parametrize(
    "user",
    [
        HUMAN,
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


@SPOOFED
def test_non_dependabot_author_still_needs_japanese(tmp_path, bot_repo, user):
    """作者を似せただけでは外さない（sender とコミットが本物でも）。"""
    assert not g.is_dependabot_author({"user": user})
    _assert_body_japanese_red(_run(bot_repo, _event(tmp_path, user=user)))


@SPOOFED
def test_spoofed_sender_is_red(tmp_path, bot_repo, user):
    """sender を似せただけでも外さない（作者とコミットが本物でも）。"""
    if user is None:
        user = {}
    _assert_body_japanese_red(_run(bot_repo, _event(tmp_path, sender=user)))


def test_dependabot_words_in_title_or_branch_do_not_exempt(tmp_path, bot_repo):
    """題名や枝名に dependabot とあっても、作者が人なら外さない。"""
    path = _event(tmp_path, title="依存を上げる (dependabot)", user=HUMAN, sender=HUMAN)
    ev = json.loads(open(path, encoding="utf-8").read())
    ev["pull_request"]["head"][
        "ref"
    ] = "dependabot/github_actions/actions/checkout-4.2.0"
    open(path, "w", encoding="utf-8").write(json.dumps(ev))
    _assert_body_japanese_red(_run(bot_repo, path))


# --- CI の仮マージ（github.sha）を HEAD にした場合（品質係の指摘 2026-10-26）---
# pull_request の CI は「base の先端と枝先の仮マージ」を checkout する。範囲は
# 基準..pull_request.head.sha で数え、HEAD の第2親が head.sha であることを確かめる。


def _test_merge(repo, *, advance_main=True, first="main", second="dep"):
    """GitHub の仮マージを真似て、切り離した HEAD にマージコミットを作る。"""
    if advance_main:
        _git(repo, "checkout", "-q", "main", author=NEXUS_ID, committer=NEXUS_ID)
        _commit(repo, "m.txt", NEXUS_ID, NEXUS_ID)
    _git(repo, "checkout", "-q", "--detach", first, author=NEXUS_ID, committer=NEXUS_ID)
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge into main",
        second,
        author=BOT_AUTHOR,
        committer=WEB_COMMITTER,
    )


@pytest.mark.parametrize("advance_main", [True, False], ids=["main前進", "main同じ"])
def test_ci_merge_head_is_exempt(tmp_path, bot_repo, advance_main):
    """仮マージが HEAD でも、第2親が head.sha なら本物の Dependabot は外れる。"""
    head = _rev(bot_repo, "dep")
    _test_merge(bot_repo, advance_main=advance_main)
    assert _rev(bot_repo, "HEAD^2") == head
    proc = _run(bot_repo, _event(tmp_path, head_sha=head))
    assert proc.returncode == 0 and '"status": "pass"' in proc.stdout, proc.stderr


def test_ci_merge_head_with_old_base_is_exempt(tmp_path, bot_repo):
    """基準が main の進む前の SHA（イベントの base.sha が古い）でも数は合う。"""
    old_base = _rev(bot_repo, "main")
    head = _rev(bot_repo, "dep")
    _test_merge(bot_repo)
    proc = _run(bot_repo, _event(tmp_path, head_sha=head), f"{old_base}..HEAD")
    assert proc.returncode == 0, proc.stderr


def test_ci_merge_head_still_checks_title(tmp_path, bot_repo):
    head = _rev(bot_repo, "dep")
    _test_merge(bot_repo)
    proc = _run(bot_repo, _event(tmp_path, title=EN_TITLE, head_sha=head))
    assert proc.returncode == 1 and "PR題名に日本語がない" in proc.stderr
    assert "PR本文に日本語の見出しがない" not in proc.stderr


def test_ci_merge_head_with_human_commit_is_red(tmp_path, bot_repo):
    """枝に人のコミットがあれば、仮マージでも外れる。"""
    _git(bot_repo, "checkout", "-q", "dep", author=NEXUS_ID, committer=NEXUS_ID)
    _commit(bot_repo, "c.txt", NEXUS_ID, NEXUS_ID)
    head = _rev(bot_repo, "dep")
    _test_merge(bot_repo)
    _assert_body_japanese_red(
        _run(bot_repo, _event(tmp_path, commits=2, head_sha=head))
    )


def test_ci_merge_head_with_reversed_parents_is_red(tmp_path, bot_repo):
    """head.sha が第1親（枝先に main を取り込んだ形）なら仮マージとみなさない。"""
    head = _rev(bot_repo, "dep")
    _git(bot_repo, "checkout", "-q", "main", author=NEXUS_ID, committer=NEXUS_ID)
    _commit(bot_repo, "m.txt", NEXUS_ID, NEXUS_ID)
    _test_merge(bot_repo, advance_main=False, first="dep", second="main")
    assert _rev(bot_repo, "HEAD^1") == head
    _assert_body_japanese_red(_run(bot_repo, _event(tmp_path, head_sha=head)))


def test_commit_beyond_head_sha_is_red(tmp_path, bot_repo):
    """HEAD が head.sha の先の別コミット（マージでない）なら外さない。"""
    head = _rev(bot_repo, "dep")
    _commit(bot_repo, "c.txt", NEXUS_ID, NEXUS_ID)
    _assert_body_japanese_red(_run(bot_repo, _event(tmp_path, head_sha=head)))


@pytest.mark.parametrize(
    "head_sha",
    [None, "", "0" * 40, "main", 12345, "SHORT"],
    ids=["headなし", "空", "存在しないSHA", "SHAでない名前", "数値", "短い"],
)
def test_bad_head_sha_on_ci_merge_is_red(tmp_path, bot_repo, head_sha):
    if head_sha == "SHORT":
        head_sha = _rev(bot_repo, "dep")[:12]
    _test_merge(bot_repo)
    _assert_body_japanese_red(_run(bot_repo, _event(tmp_path, head_sha=head_sha)))


def test_head_sha_of_other_branch_is_red(tmp_path, bot_repo):
    """head.sha が main の SHA（第2親と違う）なら外さない。"""
    _test_merge(bot_repo)
    other = _rev(bot_repo, "main")
    _assert_body_japanese_red(_run(bot_repo, _event(tmp_path, head_sha=other)))


@pytest.mark.parametrize("rng", ["main...HEAD", "..HEAD", "main..", "main..-HEAD"])
def test_odd_range_shapes_are_not_exempt(tmp_path, bot_repo, rng):
    """三点・片側・オプションに見える範囲では外さない（赤か tool_error）。"""
    proc = _run(bot_repo, _event(tmp_path), rng)
    assert proc.returncode in (1, 2) and '"status": "pass"' not in proc.stdout


def test_missing_base_is_tool_error(tmp_path, bot_repo):
    """存在しない基準の範囲は従来どおり rc=2。"""
    head = _rev(bot_repo, "dep")
    _test_merge(bot_repo)
    proc = _run(bot_repo, _event(tmp_path, head_sha=head), "0" * 40 + "..HEAD")
    assert proc.returncode == 2 and "tool_error" in proc.stdout
