"""CIの判定責務と、任意計測の起動条件を確認する。"""

from collections import Counter
from pathlib import Path
import shlex
import json
import os
import subprocess
import sys

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


def test_comparison_witness_uses_all_roles_and_portable_junit_paths():
    for job in _workflow("ci.yml")["jobs"].values():
        steps = job["steps"]
        facts = [
            step
            for step in steps
            if step.get("name") == "Record CI comparison controls"
        ]
        assert len(facts) == 1
        execution = next(
            step
            for step in steps
            if step.get("name") == "pytest (changed paths and dependency neighbors)"
        )
        for directory in ("/tmp/runner-temp", r"D:\a\_temp"):
            options = shlex.split(
                execution["env"]["PYTEST_ADDOPTS"].replace(
                    "${{ runner.temp }}", directory
                )
            )
            assert "--junitxml=" + directory + "/ci-results.xml" in options
            assert "--durations=20" in options and "-v" in options
        witness = next(
            step
            for step in steps
            if step.get("name") == "Record executed test collection"
        )
        assert witness["if"] == "always()"
    for name in (
        "documentation-contract.yml",
        "ai-ratchet-gate.yml",
        "preflight-gate.yml",
    ):
        steps = [
            step for job in _workflow(name)["jobs"].values() for step in job["steps"]
        ]
        assert (
            sum(step.get("name") == "Record CI comparison controls" for step in steps)
            == 1
        )


def test_comparison_controls_are_isolated_from_checkout_python(tmp_path):
    observations = [
        step
        for name in (
            "ci.yml",
            "documentation-contract.yml",
            "ai-ratchet-gate.yml",
            "preflight-gate.yml",
        )
        for job in _workflow(name)["jobs"].values()
        for step in job["steps"]
        if step.get("name") == "Record CI comparison controls"
    ]
    scripts = {step["run"] for step in observations}
    assert len(observations) == 6 and len(scripts) == 1
    assert all(step["shell"] == "bash" for step in observations)
    lines = scripts.pop().splitlines()
    assert lines[0] == "python -I - <<'PY'" and lines[-1] == "PY"
    for module in ("json", "platform", "hashlib", "ci_environment"):
        (tmp_path / (module + ".py")).write_text(
            "raise RuntimeError('checkout code ran')\n"
        )
    marker = "synthetic-private-environment-value"
    env = dict(os.environ, CI_ENV_SENTINEL=marker, ImageVersion=marker)
    result = subprocess.run(
        [sys.executable, "-I", "-"],
        input="\n".join(lines[1:-1]),
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    facts = json.loads(result.stdout)
    assert facts["schema"] == "ci-environment/v1"
    assert facts["image_version"] is None
    assert len(facts["dependency_sha256"]) == 64
    assert marker not in result.stdout
    assert {"hostname", "user", "home", "env", "processor"}.isdisjoint(facts)
