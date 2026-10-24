"""品質判定 H1〜H4・H6・H7・M1・M4〜M6 のカナリア（identity_gate の公開の入口）。

値はすべて仮の値で、実行時に組み立てる（このファイル自身が検査に掛からないように）。
IDENTITY_GATE_SCRIPT / BASE_GUARD_SCRIPT で何もしない版に差し替えると落ちることを確かめられる。
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = os.environ.get("IDENTITY_GATE_SCRIPT") or str(ROOT / "scripts/identity_gate.py")
GUARD = os.environ.get("BASE_GUARD_SCRIPT") or str(ROOT / "scripts/base_guard.py")
OK = "Fixture Bot <fixture@example.invalid>"
J = "".join
ZERO = "0" * 40

needs_gitleaks = pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="pre-push は固定した gitleaks で動く"
)


def _git(repo, *args, env=None, input=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        input=input,
    ).stdout.strip()


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "schema": "public-commit-identities/v1",
                "identities": [OK],
                "committer_only": ["GitHub <noreply@github.com>"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Fixture Bot",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture Bot",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        HOME=str(Path(os.environ.get("TMPDIR", "/tmp"))),
    )
    env.update(extra)
    return env


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main", env=_env())
    (path / "README.md").write_text("x\n", encoding="utf-8")
    _git(path, "add", "-A", env=_env())
    _git(path, "commit", "-qm", "root", env=_env())
    return path


def _commit(repo, files=None, msg="試験", **env):
    for rel, data in (files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
    _git(repo, "add", "-A", env=_env(**env))
    _git(repo, "commit", "-q", "--allow-empty", "-F", "-", env=_env(**env), input=msg)
    return _git(repo, "rev-parse", "HEAD")


def gate(policy, *args, cwd=None, input=None, env=None):
    return subprocess.run(
        [sys.executable, GATE, "--public", "--allowed-identities", str(policy), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        input=input,
        env=env,
    )


def message(policy, tmp_path, text):
    path = tmp_path / "msg.txt"
    path.write_text(text, encoding="utf-8")
    return gate(policy, "--repo", str(tmp_path), "--message-file", str(path))


# ---- H1 セッション行・作業 URL ----
SESSION = J(["Claude", "-Session"])
H1_FORMS = {
    "箇条書き": f"試験\n\n- {SESSION}: zz-0000\n",
    "強調": f"試験\n\n**{SESSION}:** zz-0000\n",
    "空白": "試験\n\n" + J(["Claude", " Session"]) + ": zz-0000\n",
    "全角コロン": f"試験\n\n{SESSION}" + chr(0xFF1A) + " zz-0000\n",
    "行の途中": f"試験 メモ {SESSION}: zz-0000\n",
    "Codex の作業 URL": "試験 https://"
    + J(["chatgpt", ".com/codex/tasks/task_zz0000"])
    + "\n",
    "Cursor の作業 URL": "試験 https://"
    + J(["cursor", ".com/agents/bc-00000000"])
    + "\n",
    "ChatGPT の会話 URL": "試験 https://" + J(["chatgpt", ".com/c/zz-0000"]) + "\n",
    # 実際に出た形（Claude Code のプロジェクトの URL の行）。値は仮
    "Claude Code のプロジェクト URL": "試験\n\nhttps://"
    + J(["claude", ".ai/code/project/zz000000-0000-0000-0000-000000000000"])
    + "\n",
    "Claude Code のプロジェクト URL（符号化）": "試験 "
    + J(["claude", ".ai%2Fcode%2Fproject%2Fzz0000"])
    + "\n",
}


@pytest.mark.parametrize("form", sorted(H1_FORMS))
def test_h1_session_forms_are_red(policy, tmp_path, form):
    result = message(policy, tmp_path, H1_FORMS[form])
    assert result.returncode == 1, form
    assert "セッション行あり" in result.stderr
    assert "zz-0000" not in result.stdout + result.stderr  # 値は出さない


def test_h1_control_plain_text_passes(policy, tmp_path):
    result = message(
        policy, tmp_path, "試験\n\nセッションの話を書くだけ（区切りの記号は無い）\n"
    )
    assert result.returncode == 0, result.stderr


def test_h1_session_line_in_added_file_is_red(policy, repo):
    _commit(repo, {"notes.md": f"- {SESSION}: zz-0000\n"})
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 1
    assert "差分にセッション行あり" in result.stderr


# ---- H2 メール ----
H2_ADDED = {
    "会社メール": J(["taro", "@", "zz-corp.co.jp"]),
    "全角＠": J(["taro", chr(0xFF20), "zz-corp.co.jp"]),
    "伏せ字 [at]": J(["taro", " [at] ", "zz-corp", " (dot) ", "co.jp"]),
    "地域違いの個人": J(["taro", "@", "gmail.co.uk"]),
    "短い個人": J(["taro", "@", "pm.me"]),
}


@pytest.mark.parametrize("form", sorted(H2_ADDED))
def test_h2_unlisted_mail_in_added_lines_is_red(policy, repo, form):
    _commit(repo, {"contact.md": f"連絡先 {H2_ADDED[form]}\n"})
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 1, form
    assert "メールの形" in result.stderr
    assert "zz-corp" not in result.stdout + result.stderr


def test_h2_control_reserved_domain_passes(policy, repo):
    _commit(repo, {"contact.md": "連絡先 " + J(["zz", "@", "example.invalid"]) + "\n"})
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 0, result.stderr


# ---- H3 パスとバイナリ ----
H3_ADDED = {
    "小文字 users": J(["cd /us", "ers/zzuser/work"]),
    "二重の URL 符号化": J(["%252FUs", "ers%252Fzzuser%252Fwork"]),
    "JSON の \\u": J(['{"p": "\\u002fUs', 'ers\\u002fzzuser"}']),
    "JSON の \\/": J(['{"p": "\\/Us', 'ers\\/zzuser\\/x"}']),
    "文字列の連結": J(['p = "/Us" + "', 'ers/zzuser/x"']),
    "WSL の /mnt": J(["/mnt/c/Us", "ers/zzuser/x"]),
    "ドライブ（小文字）": J(["c:\\\\us", "ers\\\\zzuser\\\\x"]),
}


@pytest.mark.parametrize("form", sorted(H3_ADDED))
def test_h3_home_path_forms_are_red(policy, repo, form):
    _commit(repo, {"notes.md": H3_ADDED[form] + "\n"})
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 1, form
    assert "実ホームパスの形" in result.stderr
    assert "zzuser" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["shot.png", "data.bin", "nb.ipynb"])
def test_h3_new_binary_or_opaque_file_is_red(policy, repo, name):
    payload = b"\x89PNG\r\n\x1a\n\x00\x01" if name != "nb.ipynb" else b'{"cells": []}\n'
    _commit(repo, {name: payload})
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 1, name
    assert "読めない形のファイル" in result.stderr
    assert name not in result.stdout + result.stderr  # ファイル名も出さない


def test_h3_control_api_path_passes(policy, repo):
    _commit(repo, {"api.md": "GET https://api.example.invalid/users/42\n"})
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 0, result.stderr


def test_h5_too_large_added_text_is_red_and_fast(policy, repo):
    # 読み切れない大きさの差分は、読まずに人の承認待ちで止める（push の時間の上限を守る）。
    _commit(repo, {"big.txt": ("abcdefghij" * 10 + "\n") * 50_000})
    start = time.perf_counter()
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    took = time.perf_counter() - start
    assert result.returncode == 1
    assert "大きすぎる中身" in result.stderr
    assert took < 10.0, took


def test_h5_control_normal_text_passes(policy, repo):
    _commit(repo, {"small.txt": ("abcdefghij" * 10 + "\n") * 1_000})
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 0, result.stderr


# ---- H6 CI を飛ばす印 ----
@pytest.mark.parametrize(
    "text",
    [
        "試験 [skip ci]\n",
        "試験 [CI SKIP]\n",
        "試験 [skip actions]\n",
        "試験\n\nskip-checks: true\n",
    ],
)
def test_h6_skip_markers_are_red(policy, tmp_path, text):
    result = message(policy, tmp_path, text)
    assert result.returncode == 1, text
    assert "CI を飛ばす指示" in result.stderr


# ---- M1 GitHub の名義は記録者だけ ----
def test_m1_github_as_author_is_red(policy, repo):
    _commit(
        repo,
        msg="試験",
        GIT_AUTHOR_NAME="GitHub",
        GIT_AUTHOR_EMAIL="noreply@github.com",
    )
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 1
    assert "作者が公開許可名義と不一致" in result.stderr


def test_m1_control_github_as_committer_passes(policy, repo):
    _commit(
        repo,
        msg="試験",
        GIT_COMMITTER_NAME="GitHub",
        GIT_COMMITTER_EMAIL="noreply@github.com",
    )
    result = gate(policy, "--repo", str(repo), "--range", "HEAD~1..HEAD")
    assert result.returncode == 0, result.stderr


def test_m1_shipped_policy_has_github_only_as_committer():
    data = json.loads((ROOT / "references/public-commit-identities.json").read_text())
    assert "GitHub <noreply@github.com>" not in data["identities"]
    assert "GitHub <noreply@github.com>" in data["committer_only"]


# ---- M6 署名行 ----
@pytest.mark.parametrize(
    "trailer", ["Assisted-by", "Reviewed-by", "Generated-by", "Helped-by"]
)
def test_m6_any_by_trailer_must_match(policy, tmp_path, trailer):
    result = message(
        policy, tmp_path, f"試験\n\n{trailer}: Zz Tool <zz@example.invalid>\n"
    )
    assert result.returncode == 1, trailer
    assert "作者情報の行が公開許可名義と不一致" in result.stderr


def test_m6_control_listed_trailer_passes(policy, tmp_path):
    result = message(policy, tmp_path, f"試験\n\nReviewed-by: {OK}\n")
    assert result.returncode == 0, result.stderr


# ---- H4・H7 pre-push ----
@pytest.fixture
def pushed(tmp_path, repo):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote), env=_env())
    _git(repo, "remote", "add", "origin", str(remote), env=_env())
    _git(repo, "push", "-q", "origin", "main", env=_env())
    _git(repo, "fetch", "-q", "origin", env=_env())
    return remote


def pre_push(policy, repo, lines, env=None):
    return gate(
        policy,
        "--repo",
        str(repo),
        "--pre-push",
        "--remote",
        "origin",
        cwd=repo,
        input="\n".join(lines) + "\n",
        env=env,
    )


@needs_gitleaks
def test_h7_annotated_tag_tagger_and_message_are_checked(policy, repo, pushed):
    _git(
        repo,
        "tag",
        "-a",
        "v9",
        "-m",
        f"版\n\n{SESSION}: zz-0000",
        env=_env(
            GIT_COMMITTER_NAME="Zz Person", GIT_COMMITTER_EMAIL="zz@example.invalid"
        ),
    )
    tag = _git(repo, "rev-parse", "v9")
    result = pre_push(policy, repo, [f"refs/tags/v9 {tag} refs/tags/v9 {ZERO}"])
    assert result.returncode == 1
    assert "タグの作者が公開許可名義と不一致" in result.stderr
    assert "セッション行あり" in result.stderr


@needs_gitleaks
def test_h7_control_clean_tag_passes(policy, repo, pushed):
    _git(repo, "tag", "-a", "v9", "-m", "版", env=_env())
    tag = _git(repo, "rev-parse", "v9")
    result = pre_push(policy, repo, [f"refs/tags/v9 {tag} refs/tags/v9 {ZERO}"])
    assert result.returncode == 0, result.stderr


@needs_gitleaks
def test_h4_pre_push_runs_gitleaks_on_the_range(policy, repo, pushed):
    base = _git(repo, "rev-parse", "HEAD")
    key = J(["ghp_", "Zz0aB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uV1"])
    head = _commit(repo, {"cfg.txt": f"token = {key}\n"})
    result = pre_push(policy, repo, [f"refs/heads/main {head} refs/heads/main {base}"])
    assert result.returncode == 1
    assert "gitleaks" in result.stderr


def test_h4_unpinned_gitleaks_is_refused(policy, repo, pushed, tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    # Windows の shutil.which は PATHEXT の拡張子が付いた名前しか拾わないので、
    # 拡張子無しの偽物は素通りされ、後ろの本物（固定版）が見つかってしまう。
    # 各 OS で「PATH の先頭で見つかる偽物」になる形で置く。
    if os.name == "nt":
        (fake / "gitleaks.cmd").write_text("@exit /b 0\r\n", encoding="utf-8")
    else:
        (fake / "gitleaks").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake / "gitleaks").chmod(0o755)
    env = _env(PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"a.txt": "a\n"})
    result = pre_push(
        policy, repo, [f"refs/heads/main {head} refs/heads/main {base}"], env=env
    )
    assert result.returncode == 2
    assert "固定した版" in result.stderr


def test_h4_ci_runs_pinned_gitleaks():
    text = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    pins = json.loads((ROOT / "references/gitleaks-pins.json").read_text())
    assert "Run pinned gitleaks" in text
    assert pins["archive_sha256"]["linux_x64"] in text
    assert "sha256sum -c" in text


# ---- M4 呼び出し側のひな形 ----
def test_m4_caller_template_is_current_form():
    import yaml

    text = (ROOT / "templates/github-actions/public-privacy-preflight.yml").read_text(
        encoding="utf-8"
    )
    data = yaml.safe_load(text)
    on = data.get("on", data.get(True))
    assert "edited" in on["pull_request"]["types"]
    jobs = data["jobs"]
    assert jobs["preflight"]["uses"] == (
        "nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate.yml@__REPO_PREFLIGHT_SHA__"
    )
    assert jobs["preflight"]["with"]["preflight-ref"] == "__REPO_PREFLIGHT_SHA__"
    assert jobs["all-green"]["name"] == "all-green"
    assert jobs["all-green"]["if"] == "always()"
    assert "preflight" in jobs["all-green"]["needs"]
    assert ".github/actions/preflight@" not in text


# ---- M5 版のタグから辿れない SHA ----
@pytest.fixture
def tool(tmp_path):
    path = tmp_path / "tool"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main", env=_env())
    _git(path, "commit", "-q", "--allow-empty", "-m", "r1", env=_env())
    released = _git(path, "rev-parse", "HEAD")
    _git(path, "tag", "v1.0.0", env=_env())
    _git(path, "commit", "-q", "--allow-empty", "-m", "unreleased", env=_env())
    unreleased = _git(path, "rev-parse", "HEAD")
    return path, released, unreleased


def _caller(sha):
    return (
        "on: [pull_request]\njobs:\n  gate:\n"
        f"    uses: nexus-ai-2045/repo-preflight/.github/workflows/preflight-gate.yml@{sha}\n"
        f"    with:\n      preflight-ref: {sha}\n"
    )


def _guard(repo, base, head, tool_path):
    return subprocess.run(
        [
            sys.executable,
            GUARD,
            "--repo",
            str(repo),
            "--base-ref",
            base,
            "--head-ref",
            head,
            "--trusted-tool",
            str(tool_path),
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("which", ["unreleased", "unknown"])
def test_m5_unreleased_or_foreign_sha_is_red(repo, tool, which):
    tool_path, released, unreleased = tool
    base = _commit(repo, {".github/workflows/ci.yml": _caller(released)})
    sha = unreleased if which == "unreleased" else "c" * 40
    _git(repo, "checkout", "-q", "-b", "pr", env=_env())
    head = _commit(repo, {"README.md": "y\n"})
    # 呼び出しの SHA だけを PR 側で変える（保護パスの赤と混ざらないよう、比較元にも同じ定義を置く）
    _git(repo, "checkout", "-q", "main", env=_env())
    base = _commit(repo, {".github/workflows/ci.yml": _caller(sha)})
    _git(repo, "checkout", "-q", "pr", env=_env())
    _git(repo, "merge", "-q", "--no-edit", "main", env=_env())
    head = _git(repo, "rev-parse", "HEAD")
    result = _guard(repo, base, head, tool_path)
    assert result.returncode == 1, result.stderr
    assert "版のタグ" in result.stderr


def test_m5_control_released_sha_passes(repo, tool):
    tool_path, released, _ = tool
    base = _commit(repo, {".github/workflows/ci.yml": _caller(released)})
    _git(repo, "checkout", "-q", "-b", "pr", env=_env())
    head = _commit(repo, {"README.md": "y\n"})
    result = _guard(repo, base, head, tool_path)
    assert result.returncode == 0, result.stderr


def test_m5_guard_template_checks_release_tags():
    text = (ROOT / "templates/github-actions/preflight-guard.yml").read_text(
        encoding="utf-8"
    )
    assert "--trusted-tool tool" in text
    assert "fetch-tags: true" in text
    assert 'tag --list "v[0-9]*" --contains "$PREFLIGHT_REF"' in text
