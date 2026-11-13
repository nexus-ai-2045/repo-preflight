import pytest

from benchmark_review_tests import Invalid, checked_summary, verify_work


@pytest.mark.parametrize(
    "output", ["1 passed, 1 skipped in 0.1s", "no tests ran in 0.1s"]
)
def test_incomplete_pytest_scope_rejected(output):
    with pytest.raises(Invalid):
        checked_summary(output, 0)


def test_successful_exit_does_not_prove_matrix_work():
    with pytest.raises(Invalid):
        verify_work("candidate", {"gate": 4, "git": 10, "main": 136}, 137)


def test_invalid_ref_creates_no_report(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    report = tmp_path / "result.json"
    script = Path(__file__).with_name("benchmark_review_tests.py")
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--base-ref",
            ":",
            "--candidate-ref",
            "HEAD",
            "--output",
            str(report),
        ],
        capture_output=True,
        cwd=script.resolve().parents[1],
    )
    assert result.returncode != 0
    assert result.stderr.strip() == b"child_failure"
    assert not report.exists()


def test_ledger_hashes_the_actual_message_file_bytes(monkeypatch):
    import sys
    from hashlib import sha256
    import json
    from types import ModuleType

    import benchmark_review_tests as benchmark

    table = ModuleType("benchmark_message_fixture")
    table.GREEN = {"safe": "safe control"}
    table.RED = {"blocked": "blocked control"}
    monkeypatch.setitem(sys.modules, table.__name__, table)
    monkeypatch.setattr(benchmark, "MATRIX", [table.__name__])
    pairs = sorted(
        (sha256(("試験\n\n" + text + "\n").encode("utf-8")).hexdigest(), code)
        for text, code in (("safe control", 0), ("blocked control", 1))
    )
    expected = sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()
    assert benchmark.ledger()["canonical_sha256"] == expected


def test_comparison_metadata_does_not_allow_production_changes():
    from benchmark_review_tests import allowed_comparison_paths

    assert allowed_comparison_paths(
        [
            b"tests/test_review_3c.py",
            b".test-selection.json",
            b"OPERATIONS.md",
            b"docs/test-efficiency.md",
            b"docs/runtime-support.md",
        ]
    )
    assert not allowed_comparison_paths([b"scripts/identity_gate.py"])
    assert not allowed_comparison_paths([b"references/gitleaks-pins.json"])


def test_other_main_calls_cannot_hide_missing_matrix_work():
    counts = {
        "gate": 13,
        "git": 96,
        "non_message_gate": 11,
        "main": 140,
        "matrix_cases": 137,
        "matrix_gate": 0,
        "matrix_main": 136,
    }
    with pytest.raises(Invalid):
        verify_work("candidate", counts, 137)
    counts["matrix_main"] = 137
    verify_work("candidate", counts, 137)


def test_matching_inputs_remain_comparable_after_the_deduplication():
    from benchmark_review_tests import matched_ledgers

    base = {"rows": 137, "unique": 137, "canonical_sha256": "a" * 64}
    matched_ledgers(base, dict(base))
    with pytest.raises(Invalid):
        matched_ledgers(base, {**base, "canonical_sha256": "b" * 64})
    with pytest.raises(Invalid):
        matched_ledgers(base, {**base, "rows": 138})
