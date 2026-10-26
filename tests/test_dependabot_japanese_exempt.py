"""Dependabot の PR だけ日本語の検査を外す試験（人の決定 2026-10-26）。

作者はイベントの pull_request.user で見る。login・type・id が全部合うときだけ外し、
ほかの検査（名義・実ホームパス・個人メール・鍵の形など）は外さない。
"""

import json

import pytest

from test_public_identity_policy import (
    FAKE_HOME,
    FAKE_KEY,
    FAKE_MAIL,
    FAKE_SESSION,
    _cli,
    g,
)

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
        {"login": "kasou-hito", "type": "User", "id": 1000001},
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
        "user": {"login": "kasou-hito", "type": "User", "id": 1000001},
    }
    proc = _cli(tmp_path, "--public", "--pr-event", _pr_event_file(tmp_path, pr))
    assert proc.returncode == 1 and '"status": "fail"' in proc.stdout
