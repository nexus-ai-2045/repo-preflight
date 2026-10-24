"""全 ref 検査（.github/workflows/all-refs-scan.yml）の形と、実際の Git 履歴での赤・緑。"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/all-refs-scan.yml"
PUBLIC_NAME = "nexus_ai"
PUBLIC_EMAIL = "273569186+nexus-ai-2045@users.noreply.github.com"
# 名簿に無い名義。値はログに出てはいけない
UNLISTED = "Unapproved Fixture <unapproved-fixture@example.invalid>"
ORIGIN_URL = "https://example.invalid/fixture.git"


def load_workflow():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps():
    return load_workflow()["jobs"]["scan"]["steps"]


def scan_script():
    for step in steps():
        if step.get("name") == "Scan every ref":
            return step["run"]
    raise AssertionError("Scan every ref が無い")


def all_run_text():
    return "\n".join(step.get("run", "") for step in steps())


def test_triggers_are_schedule_and_manual_only():
    wf = load_workflow()
    # PyYAML は on: を真偽値 True として読む
    triggers = wf.get("on", wf.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert len(crons) == 1
    minute, hour = crons[0].split()[:2]
    # 毎時0分の混雑を避けた半端な分、日に1回
    assert minute.isdigit() and int(minute) % 2 == 1 and int(minute) not in (0, 30)
    assert hour.isdigit()


def test_token_is_read_only():
    wf = load_workflow()
    assert wf["permissions"] == {"contents": "read"}
    for job in wf["jobs"].values():
        assert "permissions" not in job
    text = WORKFLOW.read_text(encoding="utf-8")
    assert ": write" not in text
    for step in steps():
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False


def test_actions_and_gitleaks_are_pinned():
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, re.M)
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
    runs = all_run_text()
    # preflight-gate・ci と同じ固定版の入れ方（配布物ハッシュと本体ハッシュ）だけを使う
    assert "tool/scripts/install_pinned_gitleaks.py" in runs
    assert "curl" not in runs and "wget" not in runs
    assert "go install" not in runs


def test_fetches_every_branch_tag_and_pull_head():
    targets = [
        step
        for step in steps()
        if str(step.get("uses", "")).startswith("actions/checkout@")
        and step["with"].get("path") == "target"
    ]
    assert len(targets) == 1 and targets[0]["with"]["fetch-depth"] == 0
    runs = all_run_text()
    assert "'+refs/pull/*/head:refs/remotes/pull/*'" in runs
    assert "refs/heads refs/remotes refs/tags" in scan_script()


def test_policy_and_exceptions_come_only_from_default_branch_tool():
    by_name = {step.get("name"): step for step in steps()}
    tool = by_name["Check out repo-preflight (default branch)"]
    assert tool["with"]["ref"] == "${{ github.sha }}"
    assert by_name["Scan every ref"]["env"]["DEFAULT_SHA"] == "${{ github.sha }}"
    assert "refs/heads/${default_branch}" in by_name["Require default branch"]["run"]
    script = scan_script()
    for call in re.findall(r"python \"\$\w+/scripts/[\w.]+\.py\"", script):
        assert call.startswith('python "$tool/scripts/'), call
    assert 'policy="$tool/references/public-commit-identities.json"' in script
    assert '--config "$tool/.gitleaks.toml"' in script
    # 検査する ref の中身（例外一覧・許可名義）を道具として読まない
    assert '"$scan/scripts' not in script and '"$scan/.repo-preflight' not in script
    assert '"$scan/references' not in script


def test_never_prints_values_or_writes_to_github():
    runs = all_run_text()
    assert "set -x" not in runs and "set +ax" in scan_script()
    gitleaks_calls = [line for line in runs.splitlines() if "gitleaks git" in line]
    assert gitleaks_calls
    for line in gitleaks_calls:
        assert "--redact" in line
        assert not re.search(r"(?:^|\s)(?:-v|--verbose)(?:\s|$)", line)
    # report から出すのはファイル・行・commit・規則だけ。値（Secret・Match・Line）は読まない
    for field in ("Secret", "Match", "'Line'", '"Line"', "Email", "Author"):
        assert field not in scan_script()
    for forbidden in (
        "gh issue",
        "gh pr",
        "--method",
        "-X POST",
        "-X PATCH",
        "git push",
    ):
        assert forbidden not in runs
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github-script" not in text and "upload-artifact" not in text


# --- 実際の履歴での赤・緑 ---------------------------------------------------

needs_tools = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("gitleaks") is None,
    reason="全 ref 検査は Ubuntu の bash と固定版 gitleaks で動く",
)


def git(repo, *args, env=None):
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch):
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "BASH_ENV",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    template = tmp_path / "empty-template"
    template.mkdir()
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", PUBLIC_NAME)
    git(repo, "config", "user.email", PUBLIC_EMAIL)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", ORIGIN_URL)
    # 小さい repo なので必須文書・CI 設定の件数は 0 にならない。人がレビューして入れた基準値として main に置く
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/preflight_ratchet.py"),
            "--repo",
            str(repo),
            "--allowed-identities",
            str(ROOT / "references/public-commit-identities.json"),
            "--update-baseline",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    git(repo, "add", ".repo-preflight/ratchet-baseline.json")
    git(repo, "commit", "-qm", "baseline")
    return repo


def commit_file(repo, name, text, *, author=None):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    args = ["commit", "-qm", f"add {name}"]
    if author:
        args[1:1] = ["--author", author]
    git(repo, *args)
    return git(repo, "rev-parse", "HEAD")


def run_scan(tmp_path, origin):
    target = tmp_path / "target"
    git(tmp_path, "clone", "-q", str(origin), str(target))
    script = tmp_path / "scan.sh"
    script.write_text(textwrap.dedent(scan_script()), encoding="utf-8")
    env = {
        **os.environ,
        "TARGET": str(target),
        "TOOL": str(ROOT),
        "DEFAULT_SHA": git(origin, "rev-parse", "main"),
        "ORIGIN_URL": ORIGIN_URL,
        "RUNNER_TEMP": str(tmp_path),
    }
    return subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env=env,
        timeout=600,
    )


def assert_no_values(result):
    out = result.stdout + result.stderr
    assert "unapproved-fixture" not in out
    assert "Unapproved Fixture" not in out


@needs_tools
def test_clean_branch_without_pr_is_green(tmp_path, origin):
    git(origin, "checkout", "-qb", "clean-no-pr")
    commit_file(origin, "notes.md", "clean change\n")
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 0, result.stderr
    assert "赤:" not in result.stderr
    assert "clean-no-pr" in result.stdout


@needs_tools
def test_leaky_branch_without_pr_is_red_and_names_only_the_ref(tmp_path, origin):
    git(origin, "checkout", "-qb", "clean-no-pr")
    commit_file(origin, "notes.md", "clean change\n")
    git(origin, "checkout", "-q", "main")
    git(origin, "checkout", "-qb", "leaky-no-pr")
    leaked = commit_file(origin, "leak.md", "unlisted author\n", author=UNLISTED)
    # 枝側で例外一覧を広げても、道具はデフォルト枝の版しか読まない
    (origin / ".repo-preflight/identity-fixture-exceptions.txt").write_text(
        "# widened on the branch\n", encoding="utf-8"
    )
    git(origin, "add", ".repo-preflight")
    git(origin, "commit", "-qm", "widen exceptions on branch")
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert "赤: refs/remotes/origin/leaky-no-pr: identity_gate" in result.stderr
    assert leaked[:8] in result.stderr
    assert "clean-no-pr: " not in result.stderr
    assert_no_values(result)


@needs_tools
def test_leaky_tag_on_no_branch_is_red(tmp_path, origin):
    git(origin, "checkout", "-q", "--detach", "main")
    commit_file(origin, "leak.md", "unlisted author\n", author=UNLISTED)
    git(origin, "tag", "leaky-tag")
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert "赤: refs/tags/leaky-tag: identity_gate" in result.stderr
    assert "refs/remotes/origin/main: " not in result.stderr
    assert_no_values(result)
