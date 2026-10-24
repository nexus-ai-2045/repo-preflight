"""品質判定 H5 のカナリア：pre-push は全履歴ラチェットを回さない（CI の preflight-gate へ移した）。

全履歴を見る道具（preflight_ratchet.py）を「呼ばれたら印を残して止まる」偽物に差し替えた
repo-preflight の写しで本物の git push を走らせ、pre-push が通り、偽物が呼ばれないことを見る。
基準値ファイルが無ければ止める点（12-3 の穴2）は残す。値はすべて仮のもの。GitHub には何も送らない。

品質判定 M7 のカナリア：送る commit の基準値を、送り先の基準値（比較元）と比べる。
手で件数を上げた基準値は止まり、締めた・同じ基準値は通る。
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

pytestmark = pytest.mark.skipif(
    os.name == "nt"
    or shutil.which("gitleaks") is None
    or shutil.which("python3") is None,
    reason="ひな形は sh・python3・gitleaks で動く",
)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
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
    # repo-preflight の写し。preflight_ratchet.py の全履歴検査（run_scan）だけ偽物にする。
    home = tmp_path / "rp"
    shutil.copytree(ROOT / "scripts", home / "scripts")
    shutil.copytree(ROOT / "references", home / "references")
    shutil.copy2(ROOT / ".gitleaks.toml", home / ".gitleaks.toml")
    marker = tmp_path / "ratchet-called"
    with (home / "scripts/preflight_ratchet.py").open("a", encoding="utf-8") as fh:
        fh.write(
            "\n\ndef run_scan(repo, scan_args):\n"
            "    import pathlib\n"
            f"    pathlib.Path({str(marker)!r}).write_text('called')\n"
            "    raise SystemExit(9)\n"
        )
    monkeypatch.setenv("REPO_PREFLIGHT_HOME", str(home))
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(ROOT / "templates/hooks/pre-push", hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)
    work = tmp_path / "w"
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    for k, v in (
        ("user.name", NAME),
        ("user.email", MAIL),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        _git(work, "config", k, v)
    _git(work, "remote", "add", "origin", str(bare))
    (work / "base").write_text("base\n", encoding="utf-8")
    _git(work, "add", "base")
    _git(work, "commit", "-q", "--no-verify", "-m", "土台")
    assert _git(work, "push", "-q", "--no-verify", "origin", "main").returncode == 0
    return work, marker


CHECKS = (
    "clean_worktree",
    "required_documents",
    "secret_scan",
    "personal_path_scan",
    "commit_identity",
    "dependency_configuration",
    "ci_configuration",
    "origin",
    "repository_consistency",
)


def _write_baseline(work, **raised):
    counts = {name: raised.get(name, 0) for name in CHECKS}
    path = work / ".repo-preflight/ratchet-baseline.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema": "repo-preflight.ratchet-baseline/v1", "counts": counts},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _git(work, "add", ".repo-preflight/ratchet-baseline.json")


def _add_commit(work, name):
    (work / name).write_text(name + "\n", encoding="utf-8")
    _git(work, "add", name)
    assert _git(work, "commit", "-q", "--no-verify", "-m", "対照").returncode == 0


def test_pre_pushは全履歴ラチェットを呼ばない(setup):
    work, marker = setup
    _write_baseline(work)
    _add_commit(work, "a.txt")
    result = _git(work, "push", "-q", "origin", "main")
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_基準値ファイルが無ければ今まで通り止める(setup):
    work, marker = setup
    _add_commit(work, "b.txt")
    result = _git(work, "push", "-q", "origin", "main")
    assert result.returncode != 0
    assert "基準値ファイル" in result.stderr
    assert not marker.exists()


def test_全履歴ラチェットはCIの再利用ワークフローで全イベントに走る():
    text = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")
    assert "python tool/scripts/preflight_ratchet.py" in text
    for event in ("pull_request)", "merge_group)", "push)"):
        assert event in text


def _push_base_baseline(work, **counts):
    _write_baseline(work, **counts)
    _add_commit(work, "base-baseline.txt")
    assert _git(work, "push", "-q", "--no-verify", "origin", "main").returncode == 0


def test_手で件数を上げた基準値は止める(setup):
    work, marker = setup
    _push_base_baseline(work)
    _write_baseline(work, secret_scan=3)
    _add_commit(work, "c.txt")
    result = _git(work, "push", "-q", "origin", "main")
    assert result.returncode != 0
    assert "緩めて" in result.stderr and "secret_scan: 0 → 3" in result.stderr
    assert not marker.exists()


def test_締めた基準値と同じ基準値は通す(setup):
    work, marker = setup
    _push_base_baseline(work, secret_scan=2)
    _add_commit(work, "same.txt")
    assert _git(work, "push", "-q", "origin", "main").returncode == 0
    _write_baseline(work, secret_scan=1)
    _add_commit(work, "tight.txt")
    result = _git(work, "push", "-q", "origin", "main")
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_新しい枝は送り先のmainと比べる(setup):
    work, marker = setup
    _push_base_baseline(work, secret_scan=1)
    _git(work, "fetch", "-q", "origin")
    _git(work, "switch", "-q", "-c", "topic")
    _write_baseline(work, secret_scan=2)
    _add_commit(work, "t.txt")
    result = _git(work, "push", "-q", "origin", "topic")
    assert result.returncode != 0
    assert "secret_scan: 1 → 2" in result.stderr


def test_作業ツリーにだけある基準値では通さない(setup):
    work, marker = setup
    _add_commit(work, "d.txt")
    _write_baseline(work)
    _git(work, "reset", "-q")  # ファイルはあるが commit には入っていない
    result = _git(work, "push", "-q", "origin", "main")
    assert result.returncode != 0
    assert "基準値ファイル" in result.stderr
