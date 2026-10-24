"""pre-commit・commit-msg のひな形（薄い形・fail-closed・時間の上限）のカナリア。

本物の git commit を一時リポで走らせ、止まるべきものが止まり、対照は上限時間内に通ることを見る。
値はすべて仮のもの。GitHub には何も送らない。
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from path_input_fixture import fixture_path

ROOT = Path(__file__).resolve().parents[1]
NAME = "nexus_ai"
MAIL = "273569186+nexus-ai-2045@users.noreply.github.com"
LIMIT = {"commit-msg": 1.0, "pre-commit": 2.0, "pre-push": 60.0}

pytestmark = pytest.mark.skipif(
    os.name == "nt"
    or shutil.which("gitleaks") is None
    or shutil.which("python3") is None,
    reason="ひな形は sh・python3・gitleaks で動く",
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("REPO_PREFLIGHT_HOME", str(ROOT))
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in LIMIT:
        shutil.copy2(ROOT / "templates/hooks" / name, hooks / name)
        (hooks / name).chmod(0o755)
    work = tmp_path / "w"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (
        ("user.name", NAME),
        ("user.email", MAIL),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
    (work / "base").write_text("base\n", encoding="utf-8")
    _git(work, "add", "base")
    _git(work, "commit", "-q", "--no-verify", "-m", "土台")
    return work


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env
    )


def _commit(repo, *args, files=None, env=None):
    for name, text in (files or {"a.txt": "a\n"}).items():
        (repo / name).write_text(text, encoding="utf-8")
        _git(repo, "add", name)
    start = time.perf_counter()
    result = _git(repo, "commit", "-q", *args, env=env)
    return result, time.perf_counter() - start


def _hook_time(repo, hook, *args, stdin=None):
    start = time.perf_counter()
    result = subprocess.run(
        [
            "sh",
            str(Path(_git(repo, "config", "core.hooksPath").stdout.strip()) / hook),
            *args,
        ],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
    )
    return result, time.perf_counter() - start


# 対照 -------------------------------------------------------------------------


def test_対照_普通のcommitは通り各フックが上限内(repo):
    result, _ = _commit(repo, "-m", "対照")
    assert result.returncode == 0, result.stderr
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    pre, t_pre = _hook_time(repo, "pre-commit")
    msg = repo / ".git" / "MSG"
    msg.write_text("対照\n", encoding="utf-8")
    cm, t_cm = _hook_time(repo, "commit-msg", str(msg))
    assert pre.returncode == 0 and cm.returncode == 0
    assert t_pre < LIMIT["pre-commit"], t_pre
    assert t_cm < LIMIT["commit-msg"], t_cm


def test_対照_commit_aの一時索引でも通る(repo):
    (repo / "base").write_text("changed\n", encoding="utf-8")
    result = _git(repo, "commit", "-q", "-a", "-m", "対照")
    assert result.returncode == 0, result.stderr


# commit-msg -------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "連絡先 taro@" "zz-dummy-corp.co.jp",
        "Claude-" "Session: 00000000-0000-0000-0000-000000000000",
        "p=" + fixture_path("linux", "zzzz", "x"),
        "Co-authored-by: unlisted <unlisted@example.invalid>",
    ],
    ids=["会社メール", "セッション行", "ホームパス", "名簿にない共著行"],
)
def test_commit_msgが本文の形で止める(repo, body):
    result, _ = _commit(repo, "-m", "試験", "-m", body)
    assert result.returncode != 0
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"


# pre-commit -------------------------------------------------------------------


def test_pre_commitが名簿にない作者で止める(repo):
    result, _ = _commit(
        repo,
        "-m",
        "試験",
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "unlisted",
            "GIT_AUTHOR_EMAIL": "unlisted@example.invalid",
        },
    )
    assert result.returncode != 0
    assert "作者が公開許可名義と不一致" in result.stderr


def test_pre_commitがステージしたホームパスで止める(repo):
    result, _ = _commit(
        repo,
        "-m",
        "試験",
        files={"d.txt": "p=" + fixture_path("linux", "zzzz", "x") + "\n"},
    )
    assert result.returncode != 0
    assert "ステージした差分に実ホームパスの形" in result.stderr


def test_pre_commitがステージした鍵の形で止める(repo):
    key = (
        "-----BEGIN OPENSSH "
        "PRIVATE KEY-----\n" + "0" * 64 + "\n-----END OPENSSH "
        "PRIVATE KEY-----\n"
    )
    result, _ = _commit(repo, "-m", "試験", files={"e.txt": key})
    assert result.returncode != 0
    assert "鍵の形" in result.stderr


def test_pre_commitが形検査に無い秘密をgitleaksで止める(repo):
    # 仮のトークン（GitHub の個人トークンの形。実在しない値）
    token = "gh" "p_" + "Zq3vT8wLm2Xr9KfB4nYc7HdJ1sGa5PeU0iQo"
    result, _ = _commit(repo, "-m", "試験", files={"g.txt": f"token = {token}\n"})
    assert result.returncode != 0
    assert "gitleaks" in result.stderr


# fail-closed ------------------------------------------------------------------


@pytest.mark.parametrize(
    "home", [None, "/nonexistent-repo-preflight"], ids=["未設定", "場所違い"]
)
def test_共通コマンドが無ければcommitを止める(repo, home):
    env = {k: v for k, v in os.environ.items() if k != "REPO_PREFLIGHT_HOME"}
    if home:
        env["REPO_PREFLIGHT_HOME"] = home
    result, _ = _commit(repo, "-m", "対照", env=env)
    assert result.returncode != 0
    assert "見つからない" in result.stderr


def test_pre_commitはgitleaksが無ければ止める(repo, tmp_path):
    shim = tmp_path / "nogitleaks"
    shim.mkdir()
    for tool in ("git", "python3", "sh"):
        (shim / tool).symlink_to(shutil.which(tool))
    result, _ = _commit(repo, "-m", "対照", env={**os.environ, "PATH": str(shim)})
    assert result.returncode != 0
    assert "gitleaks が無い" in result.stderr


# pre-push の上限（20コミットで60秒） ---------------------------------------------


def test_pre_pushは20コミットを上限内で通す(repo, tmp_path):
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/preflight_ratchet.py"),
            "--repo",
            str(repo),
            "--update-baseline",
        ],
        capture_output=True,
    )
    _git(repo, "add", ".repo-preflight/ratchet-baseline.json")
    _git(repo, "commit", "-q", "--no-verify", "-m", "基準値")
    assert _git(repo, "push", "-q", "--no-verify", "origin", "main").returncode == 0
    for i in range(20):
        (repo / f"f{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(repo, "add", f"f{i}.txt")
        assert (
            _git(repo, "commit", "-q", "--no-verify", "-m", f"対照{i}").returncode == 0
        )
    start = time.perf_counter()
    result = _git(repo, "push", "-q", "origin", "main")
    took = time.perf_counter() - start
    assert result.returncode == 0, result.stderr
    assert took < LIMIT["pre-push"], took
