"""A retained ignored original is separate from the committed delivery tree."""

import importlib.util
from pathlib import Path
import subprocess
import sys


def test_committed_deletion_with_retained_original_and_force_added_copy(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/readiness_scan.py"
    spec = importlib.util.spec_from_file_location("deleted_original_scan", script)
    scan = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scan
    spec.loader.exec_module(scan)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        ).stdout

    def commit(message):
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            message,
        )

    git("init", "-q")
    original = tmp_path / "retained.txt"
    original.write_bytes(b"unchanged original bytes")
    git("add", "retained.txt")
    commit("original")
    base = git("rev-parse", "HEAD").decode().strip()
    (tmp_path / ".gitignore").write_text("/retained.txt\n")
    git("add", ".gitignore")
    git("rm", "--cached", "--", "retained.txt")
    commit("reference boundary")
    paths, deleted = scan.changed_working_tree_files(tmp_path, base)
    assert original in deleted
    assert original not in paths
    assert original not in scan.working_tree_files(tmp_path)
    assert original.read_bytes() == b"unchanged original bytes"
    # Force-tracking must re-enter both inventory and actual submission scanning.
    git("add", "-f", "retained.txt")
    paths, _ = scan.changed_working_tree_files(tmp_path, base)
    assert original in scan.submission_files(tmp_path, paths)
    assert original in scan.working_tree_files(tmp_path)
