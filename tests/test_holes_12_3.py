"""設計抜け点検リスト 12-3 の穴5件のカナリア。直す前の版ではどれも落ちる。

値はすべて仮のもの（予約ドメインか、存在しない会社名）。GitHub には何も送らない。
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import identity_gate as ig  # noqa: E402
import preflight_ratchet as r  # noqa: E402

NAME = "nexus_ai"
MAIL = "273569186+nexus-ai-2045@users.noreply.github.com"
ZERO = {name: 0 for name in r.RATCHET_CHECKS}
BASELINE = ".repo-preflight/ratchet-baseline.json"


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", NAME)
    _git(path, "config", "user.email", MAIL)
    _git(path, "config", "commit.gpgsign", "false")
    return path


@pytest.fixture
def clean_git_env(monkeypatch, tmp_path):
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    template = tmp_path / "empty-template"
    template.mkdir()
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))


# 1. 会社ドメインのメールも commit-msg で止める ---------------------------------


def _commit_msg(tmp_path, text):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(text, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/identity_gate.py"),
            "--public",
            "--repo",
            str(tmp_path),
            "--message-file",
            str(msg),
        ],
        capture_output=True,
        text=True,
    )


def test_hole1_会社ドメインのメールはcommit_msgで止まる(tmp_path):
    result = _commit_msg(tmp_path, "試験\n\n連絡先 taro@" "zz-dummy-corp.co.jp\n")
    assert result.returncode == 1
    assert "名簿にないメールの形" in result.stderr
    assert "zz-dummy-corp" not in result.stdout + result.stderr  # 値は出さない


def test_hole1_URL符号化した会社メールも止まる(tmp_path):
    result = _commit_msg(tmp_path, "試験 taro%40" "zz-dummy-corp.co.jp\n")
    assert result.returncode == 1


@pytest.mark.parametrize(
    "text",
    [
        "対照\n",
        f"対照 {MAIL}\n",
        "対照 GitHub <noreply@github.com>\n",
        "例 user@example.com と user@x.invalid と a@b.test\n",
    ],
)
def test_hole1_対照_名簿のメールと予約ドメインは通る(tmp_path, text):
    assert _commit_msg(tmp_path, text).returncode == 0


# 2. 基準値ファイルが無ければ pre-push を止める -----------------------------------


def _push_fixture(tmp_path, *, with_baseline):
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = _repo(tmp_path / "w")
    _git(work, "remote", "add", "origin", str(bare))
    (work / "base").write_text("base\n", encoding="utf-8")
    _git(work, "add", "base")
    _git(work, "commit", "-qm", "土台")
    if with_baseline:
        # 仮のリポなので、今の件数を基準値にする（新リポの本番は全件 0）
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/preflight_ratchet.py"),
                "--repo",
                str(work),
                "--update-baseline",
            ],
            capture_output=True,
            text=True,
        )
        _git(work, "add", BASELINE)
        _git(work, "commit", "-qm", "基準値")
    _git(work, "push", "-q", "--no-verify", "origin", "main")
    (work / "a").write_text("a\n", encoding="utf-8")
    _git(work, "add", "a")
    _git(work, "commit", "-qm", "対照")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(ROOT / "templates/hooks/pre-push", hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)
    env = {**os.environ, "REPO_PREFLIGHT_HOME": str(ROOT)}
    return subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            f"core.hooksPath={hooks}",
            "push",
            "origin",
            "main",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


needs_hook_tools = pytest.mark.skipif(
    os.name == "nt"
    or shutil.which("gitleaks") is None
    or shutil.which("python3") is None,
    reason="pre-push の雛形は sh・python3・gitleaks で動く",
)


@needs_hook_tools
def test_hole2_基準値ファイルが無ければpre_pushが止まる(tmp_path, clean_git_env):
    result = _push_fixture(tmp_path, with_baseline=False)
    assert result.returncode != 0
    assert "基準値ファイル" in result.stderr


@needs_hook_tools
def test_hole2_対照_基準値ファイルがあれば通る(tmp_path, clean_git_env):
    result = _push_fixture(tmp_path, with_baseline=True)
    assert result.returncode == 0, result.stderr


# 3. 比較元に基準値が無いときも緩めを見る -----------------------------------------


def _report(counts):
    checks = {name: {"status": "pass"} for name in r.RATCHET_CHECKS}
    checks["secret_scan"] = {"status": "pass", "finding_count": 0}
    checks["personal_path_scan"] = {"status": "pass", "files": []}
    checks["public_attribution"] = {"status": "pass", "issue_count": 0}
    for name, n in counts.items():
        if n:
            checks[name] = {"status": "fail"}
    return {"status": "pass", "checks": checks}


def _pr_adds_baseline(tmp_path, monkeypatch, counts):
    repo = _repo(tmp_path / "repo")
    (repo / "x").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x")
    _git(repo, "commit", "-qm", "比較元（基準値なし）")
    base = _git(repo, "rev-parse", "HEAD")
    path = repo / BASELINE
    path.parent.mkdir()
    path.write_text(r.render_baseline(counts), encoding="utf-8")
    _git(repo, "add", BASELINE)
    _git(repo, "commit", "-qm", "PR が基準値を足す")
    monkeypatch.setattr(r, "run_scan", lambda repo, args: _report(counts))
    return r.main(["--repo", str(repo), "--baseline-base-ref", base])


def test_hole3_比較元に基準値が無くてもPRが足した免除は緩めとして止まる(
    tmp_path, monkeypatch, capsys, clean_git_env
):
    assert _pr_adds_baseline(tmp_path, monkeypatch, dict(ZERO, origin=1)) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "blocked"
    assert out["loosened"] == [{"check": "origin", "base": 0, "baseline": 1}]


def test_hole3_対照_全件0の基準値を足すのは通る(
    tmp_path, monkeypatch, capsys, clean_git_env
):
    assert _pr_adds_baseline(tmp_path, monkeypatch, ZERO) == 0


# 4. preflight-ref は SHA 固定（既定 main は無い） --------------------------------

WORKFLOW = (ROOT / ".github/workflows/preflight-gate.yml").read_text(encoding="utf-8")


def test_hole4_preflight_refに既定値が無く必須():
    block = WORKFLOW.split("      preflight-ref:\n", 1)[1].split(
        "\n      baseline:", 1
    )[0]
    assert "default:" not in block
    assert "required: true" in block


def _sha_guard():
    m = re.search(
        r"- name: Require pinned repo-preflight SHA\n(?:.*\n)*?\s+run: '(.+)'\n",
        WORKFLOW,
    )
    assert m, "SHA を確かめる段が無い"
    return m.group(1)


@pytest.mark.skipif(shutil.which("bash") is None, reason="workflow の段は bash で動く")
@pytest.mark.parametrize("ref", ["main", "v1.0.0", "abc1234", "0" * 39, "G" * 40, ""])
def test_hole4_枝名やタグや短いSHAは止まる(ref):
    result = subprocess.run(
        ["bash", "-e", "-c", _sha_guard()],
        env={**os.environ, "PREFLIGHT_REF": ref},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="workflow の段は bash で動く")
def test_hole4_対照_40桁SHAは通る():
    result = subprocess.run(
        ["bash", "-e", "-c", _sha_guard()],
        env={**os.environ, "PREFLIGHT_REF": "0123456789abcdef0123456789abcdef01234567"},
    )
    assert result.returncode == 0


def test_hole4_取り出した版をSHAと照合する():
    assert 'test "$(git -C tool rev-parse HEAD)" = "$PREFLIGHT_REF"' in WORKFLOW


# 5. github-actions[bot] は名簿から外す ------------------------------------------

ACTIONS_BOT = (
    "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
)


def test_hole5_同梱の名簿にgithub_actions_botが無い():
    policy = ig.load_public_policy(ig.DEFAULT_PUBLIC_POLICY)
    assert ACTIONS_BOT not in policy
    assert all(
        "[bot]" not in v or v.startswith("dependabot[bot] ") for v in policy.identities
    )


def test_hole5_github_actions_botのコミットは止まる(tmp_path, clean_git_env):
    repo = _repo(tmp_path / "repo")
    (repo / "x").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x")
    _git(repo, "commit", "-qm", "土台")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "y").write_text("y\n", encoding="utf-8")
    _git(repo, "add", "y")
    _git(
        repo,
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "-qm",
        "試験",
    )
    issues = ig.check_public_range(
        ig.load_public_policy(ig.DEFAULT_PUBLIC_POLICY), str(repo), f"{base}..HEAD"
    )
    assert any("公開許可名義と不一致" in i for i in issues)
