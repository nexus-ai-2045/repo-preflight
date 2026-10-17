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


def new_branch_stdin(repo, ref="topic"):
    head = git(repo, "rev-parse", "HEAD").strip()
    return f"refs/heads/{ref} {head} refs/heads/{ref} {GUARD.ZERO_SHA}\n"


def push_commits(repo, remote):
    return [
        commit
        for spec in GUARD.push_ranges_from_stdin(repo, remote, new_branch_stdin(repo))
        for commit in GUARD._rev_list(repo, spec)
    ]


def commit_file(repo, text="公開用の追記\n", env=None):
    (repo / "change.md").write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "update", env=env)
    return git(repo, "rev-parse", "HEAD").strip()


def published_history(tmp_path):
    repo = make_repo(
        tmp_path, name="Fakeo Personaru", email="fake.person@example.invalid"
    )
    old = git(repo, "rev-parse", "HEAD").strip()
    destination = tmp_path / "destination.git"
    git(tmp_path, "clone", "--bare", str(repo), str(destination))
    git(repo, "config", "user.name", "nexus_ai")
    git(repo, "config", "user.email", PUBLIC_EMAIL)
    return repo, destination, old


@pytest.mark.parametrize("remote_kind", ["path", "url", "alias"])
def test_new_branch_excludes_only_history_on_destination(tmp_path, remote_kind):
    repo, destination, old = published_history(tmp_path)
    head = commit_file(repo)
    remote = str(destination) if remote_kind == "path" else destination.as_uri()
    if remote_kind == "alias":
        git(repo, "remote", "add", "target", remote)
        remote = "target"
    commits = push_commits(repo, remote)
    assert commits == [head]
    assert old not in commits
    assert (
        GUARD.check_commits(repo, commits, identities(), GUARD.DEFAULT_ALLOWED_EMAIL)
        == []
    )


@pytest.mark.parametrize("violation", ["author", "content"])
def test_new_branch_still_blocks_new_personal_values(tmp_path, violation):
    repo, destination, _ = published_history(tmp_path)
    env = None
    text = "公開用\n"
    if violation == "author":
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Fakeo Personaru",
            GIT_AUTHOR_EMAIL="fake.person@example.invalid",
        )
    else:
        text = "作者: Fakeo Personaru\n"
    head = commit_file(repo, text, env)
    commits = push_commits(repo, destination.as_uri())
    assert commits == [head]
    problems = GUARD.check_commits(
        repo, commits, identities(), GUARD.DEFAULT_ALLOWED_EMAIL
    )
    assert problems
    assert "fake" not in "\n".join(problems).lower()


def test_deleted_remote_branch_cannot_exempt_stale_tracking_history(tmp_path):
    repo, destination, old = published_history(tmp_path)
    git(repo, "remote", "add", "target", str(destination))
    git(repo, "fetch", "target")
    branch = git(destination, "symbolic-ref", "HEAD").strip()
    git(destination, "update-ref", "-d", branch)
    head = commit_file(repo)
    assert push_commits(repo, "target") == [old, head]


def test_wrong_destination_cannot_exempt_local_tracking_history(tmp_path):
    repo, destination, old = published_history(tmp_path)
    git(repo, "remote", "add", "target", str(destination))
    git(repo, "fetch", "target")
    other = tmp_path / "empty.git"
    git(tmp_path, "init", "--bare", str(other))
    head = commit_file(repo)
    assert push_commits(repo, other.as_uri()) == [old, head]
    git(repo, "remote", "set-url", "target", str(other))
    assert push_commits(repo, "target") == [old, head]


def test_alias_uses_push_destination_not_fetch_destination(tmp_path):
    repo, destination, old = published_history(tmp_path)
    other = tmp_path / "push.git"
    git(tmp_path, "init", "--bare", str(other))
    git(repo, "remote", "add", "target", str(destination))
    git(repo, "fetch", "target")
    git(repo, "remote", "set-url", "--push", "target", str(other))
    head = commit_file(repo)
    assert push_commits(repo, "target") == [old, head]


@pytest.mark.parametrize("remote_kind", ["url", "alias"])
def test_push_url_rewrite_uses_actual_empty_destination(tmp_path, remote_kind):
    repo, destination, old = published_history(tmp_path)
    other = tmp_path / "actual-push.git"
    git(tmp_path, "init", "--bare", str(other))
    source_url, push_url = destination.as_uri(), other.as_uri()
    git(repo, "config", f"url.{push_url}.pushInsteadOf", source_url)
    remote = source_url
    if remote_kind == "alias":
        git(repo, "remote", "add", "target", source_url)
        remote = "target"
    head = commit_file(repo)
    config_before = (repo / ".git" / "config").read_bytes()
    if remote_kind == "url":
        with pytest.raises(RuntimeError, match="URL直指定とpushInsteadOf"):
            push_commits(repo, remote)
        assert (repo / ".git" / "config").read_bytes() == config_before
        return
    commits = push_commits(repo, remote)
    assert commits == [old, head]
    assert GUARD.check_commits(repo, commits, identities(), GUARD.DEFAULT_ALLOWED_EMAIL)
    assert (repo / ".git" / "config").read_bytes() == config_before


@pytest.mark.parametrize("remote_kind", ["url", "alias"])
def test_second_url_rewrite_cannot_exempt_history_from_other_destination(
    tmp_path, remote_kind
):
    repo, destination, _ = published_history(tmp_path)
    other = tmp_path / "actual-push.git"
    git(tmp_path, "init", "--bare", str(other))
    source_url, push_url = destination.as_uri(), other.as_uri()
    git(repo, "config", f"url.{push_url}.insteadOf", source_url)
    git(repo, "config", f"url.{source_url}.insteadOf", push_url)
    remote = source_url
    if remote_kind == "alias":
        git(repo, "remote", "add", "target", source_url)
        remote = "target"
    commit_file(repo)
    with pytest.raises(RuntimeError, match="送信先の履歴照会先") as failure_info:
        push_commits(repo, remote)
    assert source_url not in str(failure_info.value)
    assert push_url not in str(failure_info.value)


def test_literal_url_with_unrelated_push_rewrite_blocks(tmp_path):
    repo, destination, _ = published_history(tmp_path)
    git(
        repo,
        "config",
        "url.https://example.invalid/target/.pushInsteadOf",
        "unrelated:",
    )
    with pytest.raises(RuntimeError, match="URL直指定とpushInsteadOf"):
        push_commits(repo, destination.as_uri())


def test_push_rewrite_config_read_error_blocks(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    real_run = GUARD.subprocess.run

    def failed_config(argv, **kwargs):
        if "--get-regexp" in argv:
            return subprocess.CompletedProcess(
                argv, 2, stdout="", stderr="private-config-error"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(GUARD.subprocess, "run", failed_config)
    with pytest.raises(
        RuntimeError, match="URL書換え設定を確認できません"
    ) as failure_info:
        push_commits(repo, "destination")
    assert "private-config-error" not in str(failure_info.value)


def test_alias_with_multiple_push_destinations_blocks(tmp_path):
    repo, destination, _ = published_history(tmp_path)
    git(repo, "remote", "add", "target", str(destination))
    git(repo, "remote", "set-url", "--add", "--push", "target", str(destination))
    git(
        repo,
        "remote",
        "set-url",
        "--add",
        "--push",
        "target",
        str(tmp_path / "other.git"),
    )
    with pytest.raises(RuntimeError, match="送信先が一意でない"):
        push_commits(repo, "target")


def test_unknown_advertised_commits_do_not_fail_or_exempt_local_history(tmp_path):
    repo = make_repo(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = make_repo(other_root, name="Other Public", email=PUBLIC_EMAIL)
    destination = tmp_path / "unrelated.git"
    git(tmp_path, "clone", "--bare", str(other), str(destination))
    assert push_commits(repo, destination.as_uri()) == [
        git(repo, "rev-parse", "HEAD").strip()
    ]


@pytest.mark.parametrize("failure", ["malformed", "oserror"])
def test_unreadable_advertisement_blocks(tmp_path, monkeypatch, failure):
    repo = make_repo(tmp_path)
    real_run = GUARD.subprocess.run

    def failed_query(argv, **kwargs):
        if "ls-remote" in argv and "--get-url" not in argv:
            if failure == "oserror":
                raise OSError("private-error")
            return subprocess.CompletedProcess(
                argv, 0, stdout="invalid-advertisement", stderr=""
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(GUARD.subprocess, "run", failed_query)
    with pytest.raises(RuntimeError, match="送信先の履歴") as failure_info:
        push_commits(repo, "destination")
    assert "private-error" not in str(failure_info.value)


def test_destination_failure_blocks_without_leaking_target(
    tmp_path, monkeypatch, capsys
):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(GUARD, "collect_identities", lambda **_: identities())
    monkeypatch.setattr(sys, "stdin", io.StringIO(new_branch_stdin(repo)))
    code = GUARD.main(
        [
            "--repo",
            str(repo),
            "check",
            "--pre-push",
            "--remote",
            str(tmp_path / "sensitive-target.git"),
        ]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "検査できない" in err
    assert "sensitive-target" not in err and str(tmp_path) not in err


def test_destination_query_timeout_blocks_without_leaking_command(
    tmp_path, monkeypatch, capsys
):
    repo = make_repo(tmp_path)
    real_run = GUARD.subprocess.run

    def timeout_query(argv, **kwargs):
        if "ls-remote" in argv and "--get-url" not in argv:
            assert 0 < kwargs["timeout"] <= 30
            raise subprocess.TimeoutExpired(
                ["git", "sensitive-target"], kwargs["timeout"], stderr="private-error"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(GUARD.subprocess, "run", timeout_query)
    monkeypatch.setattr(GUARD, "collect_identities", lambda **_: identities())
    monkeypatch.setattr(sys, "stdin", io.StringIO(new_branch_stdin(repo)))
    assert (
        GUARD.main(
            ["--repo", str(repo), "check", "--pre-push", "--remote", "sensitive-target"]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "検査できない" in err
    assert "sensitive-target" not in err and "private-error" not in err


def test_multiple_new_branches_share_one_remote_advertisement(tmp_path, monkeypatch):
    repo, destination, _ = published_history(tmp_path)
    commit_file(repo)
    calls = []
    real_run = GUARD.subprocess.run

    def count_queries(argv, **kwargs):
        if "ls-remote" in argv and "--get-url" not in argv:
            calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(GUARD.subprocess, "run", count_queries)
    ranges = GUARD.push_ranges_from_stdin(
        repo,
        destination.as_uri(),
        new_branch_stdin(repo, "a") + new_branch_stdin(repo, "b"),
    )
    assert len(ranges) == 2
    assert len(calls) == 1


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


def test_exec_redacts_inherited_denylist_in_both_streams(monkeypatch, capsys):
    inherited = "synthetic-inherited-private-contact"
    fake = identities()
    monkeypatch.setenv(GUARD.EXTRA_DENYLIST_ENV, inherited)
    monkeypatch.setattr(GUARD, "collect_identities", lambda **_: fake)
    code = GUARD.main(
        [
            "exec",
            "--",
            sys.executable,
            "-c",
            "import os,sys; value=os.environ['PUBLIC_IDENTITY_DENYLIST_EXTRA'].splitlines()[0]; "
            "print(value); print(value,file=sys.stderr)",
        ]
    )
    output = capsys.readouterr()
    assert code == 0, output.err
    assert inherited not in output.out + output.err
    assert output.err.count(GUARD.REDACTED) == 2
