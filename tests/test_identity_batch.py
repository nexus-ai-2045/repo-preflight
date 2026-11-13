"""Stored commit batch reader rejects incomplete or inconsistent Git output."""

from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import identity_gate as g


@pytest.mark.parametrize(
    "frame",
    [
        b"{oid} missing\n",
        b"{oid} tag 1\nx\n",
        b"{other} commit 1\nx\n",
        b"{oid} commit -1\n",
        b"{oid} commit 9000000000000000000\nx\n",
        b"{oid} commit 2\nx\n",
        b"{oid} commit 1\nx!",
        b"{oid} commit 1\n\xff\n",
        b"{oid} commit 1\nx\nextra",
    ],
)
def test_batch_commit_rejects_invalid_stream(monkeypatch, frame):
    oid = "a" * 40
    frame = frame.replace(b"{oid}", oid.encode()).replace(b"{other}", b"b" * 40)
    program = (
        "import sys; sys.stdin.buffer.readline(); "
        f"sys.stdout.buffer.write({frame!r})"
    )
    monkeypatch.setattr(
        g, "identity_git_command", lambda *args: ([sys.executable, "-c", program], {})
    )
    with pytest.raises(ValueError):
        list(g.batch_commit_texts(".", [oid]))


def test_batch_commit_rejects_failed_process_even_after_complete_frame(monkeypatch):
    oid = "a" * 40
    program = (
        "import sys; oid = sys.stdin.buffer.readline().strip(); "
        "sys.stdout.buffer.write(oid + b' commit 1\\nx\\n'); sys.exit(1)"
    )
    monkeypatch.setattr(
        g, "identity_git_command", lambda *args: ([sys.executable, "-c", program], {})
    )
    with pytest.raises(ValueError, match="identity_commit_batch_invalid"):
        list(g.batch_commit_texts(".", [oid]))


def test_missing_revision_cannot_become_an_empty_passing_range(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(subprocess.CalledProcessError):
        list(g.raw_commits(str(tmp_path), "nonexistent-revision", g.git))
