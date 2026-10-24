"""敵対的レビュー3C（重大1・高1〜高8）のカナリアと対照。

値はすべて仮の値（alice・example・0000）で、実行時に組み立てる（このファイル自身が検査に掛からないように）。
IDENTITY_GATE_SCRIPT で直す前の版や何もしない版に差し替えると、カナリアが落ちることを確かめられる。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = os.environ.get("IDENTITY_GATE_SCRIPT") or str(ROOT / "scripts/identity_gate.py")
OK = "Fixture Bot <fixture@example.invalid>"
J = "".join
U = J(["Us", "ers"])
H = J(["ho", "me"])
AT = "@"
GM = J(["gm", "ail.com"])
ZERO = "0" * 40

needs_gitleaks = pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="pre-push は固定した gitleaks で動く"
)


@pytest.fixture(scope="module")
def policy(tmp_path_factory):
    path = tmp_path_factory.mktemp("pol") / "policy.json"
    path.write_text(
        json.dumps({"schema": "public-commit-identities/v1", "identities": [OK]}),
        encoding="utf-8",
    )
    return path


def gate(policy, *args, cwd=None, input=None):
    return subprocess.run(
        [sys.executable, GATE, "--public", "--allowed-identities", str(policy), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        input=input,
        env=_env(),
    )


def message(policy, tmp_path, text):
    path = tmp_path / "msg.txt"
    path.write_text("試験\n\n" + text + "\n", encoding="utf-8")
    return gate(policy, "--repo", str(tmp_path), "--message-file", str(path))


# ---- 止めるべきもの（カナリア） ----
RED = {
    # 重大1 日本語の直後のパス
    "重大1 かなの直後 mac": f"パスは/{U}/alice/projです",
    "重大1 全角コロンの直後 linux": f"場所：/{H}/alice/x",
    "重大1 かぎ括弧": f"「/{U}/alice/x」",
    "重大1 項目番号 1./": f"1./{U}/alice/x",
    "重大1 下線の直後 _/": f"_/{U}/alice",
    # 高2 Claude のプロジェクト URL
    "高2 ai の project": "https://" + J(["claude", ".ai/project/00000000-0000"]),
    "高2 ai の chat": "https://" + J(["claude", ".ai/chat/zz-0000"]),
    "高2 com の chat": "https://" + J(["claude", ".com/chat/00000000-0000"]),
    "高2 ai の code projects": "https://" + J(["claude", ".ai/code/projects/0000"]),
    # 高3 符号化したホームと道具のリンク
    "高3 Claude 符号化 -Users-": f"~/.claude/projects/-{U}-alice-proj/",
    "高3 Claude 符号化 C--Users-": f"C--{U}-alice-proj",
    "高3 Claude 符号化 -home-": f"~/.claude/projects/-{H}-alice-proj/",
    "高3 vscode://file": f"vscode://file/{U}/alice/x.py",
    "高3 cursor://file": f"cursor://file/{U}/alice/x.py",
    "高3 file://localhost": f"file://localhost/{U}/alice/x",
    # 高4 WSL・逆斜線
    "高4 UNC wsl$": "\\\\wsl$\\Ubuntu\\" + H + "\\alice\\x",
    "高4 UNC wsl.localhost": "\\\\wsl.localhost\\Ubuntu\\" + H + "\\alice\\x",
    "高4 逆斜線の home": "\\" + H + "\\alice\\x",
    # 高5〜高7 の直しで、本物は止まったままであること
    "高5 個人メール": "alice" + AT + GM,
    "高5 日本語の直後の個人メール": "連絡はalice" + AT + GM + "まで",
    "高5 名簿にない会社メール": "alice" + AT + "corp.example-co.jp",
    "高6 鍵の形 sk-": "sk-" + "a0" * 12,
    "高6 鍵の形 sk-proj-": "sk-proj-" + "0" * 40,
    "高7 セッション行": J(["Claude", "-Session"]) + ": 00000000-0000",
    "高7 セッション行 強調": "- **" + J(["Claude", "-Session"]) + "**: zz-0000",
    "高7 /users/ の実名": f"/{U.lower()}/alice/x",
    "高8 例示の記号の後ろの実名": f"/{H}/alice/<x>",
}

# ---- 通すべきもの（誤検出の対照） ----
GREEN = {
    "対照 README の文": "このツールは手元で動きます。",
    "高2 ai のトップ": "https://" + J(["claude", ".ai/"]),
    "高2 ai の download": "https://" + J(["claude", ".ai/download"]),
    "高5 git clone の ssh": "git clone git" + AT + "github.com:nexus-ai-2045/repo.git",
    "高5 uses の版": "uses: actions/checkout" + AT + "v4.1.0",
    "高5 npm の版": "npm install lodash" + AT + "4.17.21",
    "高5 pip の版": "pip install pkg" + AT + "1.2.0",
    "高5 行列の積": "y = x" + AT + "W.T",
    "高5 mDNS の .local": "ssh pi" + AT + "raspberrypi.local",
    "高5 画像の倍率": "icon" + AT + "2x.png",
    "高6 risk-": "risk-assessment-documentation-guidelines",
    "高6 desk-": "desk-management-and-operations-handbook",
    "高6 SPDX": "LicenseRef-scancode-generic-" + "risk-linking-protocols-exception",
    "高7 REST {id}": f"GET /{U.lower()}/{{id}}",
    "高7 REST :id": f"`/{U.lower()}/:id` を返す",
    "高7 REST GET の経路": f"GET /{H}/feed",
    "高7 英語の文 Claude": "Start a Claude " + "session: run the CLI.",
    "高7 英語の文 Copilot": "Copilot " + "session: tips",
    "高7 一般語の Session": "Session: 会議のメモ",
    "高8 <user>": f"/{H}/<user>/project",
    "高8 <username>": f"C:\\{U}\\<username>\\x",
    "高8 $USER": f"/{U}/$USER/x",
    "高8 ${{USER}}": f"/{H}/${{USER}}/x",
    "対照 runner": f"/{H}/runner/work",
}


@pytest.mark.parametrize("name", sorted(RED))
def test_review_3c_canary_is_red(policy, tmp_path, name):
    result = message(policy, tmp_path, RED[name])
    assert result.returncode == 1, (name, result.stderr)


@pytest.mark.parametrize("name", sorted(GREEN))
def test_review_3c_control_is_green(policy, tmp_path, name):
    result = message(policy, tmp_path, GREEN[name])
    assert result.returncode == 0, (name, result.stderr)


# ---- 高1 ファイル名・改名（pre-push と pre-commit） ----
def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Fixture Bot",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture Bot",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        HOME=str(Path(os.environ.get("TMPDIR", "/tmp"))),
    )
    return env


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_env(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "init", "-q", "-b", "main")
    (work / "d").mkdir()
    (work / "d" / "keep.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "root")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")
    return work


def _add(repo, rel):
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ok\n", encoding="utf-8")
    _git(repo, "add", "-A")


def _rename(repo, rel):
    _git(repo, "mv", "d/keep.txt", rel)


NAMES = {
    "メールのファイル名": lambda r: _add(r, "alice" + AT + GM + ".txt"),
    "符号化ホームのフォルダ名": lambda r: _add(r, f"C--{U}-alice-proj/a.txt"),
    "セッション名のファイル": lambda r: _add(
        r, J(["claude", ".ai_code_session_0000.txt"])
    ),
    "git mv での改名": lambda r: _rename(r, "d/alice" + AT + GM + ".txt"),
}


def _pre_push(policy, repo):
    _git(repo, "commit", "-qm", "試験")
    head = _git(repo, "rev-parse", "HEAD")
    base = _git(repo, "rev-parse", "origin/main")
    return gate(
        policy,
        "--repo",
        str(repo),
        "--pre-push",
        "--remote",
        "origin",
        cwd=repo,
        input=f"refs/heads/main {head} refs/heads/main {base}\n",
    )


@needs_gitleaks
@pytest.mark.parametrize("name", sorted(NAMES))
def test_h1_path_names_are_red_on_pre_push(policy, repo, name):
    NAMES[name](repo)
    result = _pre_push(policy, repo)
    assert result.returncode == 1, (name, result.stderr)
    assert "ファイル名に" in result.stderr


@needs_gitleaks
@pytest.mark.parametrize("name", sorted(NAMES))
def test_h1_path_names_are_red_on_pre_commit(policy, repo, name):
    NAMES[name](repo)
    result = gate(policy, "--repo", str(repo), "--staged", cwd=repo)
    assert result.returncode == 1, (name, result.stderr)
    assert "ファイル名に" in result.stderr


@needs_gitleaks
def test_h1_plain_file_names_are_green(policy, repo):
    _add(repo, "docs/assets/icon" + AT + "2x.png.txt")
    _rename(repo, "d/renamed.txt")
    assert gate(policy, "--repo", str(repo), "--staged", cwd=repo).returncode == 0
    result = _pre_push(policy, repo)
    assert result.returncode == 0, result.stderr


def test_gitleaks_rules_follow_session_shapes():
    """`.gitleaks.toml` の作業 URL とセッション行も同じ形にそろえる（高2・高7）。"""
    text = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "(?:ai|com)" in text
    assert "projects?|chat|share|code" in text
    assert (
        "[0-9]"
        in text.split('id = "claude-session-trailer"', 1)[1].split("[[rules]]", 1)[0]
    )
