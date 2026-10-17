import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_identity_guard.py"

spec = importlib.util.spec_from_file_location("local_identity_guard", SCRIPT)
GUARD = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules["local_identity_guard"] = GUARD
spec.loader.exec_module(GUARD)

# 架空の個人名義。実在の値はテストにも書かない。
FAKE_SOURCES = {
    "os_account": ["fakeperson"],
    "full_name": ["Fakeo Personaru", "Fakeo", "Personaru"],
    "git_global_email": ["fake.person@example.invalid", "fake.person"],
    "host_name": ["Fakeo", "nexus"],
}
PUBLIC_EMAIL = "273569186+nexus-ai-2045@users.noreply.github.com"


def identities():
    return GUARD.collect_identities(
        sources=FAKE_SOURCES, public_names={"nexus-ai-2045", "nexus_ai"}
    )


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


def test_collect_skips_generic_public_and_short_values():
    values = {i.value for i in identities()}
    assert "fakeperson" in values and "Fakeo Personaru" in values
    assert "nexus" not in values  # 公開名義の一部は個人名義ではない
    extra = GUARD.collect_identities(
        sources={
            "os_account": ["root", "ab", "runner", PUBLIC_EMAIL],
            "git_global_email": ["noreply@example.invalid", "noreply"],
        }
    )
    assert extra == []


def test_find_hits_respects_word_boundaries_and_case():
    ids = identities()
    assert {i.value for i in GUARD.find_hits("by FAKEPERSON today", ids)} == {
        "fakeperson"
    }
    assert GUARD.find_hits("fakepersonality", ids) == []
    assert GUARD.find_hits("ｆａｋｅｐｅｒｓｏｎ", ids)  # 全角も正規化して当てる


def test_label_and_report_never_contain_value(capsys):
    ids = identities()
    labels = [i.label for i in ids]
    assert all("fake" not in label.lower() for label in labels)
    GUARD._report([f"x: 個人名義 {ids[0].label}"])
    err = capsys.readouterr().err
    assert "fakeperson" not in err.lower()


def test_redact_replaces_values():
    out = GUARD.redact("denylist パターン「Fakeo Personaru」/fakeperson", identities())
    assert "fake" not in out.lower()
    assert GUARD.REDACTED in out


def test_clean_public_commit_passes(tmp_path):
    repo = make_repo(tmp_path)
    commits = GUARD._rev_list(repo, ["HEAD"])
    assert (
        GUARD.check_commits(repo, commits, identities(), GUARD.DEFAULT_ALLOWED_EMAIL)
        == []
    )
    assert (
        GUARD.effective_identity_problems(
            repo, GUARD.DEFAULT_ALLOWED_EMAIL, identities()
        )
        == []
    )


def test_personal_commit_identity_is_blocked(tmp_path):
    repo = make_repo(
        tmp_path, name="Fakeo Personaru", email="fake.person@example.invalid"
    )
    commits = GUARD._rev_list(repo, ["HEAD"])
    violations = GUARD.check_commits(
        repo, commits, identities(), GUARD.DEFAULT_ALLOWED_EMAIL
    )
    joined = "\n".join(violations)
    assert "author" in joined and "committer" in joined and "noreply" in joined
    assert "fake" not in joined.lower()
    assert GUARD.effective_identity_problems(
        repo, GUARD.DEFAULT_ALLOWED_EMAIL, identities()
    )


def test_content_message_and_path_are_blocked(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "fakeperson-notes.md").write_text(
        "作者: Fakeo Personaru\n", encoding="utf-8"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fakeperson のメモ")
    commits = GUARD._rev_list(repo, ["HEAD~1..HEAD"])
    violations = GUARD.check_commits(
        repo, commits, identities(), GUARD.DEFAULT_ALLOWED_EMAIL
    )
    joined = "\n".join(violations)
    assert (
        "メッセージ" in joined
        and "ファイル名" in joined
        and "追加されています" in joined
    )
    assert "fake" not in joined.lower()


def test_push_ranges_from_stdin(tmp_path):
    repo = make_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD").strip()
    old = "1" * 40
    text = f"refs/heads/a {head} refs/heads/a {old}\nrefs/heads/b {GUARD.ZERO_SHA} refs/heads/b {old}\n"
    ranges = GUARD.push_ranges_from_stdin(repo, "origin", text)
    assert ranges == [[f"{old}..{head}"]]


def test_new_branch_url_excludes_existing_origin_history(tmp_path):
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD").strip()
    url = "https://github.com/example/example.git"
    git(repo, "remote", "add", "origin", url)
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    (repo / "new.md").write_text("新しい変更\n")
    git(repo, "add", "new.md")
    git(repo, "commit", "-qm", "Add a document")
    head = git(repo, "rev-parse", "HEAD").strip()
    text = f"refs/heads/new {head} refs/heads/new {GUARD.ZERO_SHA}\n"
    ranges = GUARD.push_ranges_from_stdin(repo, url, text)
    assert GUARD._rev_list(repo, ranges[0]) == [head]


def test_new_branch_unknown_url_stops_without_guessing(tmp_path):
    repo = make_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD").strip()
    text = f"refs/heads/new {head} refs/heads/new {GUARD.ZERO_SHA}\n"
    with pytest.raises(RuntimeError):
        GUARD.push_ranges_from_stdin(repo, "https://github.com/example/unknown.git", text)


def test_exec_passes_values_by_env_and_redacts_output(monkeypatch, capsys):
    fake = identities()
    monkeypatch.setattr(GUARD, "collect_identities", lambda **_: fake)
    code = GUARD.main(
        [
            "exec",
            "--",
            sys.executable,
            "-c",
            "import os,sys; v=os.environ['PUBLIC_IDENTITY_DENYLIST_EXTRA'];"
            "print('hit:', v.splitlines()[0]); sys.exit(3)",
        ]
    )
    assert code == 3
    err = capsys.readouterr().err
    assert "hit:" in err and GUARD.REDACTED in err
    assert "fake" not in err.lower()


def test_repo_history_personal_identity_is_collected(tmp_path):
    repo = make_repo(tmp_path)
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Oldo Namae",
        GIT_AUTHOR_EMAIL="old.private@example.invalid",
        GIT_COMMITTER_NAME="Claude",
        GIT_COMMITTER_EMAIL="noreply@anthropic.com",
    )
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "old", env=env)
    values = GUARD._history_personal_values(repo, GUARD.DEFAULT_ALLOWED_EMAIL)
    assert "old.private@example.invalid" in values and "Oldo Namae" in values
    assert "Claude" not in values and "nexus_ai" not in values
