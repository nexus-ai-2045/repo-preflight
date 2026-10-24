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


def step_run(name):
    for step in steps():
        if step.get("name") == name:
            return step["run"]
    raise AssertionError(f"{name} が無い")


def fetch_script():
    return step_run("Fetch every branch, tag and pull request head quietly")


def all_run_text():
    return "\n".join(step.get("run", "") for step in steps())


def shell_lines(script):
    """行の継続をつなぎ、heredoc（python）と注釈を除いたシェルの行。"""
    joined = re.sub(r"\\\n\s*", " ", script)
    lines, heredoc = [], None
    for line in joined.splitlines():
        if heredoc:
            if line.strip() == heredoc:
                heredoc = None
            continue
        match = re.search(r"<<'(\w+)'", line)
        if match:
            heredoc = match.group(1)
        if not line.strip().startswith("#"):
            lines.append(line)
    return lines


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
    # actions/checkout は使わない（取った枝の名前・SHA をログに出す。トークンも作業場に残しうる）
    for step in steps():
        assert not str(step.get("uses", "")).startswith("actions/checkout@"), step
    # トークンは取得の1回だけ、環境変数の設定として渡し、終わったら消す
    fetch = fetch_script()
    assert "unset EVENT_FETCH_TOKEN" in fetch and "unset auth_header" in fetch
    assert "git config" not in fetch


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
    fetch = [step for step in steps() if "git -C target fetch" in step.get("run", "")]
    assert len(fetch) == 1
    run = fetch[0]["run"]
    assert "fetch -q --no-tags --no-write-fetch-head" in run
    for refspec in (
        "'+refs/heads/*:refs/all-refs/heads/*'",
        "'+refs/tags/*:refs/all-refs/tags/*'",
        "'+refs/pull/*/head:refs/all-refs/pull/*'",
    ):
        assert refspec in run
    # 取った ref だけの名前空間を見る。枝の名前で飛ばす ref は無い（HEAD という名前の枝も見る）
    script = scan_script()
    assert "for-each-ref --format='%(refname)' refs/all-refs/" in script
    assert "origin/HEAD" not in script


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
    # git の出力（ref 名・ファイル名・作者を含みうる）はどれもログへ流さない
    for line in shell_lines(all_run_text()):
        if not re.search(
            r"(?:^|[\s(;&|])git (?:-[Cc] |init|fetch|checkout|update-ref)", line
        ):
            continue
        assert re.search(
            r"^\s*quiet git |>>? \"?\$(?:log|gitlog|out)\"? 2>&1|2>/dev/null|2>> \"\$gitlog\"",
            line,
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
    # 検査の版は、この実行の commit（デフォルト枝）を取り出したものに限り、一致を確かめる
    fetch = fetch_script()
    assert 'checkout -q --detach "$GITHUB_SHA"' in fetch
    assert 'test "$(git -C tool rev-parse HEAD 2>/dev/null)" = "$GITHUB_SHA"' in fetch
    assert 'DEFAULT_SHA="$GITHUB_SHA"' in scan_script()
    require = by_name["Require default branch"]
    assert '"$GITHUB_REF" != "refs/heads/${default_branch}"' in require["run"]
    # 枝の名前・SHA を env に展開すると、ログの env 一覧に値が出る。既定の環境変数で読む
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.sha" not in text and "github.ref" not in text
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


def run_scan(tmp_path, origin, *, extra_env=None):
    """取得の手順（file:// の origin から）と検査の手順を、workflow の中身のまま続けて動かす。

    返す stdout・stderr は2つの手順の分をつないだもの。"""
    work = tmp_path / "runner"
    work.mkdir()
    env = {
        **os.environ,
        "GITHUB_SERVER_URL": f"file://{origin.parent}/",
        "GITHUB_REPOSITORY": origin.name,
        "GITHUB_SHA": git(origin, "rev-parse", "main"),
        "EVENT_FETCH_TOKEN": "fixture-token-not-a-secret",
        "TARGET": "target",
        "TOOL": str(ROOT),
        "ORIGIN_URL": ORIGIN_URL,
        "RUNNER_TEMP": str(tmp_path),
        **(extra_env or {}),
    }
    outs, errs = [], []
    for name, body in (("fetch", fetch_script()), ("scan", scan_script())):
        script = tmp_path / f"{name}.sh"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        result = subprocess.run(
            ["bash", str(script)],
            cwd=work,
            text=True,
            capture_output=True,
            env=env,
            timeout=600,
        )
        outs.append(result.stdout)
        errs.append(result.stderr)
        if name == "fetch":
            assert result.returncode == 0, result.stderr
    return subprocess.CompletedProcess(
        result.args, result.returncode, "".join(outs), "".join(errs)
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


# --- git が checkout でパスを出す場面 -------------------------------------------
# どれも、架空の個人名入りのファイル名・枝名を使う。確かめること:
# - ログ全体（取得と検査の両方の stdout・stderr）のどこにも名前やハッシュが出ない
# - 取り出せない ref は「checkout できない ref」として赤になる（緑で通らない）
# - 残りの ref も検査される（別の枝に置いた鍵が赤になり、全部の commit に「== 検査」が出る）


def git_input(repo, data, *args, env=None):
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=data,
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout.decode().strip()


def commit_with_entries(repo, branch, entries, message="crafted tree"):
    """main の木に、作業ツリーでは作れない項目（長すぎる名前・不正なパスなど）を足した commit を枝にする。"""
    base = subprocess.run(
        ["git", "ls-tree", "-z", "main"], cwd=repo, capture_output=True, check=True
    ).stdout
    records = [record for record in base.split(b"\0") if record]
    for mode, kind, oid, name in entries:
        records.append(f"{mode} {kind} {oid}\t".encode() + name)
    tree = git_input(repo, b"\0".join(records) + b"\0", "mktree", "-z", "--missing")
    commit = git(repo, "commit-tree", tree, "-p", "main", "-m", message)
    git(repo, "update-ref", f"refs/heads/{branch}", commit)
    return commit


def blob(repo, data):
    return git_input(repo, data, "hash-object", "-w", "--stdin")


def add_secret_branch(origin):
    """別の枝に置いた鍵。取り出せない ref のあとも検査が続けば、gitleaks の赤が出る。"""
    git(origin, "checkout", "-q", "-b", "other-secret", "main")
    sha = commit_file(origin, "token.txt", f"token = {FAKE_TOKEN}\n")
    git(origin, "checkout", "-q", "main")
    return sha


def assert_all_refs_scanned(result):
    match = re.search(r"検査する commit (\d+) 件", result.stdout)
    assert match, result.stdout
    total = int(match.group(1))
    seen = re.findall(r"^== 検査 (\d+)/(\d+)（ref \d+ 件）$", result.stdout, re.M)
    assert [int(i) for i, _ in seen] == list(range(1, total + 1)), result.stdout
    # 別の枝の鍵も見ている
    assert any(
        ": gitleaks: gitleaks " in line for line in red_lines(result)
    ), red_lines(result)


def assert_checkout_red(result):
    assert result.returncode == 1
    lines = red_lines(result)
    assert any(
        re.fullmatch(
            r"赤: 検査 \d+/\d+（ref \d+ 件）: checkout できない ref 1 件", line
        )
        for line in lines
    ), lines


@needs_tools
def test_file_name_over_255_bytes_is_red_without_name_and_rest_is_scanned(
    tmp_path, origin
):
    """作業ツリーに置けない長さのファイル名（cannot stat '<名前>'）。"""
    secret = add_secret_branch(origin)
    name = f"{PERSONAL}-{'x' * 300}.md".encode()
    bad = commit_with_entries(
        origin,
        f"{PERSONAL}/long-name",
        [("100644", "blob", blob(origin, b"long\n"), name)],
    )
    result = run_scan(tmp_path, origin)
    assert_checkout_red(result)
    assert_all_refs_scanned(result)
    assert_nothing_personal(result, bad, secret)


@needs_tools
def test_unencodable_working_tree_encoding_is_red_without_name(tmp_path, origin):
    """working-tree-encoding に変換できない中身（failed to encode '<名前>'。git は 0 で終わる）。"""
    secret = add_secret_branch(origin)
    git(origin, "checkout", "-q", "-b", f"{PERSONAL}/encoding", "main")
    commit_file(origin, f"{PERSONAL}-memo.txt", "絵文字 \U0001f600\n")
    (origin / ".gitattributes").write_text(
        "*.txt working-tree-encoding=SHIFT-JIS\n", encoding="utf-8"
    )
    git(origin, "add", ".gitattributes")
    git(origin, "commit", "-qm", "encoding")
    bad = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert_checkout_red(result)
    assert_all_refs_scanned(result)
    assert_nothing_personal(result, bad, secret)


LFS_RED = (
    r"赤: 検査 \d+/\d+（ref \d+ 件）: LFS のポインタの形をした blob（履歴を含む）1 件"
)

LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    f"oid sha256:{hashlib.sha256(b'all-refs-scan lfs fixture').hexdigest()}\n"
    "size 12345\n"
)


def lfs_config(tmp_path, kind):
    """ubuntu-latest の git-lfs（system の設定）と同じく、filter.lfs を必須にした設定を作る。"""
    config = tmp_path / f"lfs-{kind}.gitconfig"
    if kind == "git-lfs":
        filters = (
            "\tclean = git-lfs clean -- %f\n"
            "\tsmudge = git-lfs smudge -- %f\n"
            "\tprocess = git-lfs filter-process\n"
        )
    else:
        # git-lfs が無い手元でも、取りに行って失敗する形（名前を stderr に出して 128 で終わる）を真似る
        fake = tmp_path / "fake-lfs.sh"
        fake.write_text(
            '#!/bin/sh\necho "Downloading $1" >&2\n'
            'echo "Error downloading object: $1" >&2\nexit 128\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        filters = f"\tclean = cat\n\tsmudge = {fake} %f\n"
    config.write_text(f'[filter "lfs"]\n{filters}\trequired = true\n', encoding="utf-8")
    return {"GIT_CONFIG_GLOBAL": str(config)}


@needs_tools
@pytest.mark.parametrize("kind", ["fake", "git-lfs"])
def test_lfs_pointer_is_red_without_name_and_rest_is_scanned(tmp_path, origin, kind):
    """LFS のポインタ（Downloading <名前> / Error downloading object: <名前>）。LFS は動かさず、件数だけ赤。"""
    if kind == "git-lfs" and shutil.which("git-lfs") is None:
        pytest.skip("git-lfs が無い（ubuntu-latest には入っている）")
    secret = add_secret_branch(origin)
    git(origin, "checkout", "-q", "-b", f"{PERSONAL}/lfs", "main")
    (origin / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )
    bad = commit_file(origin, f"{PERSONAL}-scan.bin", LFS_POINTER)
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin, extra_env=lfs_config(tmp_path, kind))
    assert result.returncode == 1
    assert any(
        re.fullmatch(
            LFS_RED,
            line,
        )
        for line in red_lines(result)
    ), red_lines(result)
    assert "Downloading" not in result.stdout + result.stderr
    assert_all_refs_scanned(result)
    assert_nothing_personal(result, bad, secret)


@needs_tools
@pytest.mark.parametrize("kind", ["none", "git-lfs"])
def test_lfs_pointer_added_then_removed_is_red_without_name_and_rest_is_scanned(
    tmp_path, origin, kind
):
    """ポインタを足してから消した枝。checkout した木には無いが、履歴に残るので赤（件数だけ）。

    LFS の設定が無い runner と、ubuntu-latest と同じく filter.lfs を必須にした runner の両方で確かめる。"""
    if kind == "git-lfs" and shutil.which("git-lfs") is None:
        pytest.skip("git-lfs が無い（ubuntu-latest には入っている）")
    secret = add_secret_branch(origin)
    git(origin, "checkout", "-q", "-b", f"{PERSONAL}/lfs-removed", "main")
    (origin / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )
    added = commit_file(origin, f"{PERSONAL}-old-scan.bin", LFS_POINTER)
    git(origin, "rm", "-q", f"{PERSONAL}-old-scan.bin")
    git(origin, "commit", "-qm", "remove pointer")
    removed = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "-q", "main")
    extra_env = lfs_config(tmp_path, kind) if kind != "none" else None
    result = run_scan(tmp_path, origin, extra_env=extra_env)
    assert result.returncode == 1
    assert any(re.fullmatch(LFS_RED, line) for line in red_lines(result)), red_lines(
        result
    )
    assert not any("checkout できない" in line for line in red_lines(result))
    assert_all_refs_scanned(result)
    assert_nothing_personal(result, added, removed, secret)


@needs_tools
def test_invalid_path_is_red_without_name(tmp_path, origin):
    """git が作業ツリーに置くのを拒むパス（invalid path '<名前>'）。"""
    secret = add_secret_branch(origin)
    inner = git_input(
        origin,
        f"100644 blob {blob(origin, b'x')}\t{PERSONAL}-config\n".encode(),
        "mktree",
        "--missing",
    )
    bad = commit_with_entries(
        origin, f"{PERSONAL}/invalid", [("040000", "tree", inner, b".GIT")]
    )
    result = run_scan(tmp_path, origin)
    assert_checkout_red(result)
    assert_all_refs_scanned(result)
    assert_nothing_personal(result, bad, secret)


@needs_tools
def test_symlink_is_checked_out_as_plain_file_without_name(tmp_path, origin):
    """作業場の外を指すシンボリックリンク。リンクとしては置かず、名前も出さない。"""
    secret = add_secret_branch(origin)
    link = blob(origin, f"../../../{PERSONAL}-outside".encode())
    bad = commit_with_entries(
        origin,
        f"{PERSONAL}/symlink",
        [("120000", "blob", link, f"{PERSONAL}-link".encode())],
    )
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert "checkout できない ref" not in result.stderr
    assert_all_refs_scanned(result)
    assert_nothing_personal(result, bad, secret)


@needs_tools
def test_submodule_and_executable_file_do_not_print_names(tmp_path, origin):
    """サブモジュール（gitlink）と実行権限つきのファイル。"""
    secret = add_secret_branch(origin)
    gitlink = hashlib.sha1(b"all-refs-scan submodule fixture").hexdigest()
    bad = commit_with_entries(
        origin,
        f"{PERSONAL}/modes",
        [
            ("160000", "commit", gitlink, f"{PERSONAL}-module".encode()),
            ("100755", "blob", blob(origin, b"#!/bin/sh\n"), f"{PERSONAL}.sh".encode()),
        ],
    )
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert_all_refs_scanned(result)
    assert_nothing_personal(result, bad, secret)


@needs_tools
def test_case_colliding_names_do_not_print_names(tmp_path, origin):
    """大文字小文字だけ違う2つのファイル（大文字小文字を区別しない設定）。"""
    secret = add_secret_branch(origin)
    data = blob(origin, b"case\n")
    bad = commit_with_entries(
        origin,
        f"{PERSONAL}/case",
        [
            ("100644", "blob", data, f"{PERSONAL.upper()}.md".encode()),
            ("100644", "blob", data, f"{PERSONAL}.md".encode()),
        ],
    )
    config = tmp_path / "ignorecase.gitconfig"
    config.write_text("[core]\n\tignorecase = true\n", encoding="utf-8")
    result = run_scan(tmp_path, origin, extra_env={"GIT_CONFIG_GLOBAL": str(config)})
    assert result.returncode == 1
    assert_all_refs_scanned(result)
    assert_nothing_personal(result, bad, secret)
    assert PERSONAL.upper() not in result.stdout + result.stderr


@needs_tools
def test_branch_named_head_and_pull_head_are_scanned(tmp_path, origin):
    """HEAD という名前の枝と PR の head（refs/pull/*/head）も、飛ばさずに検査する。"""
    git(origin, "checkout", "-q", "--detach", "main")
    on_head = commit_file(origin, PERSONAL_FILE, "head branch\n", author=UNLISTED)
    git(origin, "update-ref", "refs/heads/HEAD", on_head)
    git(origin, "checkout", "-q", "--detach", "main")
    on_pull = commit_file(origin, "pull.md", f"token = {FAKE_TOKEN}\n")
    git(origin, "update-ref", "refs/pull/1/head", on_pull)
    git(origin, "checkout", "-q", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 1
    assert "検査する commit 3 件" in result.stdout
    lines = red_lines(result)
    assert any(": identity_gate: " in line for line in lines), lines
    assert any(": gitleaks: gitleaks " in line for line in lines), lines
    assert_nothing_personal(result, on_head, on_pull)


@needs_tools
def test_logs_do_not_show_default_branch_name_or_sha(tmp_path, origin):
    """取得・取り出しを含めたログに、デフォルト枝の名前と SHA を出さない。"""
    main_sha = git(origin, "rev-parse", "main")
    result = run_scan(tmp_path, origin)
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert main_sha[:7] not in out
    assert "main" not in out
