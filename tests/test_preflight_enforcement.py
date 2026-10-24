"""一時的なGit repositoryで、検査の失敗がpushを止めることを確認する。"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

TOOL = Path(__file__).resolve().parents[1]
EMAIL = "91+fixture-owner@users.noreply.github.com"


@pytest.mark.parametrize(
    "failure", ["masked_author", "masked_coauthor", "foreign_coauthor"]
)
def test_real_pre_push_stops_bad_commit_and_preserves_remote(tmp_path, failure):
    repo = tmp_path / "target"
    remote = tmp_path / "remote.git"
    policy = tmp_path / "identities.json"
    policy.write_text(
        json.dumps(
            {
                "schema": "public-commit-identities/v1",
                "identities": [f"Fixture Owner <{EMAIL}>", f"REDACTED <{EMAIL}>"],
            }
        )
    )
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    for key in list(env):
        if key.startswith(("GIT_", "PREFLIGHT_", "PUBLIC_IDENTITY_")) and key not in {
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
        }:
            env.pop(key)
    env.update(
        GIT_AUTHOR_NAME="Fixture Owner",
        GIT_AUTHOR_EMAIL=EMAIL,
        GIT_COMMITTER_NAME="Fixture Owner",
        GIT_COMMITTER_EMAIL=EMAIL,
    )

    def git(*args, check=True, extra=None, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd or repo,
            env={**env, **(extra or {})},
            capture_output=True,
            text=True,
            check=check,
        )

    repo.mkdir()
    git("init", "-b", "main")
    git("init", "--bare", str(remote))
    git("remote", "add", "origin", str(remote))
    for name in ["README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md"]:
        (repo / name).write_text("# Fixture\n")
    (repo / "PREFLIGHT.md").write_text("<!-- repo-preflight:review-record -->\n")
    workflow = repo / ".github/workflows/check.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: fixture\non: [push]\njobs:\n  check:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo fixture\n"
    )
    git("add", ".")
    git("commit", "-m", "Create fixture")
    hook = repo / ".git/hooks/pre-push"
    hook.write_text(
        "#!/bin/sh\nset -eu\n"
        + shlex.join(
            [
                sys.executable,
                str(TOOL / "scripts/preflight_ratchet.py"),
                "--repo",
                str(repo),
                "--allowed-identities",
                str(policy),
            ]
        )
        + "\nprintf should-not-run > "
        + shlex.quote(str(tmp_path / "continued"))
        + "\n"
    )
    hook.chmod(0o755)
    git("push", "-u", "origin", "main")
    initial = git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
    assert (tmp_path / "continued").exists()
    (tmp_path / "continued").unlink()
    message = "Create bad fixture"
    extra = {}
    if failure == "masked_author":
        extra["GIT_AUTHOR_NAME"] = "REDACTED"
    elif failure == "masked_coauthor":
        message += f"\n\nCo-authored-by: REDACTED <{EMAIL}>"
    else:
        message += "\n\nCo-authored-by: Different Name <92+other-fixture@users.noreply.github.com>"
    git("commit", "--allow-empty", "-m", message, extra=extra)
    result = git("push", "origin", "main", check=False)
    assert result.returncode != 0
    assert "public_attribution" in result.stdout
    assert not (tmp_path / "continued").exists()
    assert git("rev-parse", "refs/heads/main", cwd=remote).stdout.strip() == initial
    assert EMAIL not in result.stdout + result.stderr
