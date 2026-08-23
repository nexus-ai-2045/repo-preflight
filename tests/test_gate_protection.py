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
    import re

    body = _read(".github/workflows/documentation-contract.yml")
    # コメントだけの残存文字列で緑にならないよう、実行行以外を落とす
    executable = re.sub(r"(?m)^\s*#.*$", "", body)
    assert "scripts/readme_release_gate.py" in executable
    assert "scripts/consistency_gate.py" in executable
    assert "--require-config" in executable
    assert "--require-mode enforce" in executable
    # CLI / REQUIRED / status 語彙 / CI matrix の叙述契約（第二の文書システムではない）
    # ファイル名の出現だけでなく、pytest が契約テストを実行する run step を要求する
    assert re.search(
        r"(?m)^\s*run:\s*python\s+-m\s+pytest\b[^\n]*\btests/test_public_narrative_contract\.py\b",
        executable,
    ), "documentation-contract must run pytest on test_public_narrative_contract.py"
    assert (
        'pip install -e ".[test]"' in executable
        or "pip install -e '.[test]'" in executable
    )


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


def test_ci_pins_and_runs_ai_ratchet_gate_in_every_job():
    body = _read(".github/workflows/ci.yml")

    assert body.count('python -m pip install "ai-ratchet-gate==0.1.1"') == 3
    assert body.count("python -m ai_ratchet_gate --repo .") == 3
    assert (REPO / ".ai-ratchet-gate" / "baseline.txt").is_file()


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
        "/.ai-ratchet-gate/",
        "/tests/test_gate_protection.py",
        "/tests/test_public_narrative_contract.py",
    ):
        assert required in entries, required
