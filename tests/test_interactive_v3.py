import importlib.util
import json
import subprocess
from io import StringIO
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readiness_scan.py"
SPEC = importlib.util.spec_from_file_location("readiness_scan", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test Author")
    git(repo, "config", "user.email", "test-author@example.invalid")
    for name in MODULE.REQUIRED:
        body = f"# {name}\n"
        if name == MODULE.REVIEW_RECORD:
            body += f"{MODULE.REVIEW_RECORD_MARKER}\n"
        (repo / name).write_text(body, encoding="utf-8")
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", "https://github.com/example/repo.git")
    return repo


class ScriptedInput:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)

    def __call__(self, prompt: str) -> str:
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.answers.pop(0)


def test_boundary_text_lists_guarantees_and_non_guarantees():
    text = MODULE.format_boundary_text()
    assert "保証すること" in text
    assert "保証しないこと" in text
    assert "読み取り専用" in text
    assert "秘密情報が存在しないことの完全保証" in text


def test_enrich_report_adds_v3_schema_and_boundaries(tmp_path: Path):
    repo = make_repo(tmp_path)
    report = MODULE.scan(repo)
    options = MODULE.ScanOptions(repo=repo, audience="public", release=True)
    enriched = MODULE.enrich_report(report, options)

    assert enriched["schema"] == MODULE.SCHEMA
    assert enriched["options"]["audience"] == "public"
    assert enriched["options"]["mode"] == "release"
    assert enriched["guarantees"] == list(MODULE.GUARANTEES)
    assert enriched["non_guarantees"] == list(MODULE.NON_GUARANTEES)
    assert enriched["publication_decision"] == "blocked_human_review_required"


def test_collect_interactive_options_walks_menus(tmp_path: Path):
    repo = make_repo(tmp_path)
    answers = ScriptedInput(
        [
            "1",  # audience public
            "2",  # mode release
            str(repo),  # path
            "y",  # use identity
            "Release Bot <release@example.invalid>",
            "n",  # hide JSON
            "y",  # confirm
        ]
    )
    logs: list[str] = []

    options = MODULE.collect_interactive_options(
        input_fn=answers,
        output_fn=logs.append,
    )

    assert options.audience == "public"
    assert options.release is True
    assert options.repo == repo
    assert options.expected_identity == "Release Bot <release@example.invalid>"
    assert options.show_json is False
    assert options.interactive is True
    joined = "\n".join(logs)
    assert "保証すること" in joined
    assert "保証しないこと" in joined


def test_interactive_main_prints_human_summary_and_optional_json(tmp_path: Path):
    repo = make_repo(tmp_path)
    answers = ScriptedInput(
        [
            "local",
            "standard",
            str(repo),
            "n",  # no identity
            "y",  # show JSON
            "y",  # confirm
        ]
    )
    console = StringIO()
    stdout = StringIO()

    exit_code = MODULE.main(
        ["--interactive"],
        stdin_is_tty=True,
        input_fn=answers,
        console=console,
        stdout=stdout,
    )

    human = console.getvalue()
    report = json.loads(stdout.getvalue())

    assert exit_code == 0
    assert "保証すること" in human
    assert "保証しないこと" in human
    assert "status: pass" in human
    assert report["schema"] == MODULE.SCHEMA
    assert report["status"] == "pass"
    assert report["options"]["audience"] == "local"
    assert report["options"]["mode"] == "standard"
    assert report["publication_decision"] == "blocked_human_review_required"


def test_interactive_can_suppress_json(tmp_path: Path):
    repo = make_repo(tmp_path)
    answers = ScriptedInput(
        [
            "5",  # local by number
            "1",  # standard
            str(repo),
            "n",
            "n",  # no JSON
            "y",
        ]
    )
    console = StringIO()
    stdout = StringIO()

    exit_code = MODULE.main(
        ["-i"],
        stdin_is_tty=True,
        input_fn=answers,
        console=console,
        stdout=stdout,
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert "status: pass" in console.getvalue()


def test_noninteractive_without_repo_exits_with_error():
    console = StringIO()
    stdout = StringIO()

    exit_code = MODULE.main(
        [],
        stdin_is_tty=False,
        console=console,
        stdout=stdout,
    )

    assert exit_code == 2
    assert "--repo is required" in console.getvalue()
    assert stdout.getvalue() == ""


def test_noninteractive_json_includes_v3_envelope(tmp_path: Path):
    repo = make_repo(tmp_path)
    console = StringIO()
    stdout = StringIO()

    exit_code = MODULE.main(
        ["--repo", str(repo), "--audience", "team"],
        stdin_is_tty=False,
        console=console,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert console.getvalue() == ""
    assert report["schema"] == MODULE.SCHEMA
    assert report["options"]["audience"] == "team"
    assert report["guarantees"]
    assert report["non_guarantees"]


def test_human_flag_writes_summary_to_stderr(tmp_path: Path):
    repo = make_repo(tmp_path)
    console = StringIO()
    stdout = StringIO()

    exit_code = MODULE.main(
        ["--repo", str(repo), "--human"],
        stdin_is_tty=False,
        console=console,
        stdout=stdout,
    )

    assert exit_code == 0
    assert "保証すること" in console.getvalue()
    assert json.loads(stdout.getvalue())["status"] == "pass"


def test_cli_subprocess_noninteractive_still_json_only(tmp_path: Path):
    repo = make_repo(tmp_path)
    result = subprocess.run(
        ["python", str(SCRIPT), "--repo", str(repo)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["schema"] == MODULE.SCHEMA
    assert report["status"] == "pass"


def test_cli_subprocess_interactive_keeps_prompts_off_stdout(tmp_path: Path):
    """input() の prompt が stdout に混ざると JSON が壊れる。stderr へ分離する。"""
    repo = make_repo(tmp_path)
    answers = "\n".join(
        [
            "local",
            "standard",
            str(repo),
            "n",
            "y",
            "y",
            "",
        ]
    )
    result = subprocess.run(
        ["python", str(SCRIPT), "--interactive"],
        input=answers,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["schema"] == MODULE.SCHEMA
    assert report["options"]["interactive"] is True
    assert "保証すること" in result.stderr
    assert "番号または key" in result.stderr
    assert "番号または key" not in result.stdout
