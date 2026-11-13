"""CIの判定責務と、任意計測の起動条件を確認する。"""

from collections import Counter
from pathlib import Path

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
