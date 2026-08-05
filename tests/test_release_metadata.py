import re
import tomllib
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
