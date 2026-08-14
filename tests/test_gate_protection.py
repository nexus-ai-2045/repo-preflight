"""門番 (documentation-contract workflow) の無音改変を検知する見張り。

codex review (PR #16) 指摘の再発防止: 文書整合性ゲートの呼び出しが
workflow から消えたり弱められたりすると、この test が赤くなる。
この test 自体や workflow を消す変更は .github/CODEOWNERS により
所有者 review が必須になり、無音では merge できない。
残余リスク (所有者自身が bypass して merge する場合) は守備範囲外。
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_documentation_contract_workflow_invokes_the_gate_at_full_strength():
    body = _read(".github/workflows/documentation-contract.yml")
    assert "scripts/readme_release_gate.py" in body
    assert "scripts/consistency_gate.py" in body
    assert "--require-config" in body
    assert "--require-mode enforce" in body


def test_documentation_contract_runs_on_pr_merge_queue_and_main_push():
    body = _read(".github/workflows/documentation-contract.yml")
    assert "pull_request:" in body
    assert "merge_group:" in body
    assert "branches: [main]" in body


def test_ci_workflow_keeps_ruleset_required_job_names():
    body = _read(".github/workflows/ci.yml")
    assert "name: test (${{ matrix.python-version }})" in body
    assert '"3.11"' in body
    assert '"3.13"' in body


def test_consistency_config_stays_enforce():
    config = json.loads(_read(".repo-preflight-consistency.json"))
    assert config["mode"] == "enforce"


def test_codeowners_covers_gate_and_watchdog():
    entries = [
        line.split()[0]
        for line in _read(".github/CODEOWNERS").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for required in (
        "/.github/",
        "/.repo-preflight-consistency.json",
        "/tests/test_gate_protection.py",
    ):
        assert required in entries, required
