"""local_identity_guard の読み飛ばしを、許可名義の一覧だけに絞ることを確かめるカナリア。

端末名（ホスト名）は集める。許可名義の一覧の語（dependabot[bot] の bot など）だけを外し、
端末名にだけ出る個人名は止める。global のメールを読み飛ばすのは、一覧に載った
nexus の noreply と許可済みの bot だけ（ほかの users.noreply は止める）。
値はすべて架空（example.invalid など）。
"""

import importlib.util
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_identity_guard.py"

spec = importlib.util.spec_from_file_location("local_identity_guard_allowlist", SCRIPT)
GUARD = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules["local_identity_guard_allowlist"] = GUARD
spec.loader.exec_module(GUARD)

PUBLIC_EMAIL = "273569186+nexus-ai-2045@users.noreply.github.com"
# 名簿にない noreply の形は、試験ファイル自体がゲートに当たらないよう実行時に組む。
NOREPLY_DOMAIN = "@" + "users.noreply.github.com"


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path, monkeypatch):
    # 架空の履歴を作るfixtureは実環境の名義・global hooksを読み込まない。
    # 実repoの設定やhooksPathを変更せず、fixtureの設定だけを使う。
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG", "GIT_AUTHOR", "GIT_COMMITTER")) or key in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        }:
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    template = tmp_path / "empty-template"
    template.mkdir()
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))


def git(repo: Path, *args: str, env=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout


def make_repo(tmp_path: Path, name="nexus_ai", email=PUBLIC_EMAIL) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", name)
    git(repo, "config", "user.email", email)
    (repo / "README.md").write_text("公開用\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


CANARY_EMAIL = "canary.person@example.invalid"
CANARY_ACCOUNT = "canaryacct"
CANARY_FULL_NAME = "Canary Personaru"
CANARY_HOME = "canaryhome"
# ホームのパスは実行時に組み立てる（ファイルにはパスの形で書かない）。
CANARY_HOME_PATH = "/".join(["", "Users", CANARY_HOME, "notes", "memo.md"])


def collect_from_environment(
    monkeypatch, tmp_path, *, host="", email=PUBLIC_EMAIL, account="runner"
):
    """環境から集める経路（sources を渡さない）を、架空の環境で動かす。"""

    def fake_git_config(key, *scope, cwd=None):
        if "--global" not in scope:
            return []
        return {"user.email": [email], "user.name": ["nexus-ai-2045"]}.get(key, [])

    def fake_command_output(argv):
        # 端末名を返し得る外部コマンドも、すべて同じ架空の端末名を返す。
        return host

    monkeypatch.setattr(GUARD, "_git_config", fake_git_config)
    monkeypatch.setattr(GUARD, "_command_output", fake_command_output)
    monkeypatch.setattr(GUARD, "_full_name_from_passwd", lambda: CANARY_FULL_NAME)
    monkeypatch.setattr(GUARD.getpass, "getuser", lambda: account)
    # guard がどの経路で端末名を読んでも、同じ架空の端末名になる。
    monkeypatch.setattr(socket, "gethostname", lambda: host)
    monkeypatch.setattr(platform, "node", lambda: host)
    monkeypatch.setattr(GUARD, "OPTIONAL_PRIVATE_FILE", tmp_path / "absent.txt")
    environ = {
        "USER": account,
        "LOGNAME": account,
        "HOME": str(tmp_path / "Users" / CANARY_HOME),
    }
    return GUARD.collect_identities(
        public_names={"nexus-ai-2045", "nexus_ai"}, environ=environ
    )


def commit_public_change(repo, text, message="update"):
    (repo / "change.md").write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)
    return GUARD._rev_list(repo, ["HEAD~1..HEAD"])


def test_allowed_noreply_global_email_is_not_counted_as_personal(tmp_path, monkeypatch):
    found = collect_from_environment(monkeypatch, tmp_path, email=PUBLIC_EMAIL)
    values = {GUARD._normalize(i.value) for i in found}
    local = PUBLIC_EMAIL.split("@", 1)[0]
    assert GUARD._normalize(PUBLIC_EMAIL) not in values
    assert GUARD._normalize(local) not in values
    repo = make_repo(tmp_path)
    commits = commit_public_change(repo, "公開用の追記\n")
    allowed = GUARD.DEFAULT_ALLOWED_EMAIL
    assert GUARD.check_commits(repo, commits, found, allowed) == []
    assert GUARD.effective_identity_problems(repo, allowed, found) == []


@pytest.mark.parametrize("host", ["", "bot", "dependabot", "nexus-ai-2045"])
def test_permitted_hostname_words_do_not_block_bot_lines(tmp_path, monkeypatch, host):
    # 端末名が許可名義の一覧の語だけのときは、dependabot[bot] の行で止まらない。
    found = collect_from_environment(monkeypatch, tmp_path, host=host)
    repo = make_repo(tmp_path)
    commits = commit_public_change(
        repo, "# Dependabot の PR は作者が dependabot[bot] になる\n"
    )
    assert GUARD.check_commits(repo, commits, found, GUARD.DEFAULT_ALLOWED_EMAIL) == []


@pytest.mark.parametrize(
    "host", ["Canaryperson-MacBook-Pro", "Canarypersons-MacBook-Air", "canaryperson"]
)
def test_personal_hostname_is_still_blocked(tmp_path, monkeypatch, host):
    # P1: 端末名にだけ出る個人名（アカウント名は一般の runner）も止める。
    found = collect_from_environment(monkeypatch, tmp_path, host=host)
    assert any(i.source == "host_name" for i in found)
    repo = make_repo(tmp_path)
    commits = commit_public_change(repo, "メモ: Canaryperson の手元で確かめた\n")
    violations = GUARD.check_commits(repo, commits, found, GUARD.DEFAULT_ALLOWED_EMAIL)
    assert violations
    assert "canary" not in "\n".join(violations).lower()


@pytest.mark.parametrize(
    "email",
    [
        "12345+canarylogin" + NOREPLY_DOMAIN,
        "canarylogin" + NOREPLY_DOMAIN,
        "99+canarylogin[bot]" + NOREPLY_DOMAIN,
    ],
)
def test_unlisted_noreply_global_email_is_collected(tmp_path, monkeypatch, email):
    # GitHub の noreply 全般は読み飛ばさない。一覧に無いログイン名は個人名義として止める。
    found = collect_from_environment(monkeypatch, tmp_path, email=email)
    values = {GUARD._normalize(i.value) for i in found}
    assert GUARD._normalize(email.split("@", 1)[0]) in values
    repo = make_repo(tmp_path)
    commits = commit_public_change(repo, "作業: canarylogin\n")
    assert GUARD.check_commits(repo, commits, found, GUARD.DEFAULT_ALLOWED_EMAIL)


def test_listed_bot_global_email_is_not_collected(tmp_path, monkeypatch):
    email = "49699333+dependabot[bot]@users.noreply.github.com"
    found = collect_from_environment(monkeypatch, tmp_path, email=email)
    values = {GUARD._normalize(i.value) for i in found}
    assert GUARD._normalize(email) not in values
    assert GUARD._normalize(email.split("@", 1)[0]) not in values


def test_unreadable_permitted_list_skips_nothing(tmp_path, monkeypatch):
    # 許可名義の一覧が読めないときは、何も読み飛ばさない（止める側に倒す）。
    monkeypatch.setattr(GUARD, "PERMITTED_IDENTITIES_FILE", tmp_path / "absent.json")
    found = collect_from_environment(monkeypatch, tmp_path, host="bot")
    values = {(i.source, GUARD._normalize(i.value)) for i in found}
    assert ("host_name", "bot") in values
    local = GUARD._normalize(PUBLIC_EMAIL.split("@", 1)[0])
    assert ("git_global_email", local) in values


def test_permitted_list_matches_the_reference_file():
    emails, tokens = GUARD.load_permitted_identities()
    assert GUARD._normalize(PUBLIC_EMAIL) in emails
    assert {"bot", "dependabot"} <= tokens
    assert not any(e.endswith("@example.invalid") for e in emails)


@pytest.mark.parametrize("host", ["", "bot"])
@pytest.mark.parametrize(
    "kind,text,message",
    [
        ("personal_email", f"連絡先: {CANARY_EMAIL}\n", "update"),
        ("full_name", f"作者: {CANARY_FULL_NAME}\n", "update"),
        ("os_account", f"login: {CANARY_ACCOUNT}\n", "update"),
        ("home_path", CANARY_HOME_PATH + "\n", "update"),
        (
            "co_author",
            "公開用の追記\n",
            f"update\n\nCo-authored-by: {CANARY_FULL_NAME} <{CANARY_EMAIL}>\n",
        ),
    ],
)
def test_personal_values_are_still_blocked(
    tmp_path, monkeypatch, host, kind, text, message
):
    found = collect_from_environment(
        monkeypatch, tmp_path, host=host, email=CANARY_EMAIL, account=CANARY_ACCOUNT
    )
    repo = make_repo(tmp_path)
    commits = commit_public_change(repo, text, message)
    violations = GUARD.check_commits(repo, commits, found, GUARD.DEFAULT_ALLOWED_EMAIL)
    assert violations, kind
    joined = "\n".join(violations).lower()
    assert "canary" not in joined


def test_personal_commit_email_is_still_blocked_with_any_hostname(
    tmp_path, monkeypatch
):
    found = collect_from_environment(
        monkeypatch, tmp_path, host="bot", email=CANARY_EMAIL
    )
    repo = make_repo(tmp_path, name="nexus_ai", email=CANARY_EMAIL)
    commits = GUARD._rev_list(repo, ["HEAD"])
    joined = "\n".join(
        GUARD.check_commits(repo, commits, found, GUARD.DEFAULT_ALLOWED_EMAIL)
    )
    assert "noreply" in joined and "canary" not in joined.lower()
