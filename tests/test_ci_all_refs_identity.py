import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_all_refs_identity as audit
import identity_gate as gate


@pytest.fixture
def old_fixture(tmp_path):
    identity = "Fixture <fixture@example.invalid>"
    email = "fixture@example.invalid"
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL=email,
    )

    def git(*args):
        return subprocess.check_output(
            ["git", *args], env=env, cwd=tmp_path, text=True, stderr=subprocess.PIPE
        ).strip()

    git("init", "--quiet")
    git(
        "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "base"
    )
    personal = f"fixture{chr(64)}gmail.com"
    unlisted = f"fixture{chr(64)}fictional-business.co.jp"
    rows = f"{personal}\n{unlisted}\n"
    (tmp_path / "fixture.txt").write_bytes(rows.encode("utf-8"))
    git("add", "fixture.txt")
    git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")
    policy = gate.parse_public_policy(
        json.dumps(
            {"schema": "repo-preflight.identity-policy/v1", "identities": [identity]}
        )
    )
    return (
        policy,
        str(tmp_path),
        git("rev-parse", "HEAD"),
        hashlib.sha256(rows.encode()).hexdigest(),
        git,
    )


def approve_fixture(monkeypatch, commit, blob):
    monkeypatch.setattr(audit, "REVIEWED_COMMIT", commit, raising=False)
    monkeypatch.setattr(
        audit,
        "REVIEWED_FILES",
        {"fixture.txt": (blob, {1: "個人メールの形", 2: "名簿にないメールの形"})},
        raising=False,
    )


def test_audit_only_ignores_the_exact_reviewed_fixture(old_fixture, monkeypatch):
    policy, repo, commit, blob, _ = old_fixture
    approve_fixture(monkeypatch, commit, blob)

    assert len(gate.check_public_range(policy, repo, commit, diffs=True)) == 2
    assert audit.scan_range(policy, repo, commit) == []


@pytest.mark.parametrize("mismatch", ["commit", "blob", "line", "kind"])
def test_audit_keeps_findings_if_the_review_does_not_match(
    old_fixture, monkeypatch, mismatch
):
    policy, repo, commit, blob, _ = old_fixture
    approve_fixture(monkeypatch, commit, blob)
    if mismatch == "commit":
        monkeypatch.setattr(audit, "REVIEWED_COMMIT", "0" * 40)
    elif mismatch == "blob":
        monkeypatch.setattr(audit, "REVIEWED_FILES", {"fixture.txt": ("0" * 64, {})})
    elif mismatch == "line":
        audit.REVIEWED_FILES["fixture.txt"][1][3] = audit.REVIEWED_FILES["fixture.txt"][
            1
        ].pop(1)
    else:
        audit.REVIEWED_FILES["fixture.txt"][1][1] = "名簿にないメールの形"

    baseline = gate.check_public_range(policy, repo, commit, diffs=True)
    assert len(baseline) == 2
    assert audit.scan_range(policy, repo, commit) == baseline


def test_audit_keeps_unreviewed_findings_on_other_commits(old_fixture, monkeypatch):
    policy, repo, commit, blob, git = old_fixture
    approve_fixture(monkeypatch, commit, blob)
    (Path(repo) / "unreviewed.txt").write_text(f"another{chr(64)}gmail.com\n")
    git("add", "unreviewed.txt")
    git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "unreviewed")
    head = git("rev-parse", "HEAD")

    assert audit.scan_range(policy, repo, head) == [f"{head[:8]}: 差分に個人メールの形"]


def test_audit_keeps_same_commit_findings_outside_reviewed_lines(
    old_fixture, monkeypatch
):
    policy, repo, commit, blob, _ = old_fixture
    approve_fixture(monkeypatch, commit, blob)
    audit.REVIEWED_FILES["fixture.txt"][1].pop(2)

    baseline = gate.check_public_range(policy, repo, commit, diffs=True)
    assert len(baseline) == 2
    assert audit.scan_range(policy, repo, commit) == baseline


def test_audit_keeps_an_extra_finding_even_if_its_kind_is_already_reviewed(
    old_fixture, monkeypatch
):
    policy, repo, _, _, git = old_fixture
    path = Path(repo) / "fixture.txt"
    path.write_bytes(path.read_bytes() + f"another{chr(64)}gmail.com\n".encode())
    assert len(path.read_bytes().splitlines()) == 3
    git("add", "fixture.txt")
    git("-c", "commit.gpgsign=false", "commit", "--quiet", "--amend", "--no-edit")
    commit = git("rev-parse", "HEAD")
    approve_fixture(monkeypatch, commit, hashlib.sha256(path.read_bytes()).hexdigest())

    baseline = gate.check_public_range(policy, repo, commit, diffs=True)
    assert len(baseline) == 2
    assert audit.scan_range(policy, repo, commit) == baseline
