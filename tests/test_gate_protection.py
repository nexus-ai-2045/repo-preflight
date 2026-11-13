"""門番 (documentation-contract workflow) の無音改変を検知する見張り。

codex review (PR #16) 指摘の再発防止: 文書整合性ゲートの呼び出しが
workflow から消えたり弱められたりすると、この test が赤くなる。
solo 運用では CODEOWNERS 必須 review を使わない。門番の無音改変は
必須 CI でこの test が落ちることで止める。
"""

import json
from pathlib import Path
import runpy

import pytest
import yaml

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
    # 配布物 docs/pr-self-review.md の整合性ゲート。
    # rules_version の自己整合だけだと前置き (frontmatter・出典・警告) が
    # 無防備なので、完全ファイル digest の allowlist 照合も要求する。
    assert "PR self-review copy integrity" in executable
    workflow = yaml.safe_load(body)
    step = next(
        step
        for step in workflow["jobs"]["documentation-contract"]["steps"]
        if step.get("name") == "PR self-review copy integrity"
    )
    assert step["run"] == "python scripts/check_pr_self_review.py"


@pytest.mark.parametrize(
    "change",
    [
        "clean",
        "crlf",
        "body",
        "preamble",
        "missing-version",
        "empty-list",
        "bad-list",
        "missing-copy",
    ],
)
def test_pr_self_review_checker_accepts_only_the_reviewed_copy(
    tmp_path, monkeypatch, change
):
    docs = tmp_path / "docs"
    docs.mkdir()
    copy = docs / "pr-self-review.md"
    allowlist = docs / "pr-self-review-trusted-digests.txt"
    text = _read("docs/pr-self-review.md")
    if change == "body":
        text += "\nChanged body.\n"
    elif change == "preamble":
        text = "Changed preamble.\n" + text
    elif change == "missing-version":
        text = text.replace("rules_version", "missing_version")
    copy.write_bytes(
        text.replace("\n", "\r\n").encode() if change == "crlf" else text.encode()
    )
    allowlist.write_text(
        _read("docs/pr-self-review-trusted-digests.txt"), encoding="utf-8"
    )
    if change == "empty-list":
        allowlist.write_text("", encoding="utf-8")
    elif change == "bad-list":
        allowlist.write_text("invalid\n", encoding="utf-8")
    elif change == "missing-copy":
        copy.unlink()
    monkeypatch.chdir(tmp_path)
    if change in {"clean", "crlf"}:
        runpy.run_path(
            str(REPO / "scripts/check_pr_self_review.py"), run_name="__main__"
        )
    else:
        with pytest.raises(SystemExit) as failure:
            runpy.run_path(
                str(REPO / "scripts/check_pr_self_review.py"), run_name="__main__"
            )
        assert failure.value.code


def test_pr_self_review_allowlist_matches_the_distributed_copy():
    """allowlist が実物とずれたまま緑にならないよう、test 側でも突き合わせる。"""
    import hashlib
    import re

    canonical = (REPO / "docs/pr-self-review.md").read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(canonical).hexdigest()
    allowed = {
        line.strip().lower()
        for line in _read("docs/pr-self-review-trusted-digests.txt").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert allowed, "allowlist is empty"
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in allowed), allowed
    assert digest in allowed, f"{digest} not allowlisted"


def test_documentation_contract_runs_on_pr_merge_queue_and_main_push():
    body = _read(".github/workflows/documentation-contract.yml")
    assert "pull_request:" in body
    assert "merge_group:" in body
    assert "  push:\n    branches:\n      - main\n" in body


def test_ci_workflow_keeps_ruleset_required_job_names():
    body = _read(".github/workflows/ci.yml")
    assert "name: test (${{ matrix.python-version }})" in body
    assert '"3.11"' in body
    assert '"3.13"' in body


def test_ci_pins_and_runs_ai_ratchet_gate_in_every_job():
    body = _read(".github/workflows/ci.yml")
    requirements = _read("requirements-tools.txt")

    assert 'PYTHONUTF8: "1"' in body
    assert (
        body.count("python -m pip install --require-hashes -r requirements-tools.txt")
        == 2
    )
    assert body.count("python -m ai_ratchet_gate --repo .") == 2
    assert "./vendor/ai_ratchet_gate-0.1.1-py3-none-any.whl" in requirements
    assert (
        "sha256:b3f22ab772699d57906326e42189e0ab7a0ee9e33dcad55a90909ece35106cd7"
        in requirements
    )
    assert (REPO / ".ai-ratchet-gate" / "baseline.txt").is_file()


def test_consistency_config_stays_enforce():
    config = json.loads(_read(".repo-preflight-consistency.json"))
    assert config["mode"] == "enforce"


def test_solo_profile_does_not_ship_self_locking_codeowners():
    # 一人の CODEOWNERS + require_code_owner_review は自己承認不能になる。
    # 門番の保護は上の workflow / CI 見張りに置く。
    assert not (REPO / ".github" / "CODEOWNERS").exists()
