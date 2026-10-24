"""品質判定 M2 のカナリア：Mac 標準の Python 3.9 でも検査が tool_error で全部止まらない。

python3.9 が手元にあれば（uv・pyenv など）、それで readiness_scan.py・preflight_ratchet.py と
本物の git commit・push（共通フック）を走らせる。3.9 より古い python3 しか無いときは、
フックが版を確かめて分かりやすい理由で止めることも見る。値はすべて仮のもの。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME = "nexus_ai"
MAIL = "273569186+nexus-ai-2045@users.noreply.github.com"


def _find_py39():
    found = shutil.which("python3.9")
    if found:
        return found
    if shutil.which("uv"):
        proc = subprocess.run(
            ["uv", "python", "find", "3.9"], capture_output=True, text=True
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


PY39 = _find_py39()
needs_hooks = pytest.mark.skipif(
    os.name == "nt" or shutil.which("gitleaks") is None,
    reason="ひな形は sh・gitleaks で動く",
)
needs_py39 = pytest.mark.skipif(PY39 is None, reason="python3.9 が無い")


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env
    )


@pytest.fixture
def work(tmp_path, monkeypatch):
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("REPO_PREFLIGHT_HOME", str(ROOT))
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "commit-msg", "pre-push"):
        shutil.copy2(ROOT / "templates/hooks" / name, hooks / name)
        (hooks / name).chmod(0o755)
    repo = tmp_path / "w"
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    for k, v in (
        ("user.name", NAME),
        ("user.email", MAIL),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        _git(repo, "config", k, v)
    _git(repo, "remote", "add", "origin", str(bare))
    (repo / "base").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base")
    _git(repo, "commit", "-q", "--no-verify", "-m", "土台")
    return repo


def _shim_path(tmp_path, python3):
    """python3 だけを差し替えた PATH（ほかの python は見えない）。"""
    shim = tmp_path / "shim"
    shim.mkdir()
    for tool in ("git", "gitleaks", "sh", "cat", "env"):
        found = shutil.which(tool)
        if found:
            (shim / tool).symlink_to(found)
    target = shim / "python3"
    if isinstance(python3, str):
        target.write_text(python3, encoding="utf-8")
        target.chmod(0o755)
    else:
        target.symlink_to(python3)
    env = dict(os.environ)
    env["PATH"] = str(shim)
    return env


@needs_py39
def test_readiness_scanは39でもtool_errorにならない(work):
    proc = subprocess.run(
        [
            PY39,
            str(ROOT / "scripts/readiness_scan.py"),
            "--repo",
            str(work),
            "--allowed-identities",
            str(ROOT / "references/public-commit-identities.json"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    report = json.loads(proc.stdout)
    assert report["status"] != "tool_error", report.get("issues")
    assert "Traceback" not in proc.stderr


@needs_py39
def test_ラチェットは39でも判定できる(work):
    script = str(ROOT / "scripts/preflight_ratchet.py")
    made = subprocess.run(
        [PY39, script, "--repo", str(work), "--update-baseline"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert made.returncode == 0, made.stdout[-300:]
    _git(work, "add", ".repo-preflight/ratchet-baseline.json")
    _git(work, "commit", "-q", "--no-verify", "-m", "基準値")
    again = subprocess.run(
        [PY39, script, "--repo", str(work)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(again.stdout)["status"] == "pass"


@needs_hooks
@needs_py39
def test_python3が39だけでもフックでcommitとpushができる(work, tmp_path):
    env = _shim_path(tmp_path, Path(PY39))
    # 新リポと同じ全件 0 の基準値（送り先に基準値が無ければ比較元は全件 0）
    counts = dict.fromkeys(
        json.loads(
            subprocess.run(
                [
                    PY39,
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import json, preflight_ratchet as r; print(json.dumps(r.RATCHET_CHECKS))",
                    str(ROOT / "scripts"),
                ],
                capture_output=True,
                text=True,
            ).stdout
        ),
        0,
    )
    (work / ".repo-preflight").mkdir()
    (work / ".repo-preflight/ratchet-baseline.json").write_text(
        json.dumps({"schema": "repo-preflight.ratchet-baseline/v1", "counts": counts})
        + "\n",
        encoding="utf-8",
    )
    _git(work, "add", ".repo-preflight/ratchet-baseline.json")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _git(work, "add", "a.txt")
    result = _git(work, "commit", "-q", "-m", "対照", env=env)
    assert result.returncode == 0, result.stderr
    pushed = _git(work, "push", "-q", "origin", "main", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert "Traceback" not in result.stderr + pushed.stderr


@needs_hooks
def test_古いpythonしか無ければ理由を出して止める(work, tmp_path):
    # 版を聞かれたら 3.9 未満と答え、それ以外は何もせず通す古い python3（版を見ないフックは素通りする）
    old = "#!/bin/sh\n" 'case "$*" in *version_info*) exit 1;; esac\n' "exit 0\n"
    env = _shim_path(tmp_path, old)
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _git(work, "add", "a.txt")
    result = _git(work, "commit", "-q", "-m", "対照", env=env)
    assert result.returncode != 0
    assert "Python 3.9 以上が見つからない" in result.stderr
    pushed = _git(work, "push", "-q", "origin", "main", env=env)
    assert pushed.returncode != 0
    assert "Python 3.9 以上が見つからない" in pushed.stderr
