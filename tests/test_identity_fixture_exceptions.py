"""名義の検査から外す試験データ（.repo-preflight/identity-fixture-exceptions.txt）の試験。

外すのは一覧に書いたファイルだけで、中身の SHA256 が一致するときだけ。
値はすべて架空。形の文字列は分割して組み立て、このファイル自身が検査に当たらないようにする。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import identity_gate as g  # noqa: E402

NEXUS = "nexus-ai-2045 <273569186+nexus-ai-2045@users.noreply.github.com>"
FAKE_HOME = "/".join(["", "home", "zz0000-fixture", "work"])
FAKE_MAIL = "@".join(["zz0000", "zz-dummy-corp.co.jp"])
FIXTURE_TEXT = f"HOME = {FAKE_HOME!r}\nMAIL = {FAKE_MAIL!r}\n"


def policy():
    data = {"schema": "repo-preflight.identity-policy/v1", "identities": [NEXUS]}
    return g.parse_public_policy(json.dumps(data))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "nexus-ai-2045")
    _git(
        repo, "config", "user.email", "273569186+nexus-ai-2045@users.noreply.github.com"
    )
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _commit(repo: Path, files: dict[str, str], message: str = "変更") -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def test_listed_fixture_with_same_content_is_skipped(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"tests/test_listed.py": FIXTURE_TEXT})
    exceptions = {"tests/test_listed.py": _sha(FIXTURE_TEXT)}
    assert g.check_public_range(policy(), str(repo), "HEAD", diffs=True) != []
    assert (
        g.check_public_range(
            policy(), str(repo), "HEAD", diffs=True, exceptions=exceptions
        )
        == []
    )


def test_new_tests_file_not_in_list_is_still_red(tmp_path):
    """例外一覧にない tests/ の新しいファイルは、これまでどおり止まる（フォルダごとは外さない）。"""
    repo = _repo(tmp_path)
    _commit(repo, {"tests/test_listed.py": FIXTURE_TEXT})
    _commit(repo, {"tests/test_new_unlisted.py": FIXTURE_TEXT})
    exceptions = {"tests/test_listed.py": _sha(FIXTURE_TEXT)}
    issues = g.check_public_range(
        policy(), str(repo), "HEAD~1..HEAD", diffs=True, exceptions=exceptions
    )
    assert any("差分に実ホームパスの形" in i for i in issues)
    assert any("差分に名簿にないメールの形" in i for i in issues)
    assert not any(FAKE_HOME in i or FAKE_MAIL in i for i in issues)


def test_changed_content_of_listed_file_is_red_again(tmp_path):
    """例外一覧に書いたファイルでも、中身が変われば SHA256 が合わず、また止まる。"""
    repo = _repo(tmp_path)
    _commit(repo, {"tests/test_listed.py": FIXTURE_TEXT})
    exceptions = {"tests/test_listed.py": _sha(FIXTURE_TEXT)}
    _commit(repo, {"tests/test_listed.py": FIXTURE_TEXT + f"MORE = {FAKE_HOME!r}\n"})
    issues = g.check_public_range(
        policy(), str(repo), "HEAD~1..HEAD", diffs=True, exceptions=exceptions
    )
    assert any("差分に実ホームパスの形" in i for i in issues)


def test_staged_changed_listed_file_is_red_again(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    exceptions = {"tests/test_listed.py": _sha(FIXTURE_TEXT)}
    (repo / "tests").mkdir()
    (repo / "tests/test_listed.py").write_bytes((FIXTURE_TEXT).encode("utf-8"))
    _git(repo, "add", "tests/test_listed.py")
    monkeypatch.chdir(repo)
    assert g.check_public_staged(policy(), str(repo), exceptions=exceptions) == []
    (repo / "tests/test_listed.py").write_bytes(
        (FIXTURE_TEXT + "# 変えた\n").encode("utf-8")
    )
    _git(repo, "add", "tests/test_listed.py")
    issues = g.check_public_staged(policy(), str(repo), exceptions=exceptions)
    assert any("ステージした差分に実ホームパスの形" in i for i in issues)


def test_listed_binary_is_skipped_only_with_same_content(tmp_path):
    repo = _repo(tmp_path)
    blob = b"PK\x03\x04\x00\x00fixture\x00"
    (repo / "vendor").mkdir()
    (repo / "vendor/tool.whl").write_bytes(blob)
    _git(repo, "add", "vendor/tool.whl")
    _git(repo, "commit", "-q", "-m", "変更")
    ok = {"vendor/tool.whl": hashlib.sha256(blob).hexdigest()}
    bad = {"vendor/tool.whl": "0" * 64}
    assert (
        g.check_public_range(policy(), str(repo), "HEAD", diffs=True, exceptions=ok)
        == []
    )
    issues = g.check_public_range(
        policy(), str(repo), "HEAD", diffs=True, exceptions=bad
    )
    assert any(g.NEW_BINARY_KIND in i for i in issues)


@pytest.mark.parametrize(
    "line",
    [
        "0" * 64 + "  tests/  # フォルダ",
        "0" * 64 + "  tests/*.py  # ワイルドカード",
        "0" * 64 + "  tests/test_?.py  # ワイルドカード",
        "0" * 64 + "  /abs/file.py  # 絶対パス",
        "0" * 64 + "  tests/../x.py  # 親へ",
        "0" * 64 + "  tests/x.py",
        "abc  tests/x.py  # 短いハッシュ",
    ],
)
def test_exception_list_rejects_folders_wildcards_and_bad_lines(line):
    with pytest.raises(ValueError):
        g.parse_fixture_exceptions(line + "\n")


def test_bundled_exception_list_matches_current_files_one_per_line():
    """同梱の一覧は1行1ファイルで理由つき、書いたハッシュが今のファイルの中身と一致する。"""
    text = g.FIXTURE_EXCEPTIONS_FILE.read_text(encoding="utf-8")
    entries = g.parse_fixture_exceptions(text)
    assert entries
    for path, sha in entries.items():
        assert (ROOT / path).is_file(), path
        # 索引の中身（改行の変換前）で比べる。検査もコミットの中身で比べる
        blob = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "blob", f":{path}"],
            check=True,
            capture_output=True,
        ).stdout
        assert hashlib.sha256(blob).hexdigest() == sha, path
    assert "tests/test_identity_fixture_exceptions.py" not in entries
