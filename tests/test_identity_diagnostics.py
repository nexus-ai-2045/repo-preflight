"""Detector verdicts stay authoritative; diagnostics reveal locations only."""

import json
import re
import shutil
import subprocess

import pytest
import sys

from test_identity_fixture_exceptions import (
    FAKE_HOME,
    FAKE_MAIL,
    ROOT,
    _commit,
    _git,
    _repo,
    g,
    policy,
)


def _cli(repo, *args, stdin=None):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/identity_gate.py"),
            "--public",
            "--repo",
            str(repo),
            *args,
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        input=stdin,
    )
    return result.returncode, json.loads(result.stdout), result.stderr


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="requires pinned gitleaks")
def test_staged_and_push_use_new_file_lines_without_values(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"sample.txt": "first\nsecond\nthird\n"})
    base = _git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    _git(repo, "clone", "--bare", ".", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    key = "".join(("g", "hp", "_", "Q9" * 19))
    text = "first\n" + FAKE_MAIL + "\nsecond\nthird\n" + FAKE_HOME + "\n" + key + "\n"
    (repo / "sample.txt").write_text(text)
    _git(repo, "add", "sample.txt")
    code, report, stderr = _cli(repo, "--staged")
    assert code == 1
    assert {
        f["line"]
        for f in report["findings"]
        if f["source"] == "staged_diff" and f["path"] == "sample.txt"
    } == {2, 5, 6}
    for value in (FAKE_MAIL, FAKE_HOME, key):
        assert value not in json.dumps(report) + stderr
    assert "sample.txt:5" in stderr
    _git(repo, "commit", "-qm", "変更")
    oid = _git(repo, "rev-parse", "HEAD")
    code, report, _ = _cli(
        repo, "--pre-push", stdin=f"refs/heads/main {oid} refs/heads/main {base}\n"
    )
    assert code == 1
    assert {
        (f["commit"], f["line"])
        for f in report["findings"]
        if f["source"] == "commit_diff"
    } == {(oid[:8], n) for n in (2, 5, 6)}


def test_message_lines_pass_compatibility_and_public_identity_field(tmp_path):
    repo = _repo(tmp_path)
    message = tmp_path / "message"
    message.write_text("変更\n")
    assert _cli(repo, "--message-file", str(message)) == (
        0,
        {"status": "pass", "issue_count": 0},
        "",
    )
    message.write_text("変更\n\n" + FAKE_MAIL)
    code, report, stderr = _cli(repo, "--message-file", str(message))
    assert code == 1
    assert report["findings"][0]["line"] == 3
    assert FAKE_MAIL not in json.dumps(report) + stderr
    _git(repo, "config", "user.email", FAKE_MAIL)
    _commit(repo, {"ok.txt": "ok"})
    code, report, stderr = _cli(repo, "--range", "HEAD")
    assert {f["field"] for f in report["findings"]} == {
        "author.email",
        "committer.email",
    }
    assert code == 1 and FAKE_MAIL not in stderr


def test_private_index_multiline_and_location_cap(tmp_path):
    repo = _repo(tmp_path)
    private = g.Policy(
        allow_email_res=[re.compile(".*")],
        deny_res=[re.compile("missing"), re.compile("alpha\\nbeta")],
    )
    _commit(repo, {"ok.txt": "ok"}, message="変更\n\nalpha\nbeta")
    d = g.Diagnostics(private)
    assert g.check_range(private, str(repo), "HEAD", diagnostics=d) == g.check_range(
        private, str(repo), "HEAD"
    )
    (finding,) = d.findings
    assert finding["rule"] == "deny_patterns[1]" and finding["line"] is None
    assert finding["source"] == "commit_message_unlocated"
    assert "alpha" not in str(d.as_report())

    (repo / "ok.txt").write_text((FAKE_MAIL + "\n") * 4)
    _git(repo, "add", "ok.txt")
    d = g.Diagnostics(policy(), max_findings=2)
    assert g.check_public_staged(
        policy(), str(repo), diagnostics=d
    ) == g.check_public_staged(policy(), str(repo))
    assert [f["line"] for f in d.findings] == [1, 2] and d.truncated > 0


def test_sensitive_and_control_paths_are_withheld(tmp_path):
    repo = _repo(tmp_path)
    for path in (FAKE_MAIL, "split\nname.txt"):
        (repo / path).write_text(FAKE_MAIL + "\n")
    _git(repo, "add", ".")
    d = g.Diagnostics(policy())
    issues = g.check_public_staged(policy(), str(repo), diagnostics=d)
    assert issues and d.findings and all(f["path"] is None for f in d.findings)
    assert FAKE_MAIL not in str(d.as_report())
    private = g.Diagnostics(g.Policy(deny_res=[re.compile("sensitive")]))
    assert private.safe_path("src/sensitive.txt") is None


def test_joined_only_diff_hit_does_not_invent_a_line(tmp_path):
    repo = _repo(tmp_path)
    key = ("g" + "hp", "_" + "Q9" * 19)
    (repo / "sample.txt").write_text(f'x = ("{key[0]}"\n"{key[1]}")\n')
    _git(repo, "add", ".")
    d = g.Diagnostics()
    issues = g.check_public_staged(policy(), str(repo), diagnostics=d)
    assert issues and any(
        f["source"] == "staged_diff_unlocated" and f["line"] is None for f in d.findings
    )


def test_missing_scanner_remains_a_tool_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    git_only = tmp_path / "bin"
    git_only.mkdir()
    git_binary = shutil.which("git")
    assert git_binary
    (git_only / "git").symlink_to(git_binary)
    monkeypatch.setenv("PATH", str(git_only))
    code, report, stderr = _cli(repo, "--staged")
    assert code == 2
    assert report == {"status": "tool_error", "reason": "identity_input_unavailable"}
    assert "gitleaks" in stderr


def test_quoted_japanese_filename_and_hunk_content(tmp_path):
    repo = _repo(tmp_path)
    path = "手順/確認.md"
    _commit(repo, {path: "変更前\n"})
    (repo / path).write_text("++ b/another.txt\n" + FAKE_MAIL + "\n")
    _git(repo, "add", ".")
    d = g.Diagnostics(policy())
    assert g.check_public_staged(policy(), str(repo), diagnostics=d)
    (finding,) = d.findings
    assert (finding["path"], finding["line"]) == (path, 2)
    for invalid in [r'"b/\777"', r'"b/\377"', r'"b/\101', r'"b/\x41"', r'"b/\n"']:
        assert g._diff_target(invalid) is None
    private = g.Diagnostics(g.Policy(deny_res=[re.compile("確認")]))
    assert private.safe_path(path) is None
