"""CIの判定責務と、任意計測の起動条件を確認する。"""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POLICY_COMMAND = "python -m ai_ratchet_gate --repo ."
BENCHMARK_CONDITION = (
    "github.event_name == 'pull_request' && matrix.python-version == '3.11' && "
    "contains(github.event.pull_request.labels.*.name, 'ci-benchmark')"
)


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text("utf-8"))


def test_repository_policy_has_one_owner_per_supported_runtime():
    owners = []
    for name in ("ci.yml", "ai-ratchet-gate.yml"):
        for job in _workflow(name)["jobs"].values():
            runs = [
                step
                for step in job["steps"]
                if POLICY_COMMAND in step.get("run", "").splitlines()
            ]
            if not runs:
                continue
            entries = job.get("strategy", {}).get("matrix", {}).get("include")
            if entries is None:
                setup = next(
                    step
                    for step in job["steps"]
                    if "actions/setup-python@" in step.get("uses", "")
                )
                entries = [{"python-version": setup["with"]["python-version"]}]
            for entry in entries:
                for step in runs:
                    if step.get("if"):
                        assert step["if"] == "matrix.repository-policy"
                        assert type(entry["repository-policy"]) is bool
                        if not entry["repository-policy"]:
                            continue
                    owners.append((job["runs-on"], entry["python-version"]))
    assert Counter(owners) == Counter(
        [
            ("ubuntu-latest", "3.11"),
            ("ubuntu-latest", "3.13"),
            ("windows-latest", "3.13"),
        ]
    )


def test_matched_benchmark_is_explicit_and_not_a_default_ci_cost():
    steps = [
        step
        for job in _workflow("ci.yml")["jobs"].values()
        for step in job["steps"]
        if "tests/benchmark_review_tests.py" in step.get("run", "")
    ]
    assert len(steps) == 1
    assert steps[0]["if"] == BENCHMARK_CONDITION
    assert (
        steps[0]["env"]["BENCHMARK_BASE_REF"]
        == "${{ github.event.pull_request.base.sha }}"
    )


@pytest.mark.skipif(os.name == "nt", reason="documentation workflow uses Ubuntu shell")
def test_first_pr_checks_document_and_artifact_updates_against_event_base(tmp_path):
    steps = _workflow("documentation-contract.yml")["jobs"]["documentation-contract"][
        "steps"
    ]
    step = next(
        step for step in steps if step.get("name") == "Documentation consistency gate"
    )
    assert step["env"]["CONSISTENCY_BASE"] == (
        "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha "
        "|| github.event.before }}"
    )

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main", "--template=")
    git("config", "user.name", "nexus_ai")
    git("config", "user.email", "273569186+nexus-ai-2045@users.noreply.github.com")
    git("config", "commit.gpgsign", "false")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("consistency_gate.py", "readme_release_gate.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts)
    source = tmp_path / "source.py"
    overview = tmp_path / "README.md"
    artifact = tmp_path / "generated.txt"
    config = tmp_path / ".repo-preflight-consistency.json"
    policy = {
        "schema": "repo-preflight.consistency/v1",
        "mode": "enforce",
        "impact_map": [{"change": ["source.py"], "requires_any": ["README.md"]}],
        "generated_artifacts": [{"path": "generated.txt", "sources": ["source.py"]}],
    }

    def write_artifact(text):
        artifact.write_text(text, encoding="utf-8")
        policy["generated_artifacts"][0]["sha256"] = hashlib.sha256(
            artifact.read_bytes()
        ).hexdigest()
        config.write_text(json.dumps(policy), encoding="utf-8")

    overview.write_text("# Fixture\n", encoding="utf-8")
    source.write_text("VALUE = 1\n", encoding="utf-8")
    write_artifact("revision one\n")
    git("add", ".")
    git("commit", "-qm", "initial root")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    git("commit", "-qam", "initial push tip")
    base = git("rev-parse", "HEAD")

    def check_pr():
        return subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            cwd=tmp_path,
            env={
                **os.environ,
                "CONSISTENCY_BASE": base,
                "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
        )

    git("switch", "-qc", "first-pr")
    source.write_text("VALUE = 3\n", encoding="utf-8")
    git("commit", "-qam", "change source in first PR")
    blocked = check_pr()
    assert blocked.returncode == 1, blocked.stderr
    assert set(json.loads(blocked.stdout)["findings"]) == {
        "related_docs_update_missing:impact-1",
        "generated_artifact_update_missing:generated.txt",
    }

    overview.write_text("# Fixture\n\nDocument the new value.\n", encoding="utf-8")
    write_artifact("revision three\n")
    git("commit", "-qam", "update documentation and the generated artifact")
    passed = check_pr()
    assert passed.returncode == 0, passed.stderr
    report = json.loads(passed.stdout)
    assert report["status"] == "pass"
    assert report["findings"] == []
