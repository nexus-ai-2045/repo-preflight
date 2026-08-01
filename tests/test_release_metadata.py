import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_version_matches_latest_changelog_release():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert version == "0.2.0"
    assert f"## {version} - " in changelog
