import re

try:
    import tomllib
except ImportError:  # Python 3.9/3.10 は tomli（test 依存）
    import tomli as tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_HEADING = re.compile(
    r"(?m)^## (?P<version>\S+) - (?P<date>\d{4}-\d{2}-\d{2})\s*$"
)


def latest_changelog_release(changelog: str) -> tuple[str, str]:
    """CHANGELOGの先頭release見出しを返す。古い見出しへのfallbackはしない。"""
    match = RELEASE_HEADING.search(changelog)
    assert match, "CHANGELOGにrelease見出しが1件もない"
    return match.group("version"), match.group("date")


def test_project_version_matches_latest_changelog_release():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    latest_version, _ = latest_changelog_release(changelog)

    assert version == latest_version


def test_newer_changelog_entry_without_version_bump_is_detected():
    """先頭見出しだけを見るので、古い見出しが残っていてもdriftを検知できる."""
    changelog = "# Changelog\n\n## 0.3.0 - 2026-09-01\n\n- next\n\n## 0.2.0 - 2026-08-01\n\n- current\n"

    latest_version, latest_date = latest_changelog_release(changelog)

    assert latest_version == "0.3.0"
    assert latest_date == "2026-09-01"


def test_changelog_release_versions_are_unique():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = [match.group("version") for match in RELEASE_HEADING.finditer(changelog)]
    assert len(versions) == len(
        set(versions)
    ), "同じversionのrelease見出しが重複している"


def _release_steps():
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    return workflow["jobs"]["release"]["steps"]


def test_release_checks_existing_tag_before_remote_writes(tmp_path):
    """実際のworkflowのshellを、ネットワークのない一時repoで実行する。"""
    import os
    import shutil
    import subprocess

    import pytest

    if os.name == "nt" or not shutil.which("bash"):
        pytest.skip("releaseのUbuntu用shellを実行する検査")
    # hook起動時の外側repo/indexへfixtureのGit操作を向けない。
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "VERSION": "1.2.3",
        }
    )

    def git(*args):
        return subprocess.check_output(
            ["git", *args], cwd=tmp_path, env=env, text=True
        ).strip()

    git("init", "--quiet")
    (tmp_path / "pyproject.toml").write_text('version = "1.2.3"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("## 1.2.3 - 2026-11-06\n", encoding="utf-8")
    notes = tmp_path / "docs/releases/v1.2.3.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("# テスト版\n\n変更内容\n", encoding="utf-8")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Fixture")
    expected = git("rev-parse", "HEAD")
    env["GITHUB_SHA"] = expected
    steps = _release_steps()
    verification = steps[1]["run"]
    assert steps[2]["if"] == "inputs.delete-branches != ''"

    def verify():
        return subprocess.run(
            ["bash", "-c", verification],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    # タグなしでは公開や枝削除の前に理由付きで拒否。
    result = verify()
    assert result.returncode != 0 and "承認済みタグ" in result.stderr
    assert git("tag", "--list") == ""
    # 軽量タグ・注釈付きタグとも参照先commitが一致すれば通る。
    for annotated in [False, True]:
        if annotated:
            git("-c", "tag.gpgsign=false", "tag", "-a", "v1.2.3", "-m", "Fixture")
        else:
            git("-c", "tag.gpgsign=false", "tag", "v1.2.3")
        assert verify().returncode == 0
        git("tag", "-d", "v1.2.3")
    # 版が同じでも別commitを指せば通さない。
    git(
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "Other",
    )
    git("-c", "tag.gpgsign=false", "tag", "v1.2.3")
    before = git("rev-parse", "refs/tags/v1.2.3")
    result = verify()
    assert result.returncode != 0 and "一致しません" in result.stderr
    assert git("rev-parse", "refs/tags/v1.2.3") == before


def test_release_publication_requires_remote_tag(tmp_path):
    """公開shellのgh呼出しを置換し、タグ自動作成禁止の引数を確認する。"""
    import os
    import shutil
    import subprocess

    import pytest

    if os.name == "nt" or not shutil.which("bash"):
        pytest.skip("releaseのUbuntu用shellを実行する検査")
    notes = tmp_path / "docs/releases/v1.2.3.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("# テスト版\n\n変更内容\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "VERSION": "1.2.3",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_SHA": "a" * 40,
            "RUNNER_TEMP": str(tmp_path),
        }
    )
    # 同じshell内の関数で外部送信を止め、実際のargvだけを記録する。
    shell = (
        'gh() { printf "%s\\n" "$@" > "$RUNNER_TEMP/argv"; };\n'
        + _release_steps()[-1]["run"]
    )
    result = subprocess.run(
        ["bash", "-c", shell],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    args = (tmp_path / "argv").read_text(encoding="utf-8").splitlines()
    assert args[:3] == ["release", "create", "v1.2.3"]
    assert "--verify-tag" in args
    assert args[args.index("--target") + 1] == env["GITHUB_SHA"]
    assert args[args.index("--repo") + 1] == "owner/repo"
