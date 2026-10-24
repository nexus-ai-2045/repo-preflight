"""全 ref 検査（.github/workflows/all-refs-scan.yml）の形と、実際の Git 履歴での赤・緑。"""

import hashlib
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
# 以下はすべて架空の値。個人名が ref 名・ファイル名・作者名・タグの作者・注釈文に入った場合の代わりで、
# ログ（標準出力・標準エラー）のどこにも出てはいけない
PERSONAL = "fictional-hanako-fixture"
UNLISTED_NAME = "Fictional Hanako"
UNLISTED_EMAIL = "fictional-hanako@example.invalid"
UNLISTED = f"{UNLISTED_NAME} <{UNLISTED_EMAIL}>"
PERSONAL_FILE = f"{PERSONAL}-notes.md"
ORIGIN_URL = "https://example.invalid/fixture.git"
# 実在しない形だけの鍵（gitleaks の github-pat 規則に当たる）。ソースに字面で置かないよう実行時に組み立てる
FAKE_TOKEN = "gh" + "p_" + hashlib.sha256(b"all-refs-scan fixture").hexdigest()[:36]


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
    # actions/checkout の全履歴取得は取った枝・タグの名前をログに出すので、浅く1つだけ取る
    assert len(targets) == 1 and targets[0]["with"]["fetch-depth"] == 1
    fetch = [step for step in steps() if "git -C target fetch" in step.get("run", "")]
    assert len(fetch) == 1
    run = fetch[0]["run"]
    assert "fetch -q --unshallow" in run
    for refspec in (
        "'+refs/heads/*:refs/remotes/origin/*'",
        "'+refs/tags/*:refs/tags/*'",
        "'+refs/pull/*/head:refs/remotes/pull/*'",
    ):
        assert refspec in run
    assert "refs/heads refs/remotes refs/tags" in scan_script()


def test_logs_only_counts_and_kinds():
    """ref 名・ファイル名・作者名・ハッシュはログに出さない。各検査の出力はファイルに書き、種類と件数に直す。"""
    script = scan_script()
    for line in script.splitlines():
        if re.search(r"\b(?:echo|printf)\b", line) and ">>" not in line:
            # ログに出す echo・printf は固定の文と件数・番号の変数だけ
            for var in ("ref", "sha", "refs", "tag_sha", "mb", "label"):
                assert f"${var}" not in line and f"${{{var}" not in line, line
    # 検査道具の出力はそのままログへ流さない（行の継続はつないで見る）
    joined = re.sub(r"\\\n\s*", " ", script)
    for call in (
        'identity_gate.py" --public',
        'preflight_ratchet.py" "${ratchet_args[@]}"',
        "gitleaks git ",
    ):
        lines = [line for line in joined.splitlines() if call in line]
        assert lines, call
        for line in lines:
            assert re.search(
                r">\s*\"?\$work/\w+\.out\"?\s+2>&1|> /dev/null 2>&1", line
            ), line
    # GitHub の注釈（::error など）も出さない
    assert "::" not in all_run_text()
    # actions/checkout 以外で ref を取る git fetch は全部 -q
    for line in all_run_text().splitlines():
        if re.search(r"\bgit\b.*\bfetch\b", line):
            assert re.search(r"fetch -q\b", line), line


def test_annotated_tags_are_checked_before_peeling():
    script = scan_script()
    assert 'cat-file -t "$ref"' in script
    assert "identity_gate.check_pushed_tags(" in script
    # 許可名義はデフォルト枝の版（tool 側）
    assert 'python - "$tool/scripts" "$policy" "$target"' in script


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


def assert_nothing_personal(result, *hashes):
    """ログのどこにも、ref 名・ファイル名・作者名・タグの作者・注釈文・ハッシュが出ていない。"""
    out = result.stdout + result.stderr
    for value in (
        PERSONAL,
        PERSONAL_FILE,
        UNLISTED_NAME,
        UNLISTED_EMAIL,
        "fictional",
        "Fictional",
        "refs/",
        "leak.md",
        FAKE_TOKEN,
    ):
        assert value not in out, value
    for sha in hashes:
        assert sha[:7] not in out


def red_lines(result):
    return [line for line in result.stderr.splitlines() if line.startswith("赤:")]


@needs_tools
def test_clean_branch_without_pr_is_green(tmp_path, origin):
    git(origin, "checkout", "-qb", f"{PERSONAL}/clean")
    commit_file(origin, "notes.md", "clean change\n")
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 0, result.stderr
    assert not red_lines(result)
    assert "緑: 全 ref" in result.stdout
    assert re.search(
        r"^ref \d+ 件（注釈付きタグ 0 件、検査する commit 2 件）$", result.stdout, re.M
    )
    assert_nothing_personal(result)


@needs_tools
def test_leaky_branch_is_red_with_kind_only(tmp_path, origin):
    """個人名入りの枝名・ファイル名・作者名（架空）。赤だが、ログは種類と件数だけ。"""
    git(origin, "checkout", "-qb", f"{PERSONAL}/leak")
    leaked = commit_file(origin, PERSONAL_FILE, f"連絡先 {UNLISTED}\n", author=UNLISTED)
    # 枝側で例外一覧を広げても、道具はデフォルト枝の版しか読まない
    (origin / ".repo-preflight/identity-fixture-exceptions.txt").write_text(
        "# widened on the branch\n", encoding="utf-8"
    )
    git(origin, "add", ".repo-preflight")
    git(origin, "commit", "-qm", "widen exceptions on branch")
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    lines = red_lines(result)
    assert any(
        re.fullmatch(r"赤: 検査 \d+/2（ref 1 件）: identity_gate: .+ \d+ 件", line)
        for line in lines
    ), lines
    assert_nothing_personal(result, leaked)


@needs_tools
def test_leaky_tag_on_no_branch_is_red(tmp_path, origin):
    git(origin, "checkout", "-q", "--detach", "main")
    leaked = commit_file(origin, PERSONAL_FILE, "unlisted author\n", author=UNLISTED)
    git(origin, "tag", f"{PERSONAL}-light")
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert any(": identity_gate: " in line for line in red_lines(result))
    assert_nothing_personal(result, leaked)


@needs_tools
def test_secret_in_personal_file_is_red_with_rule_only(tmp_path, origin):
    """gitleaks の指摘も、ファイル名・行・commit を出さず規則の種類と件数だけ。"""
    git(origin, "checkout", "-qb", f"{PERSONAL}/secret")
    leaked = commit_file(origin, PERSONAL_FILE, f"token = {FAKE_TOKEN}\n")
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert any(
        re.fullmatch(
            r"赤: 検査 \d+/2（ref 1 件）: gitleaks: gitleaks [\w-]+ \d+ 件", line
        )
        for line in red_lines(result)
    ), red_lines(result)
    assert_nothing_personal(result, leaked)


@needs_tools
def test_ref_to_non_commit_is_red_without_its_name(tmp_path, origin):
    tree = git(origin, "rev-parse", "HEAD^{tree}")
    git(origin, "update-ref", f"refs/tags/{PERSONAL}-tree", tree)
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert "赤: commit を指していない ref 1 件" in result.stderr
    assert_nothing_personal(result, tree)


# --- P2: 注釈付きタグの作者（tagger）と注釈文 ---------------------------------


def make_tag(repo, name, message, *, tagger=None, target="main"):
    env = {**os.environ}
    if tagger:
        env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"] = tagger
    git(repo, "tag", "-a", name, "-m", message, target, env=env)
    return git(repo, "rev-parse", name)


@needs_tools
def test_clean_annotated_tag_is_green(tmp_path, origin):
    make_tag(origin, "v0.0.1", "公開用のタグ\n")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 0, result.stderr
    assert "注釈付きタグ 1 件" in result.stdout


@needs_tools
def test_annotated_tag_with_unlisted_tagger_is_red(tmp_path, origin):
    """commit はきれいでも、タグの作者が名簿に無ければ赤。値・タグ名・ハッシュは出さない。"""
    tag = make_tag(
        origin,
        f"{PERSONAL}-tagger",
        "公開用のタグ\n",
        tagger=(UNLISTED_NAME, UNLISTED_EMAIL),
    )
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert red_lines(result) == [
        "赤: 注釈付きタグ: タグの作者が公開許可名義と不一致 1 件"
    ]
    assert_nothing_personal(result, tag)


@needs_tools
def test_annotated_tag_with_personal_identity_in_message_is_red(tmp_path, origin):
    """作者が公開名義でも、注釈文に名簿に無い名義の行があれば赤。値は出さない。"""
    tag = make_tag(
        origin,
        "v0.0.2",
        f"公開用のタグ {PERSONAL}\n\nCo-authored-by: {UNLISTED}\n",
    )
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert red_lines(result) == [
        "赤: 注釈付きタグ: 作者情報の行が公開許可名義と不一致 1 件"
    ]
    assert_nothing_personal(result, tag)


@needs_tools
def test_nested_annotated_tag_is_followed(tmp_path, origin):
    """タグを指すタグ。外側がきれいでも、内側の作者が名簿に無ければ赤。"""
    inner = make_tag(origin, "inner", "内側\n", tagger=(UNLISTED_NAME, UNLISTED_EMAIL))
    outer = make_tag(origin, "outer", "外側\n", target=inner)
    git(origin, "tag", "-d", "inner")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert red_lines(result) == [
        "赤: 注釈付きタグ: タグの作者が公開許可名義と不一致 1 件"
    ]
    assert_nothing_personal(result, inner, outer)
