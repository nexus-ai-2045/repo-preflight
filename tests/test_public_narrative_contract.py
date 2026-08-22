"""公開向け叙述面が、コード / CI 正本と食い違わないことを fail-closed で固定する。

consistency gate はリンクや impact_map は見るが、CLI フラグ・REQUIRED・
status 語彙・CI OS matrix は見ない。ここがその欠落を埋める契約。
第二の文書システムは作らず、argparse / REQUIRED / dialogue_status / ci.yml を正本にする。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN_SCRIPT = ROOT / "scripts" / "readiness_scan.py"
DIALOGUE_SCRIPT = ROOT / "scripts" / "dialogue_gate.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")
# 存在しないフラグへの言及を許す否定・歴史記述の近傍語
FLAG_NEGATION_RE = re.compile(
    r"はない|ありません|受け付けない|there is no|does not (?:exist|have)|"
    r"no `--|フラグ名としての|CLI に|not (?:a |an )?(?:CLI )?flag|removed|削除",
    re.IGNORECASE,
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


SCAN = _load(SCAN_SCRIPT, "readiness_scan_narrative")
DIALOGUE = _load(DIALOGUE_SCRIPT, "dialogue_gate_narrative")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _parser_long_flags(parser) -> set[str]:
    flags: set[str] = set()
    for action in parser._actions:
        for opt in action.option_strings:
            if opt.startswith("--"):
                flags.add(opt)
    return flags


def readiness_scan_flags(*, include_help: bool = False) -> frozenset[str]:
    flags = _parser_long_flags(SCAN.build_parser())
    if not include_help:
        flags.discard("--help")
    return frozenset(flags)


def all_scripts_cli_flags() -> frozenset[str]:
    """scripts/ 配下の argparse 長オプション。叙述面の --apply / --json 等を誤検知しない。"""
    flags: set[str] = set(readiness_scan_flags(include_help=True))
    for path in sorted((ROOT / "scripts").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        flags.update(re.findall(r'add_argument\(\s*"(--[a-z][a-z0-9-]*)"', src))
        flags.update(re.findall(r"add_argument\(\s*'(--[a-z][a-z0-9-]*)'", src))
    flags.discard("--help")
    return frozenset(flags)


def dismissal_modes() -> frozenset[str]:
    parser = SCAN.build_parser()
    for action in parser._actions:
        if "--dismissal-mode" in action.option_strings:
            return frozenset(action.choices or ())
    raise AssertionError("--dismissal-mode missing from build_parser()")


def scan_statuses() -> frozenset[str]:
    """scan/v3 の status。GUARANTEES 正本と exit 写像の両方に現れる語だけを採用する。"""
    blob = " ".join(SCAN.GUARANTEES)
    found = {
        token
        for token in ("pass", "blocked", "tool_error")
        if re.search(rf"\b{token}\b", blob)
    }
    assert found == {"pass", "blocked", "tool_error"}, found
    # exit 写像も同じ語彙を扱う
    src = SCAN_SCRIPT.read_text(encoding="utf-8")
    assert 'report["status"] == "pass"' in src
    assert 'report["status"] == "tool_error"' in src
    return frozenset(found)


def dialogue_statuses() -> frozenset[str]:
    """dialogue/v3 の status。dialogue_status() の分岐を実行して列挙する。"""
    cases = [
        ({"status": "tool_error"}, []),
        (
            {"status": "pass"},
            [{"kind": "security_hold", "blocks_intent": True, "severity": "required"}],
        ),
        (
            {"status": "pass"},
            [{"kind": "missing_doc", "blocks_intent": True, "severity": "required"}],
        ),
        ({"status": "blocked"}, []),
        ({"status": "pass"}, []),
    ]
    found = {
        DIALOGUE.dialogue_status(scan=scan, proposals=proposals)
        for scan, proposals in cases
    }
    assert found == {"needs_human_input", "blocked", "ready_after_confirmation"}, found
    return frozenset(found)


def ci_os_python_matrix(ci_text: str) -> dict[str, frozenset[str]]:
    """ci.yml から {ubuntu|macos|windows: frozenset(python versions)} を読む。"""
    matrix: dict[str, set[str]] = {}
    for match in re.finditer(
        r"runs-on:\s*(?P<runner>[^\s#]+)\n(?P<body>.*?)(?=\n  [A-Za-z]|\Z)",
        ci_text,
        re.DOTALL,
    ):
        runner = match.group("runner")
        body = match.group("body")
        family = runner.split("-", 1)[0]
        versions: set[str] = set()
        for block in re.findall(r"python-version:\s*\[([^\]]+)\]", body):
            versions.update(re.findall(r"(\d+\.\d+)", block))
        for single in re.findall(r'python-version:\s*"(\d+\.\d+)"', body):
            versions.add(single)
        if not versions:
            continue
        matrix.setdefault(family, set()).update(versions)
    assert matrix, "ci.yml から OS/Python matrix を読めない"
    return {os_name: frozenset(versions) for os_name, versions in matrix.items()}


def unknown_flag_claims(text: str, known: frozenset[str]) -> list[str]:
    """既知 CLI に無い --flag を、否定文脈なしで主張している箇所を返す。"""
    bad: list[str] = []
    for match in FLAG_RE.finditer(text):
        flag = match.group(0)
        if flag in known:
            continue
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 80)
        window = text[start:end]
        if FLAG_NEGATION_RE.search(window):
            continue
        bad.append(flag)
    return sorted(set(bad))


def assert_contains_all(text: str, items: list[str] | frozenset[str], *, label: str) -> None:
    missing = [item for item in items if item not in text]
    assert not missing, f"{label} missing {missing}"


@pytest.fixture(scope="module")
def truth():
    readiness_flags = readiness_scan_flags()
    return {
        "flags": readiness_flags,
        "known_flags": all_scripts_cli_flags() | readiness_flags,
        "required": frozenset(SCAN.REQUIRED),
        "scan_statuses": scan_statuses(),
        "dialogue_statuses": dialogue_statuses(),
        "intents": frozenset(DIALOGUE.INTENTS),
        "dismissal_modes": dismissal_modes(),
        "ci_matrix": ci_os_python_matrix(_read(".github/workflows/ci.yml")),
    }


def test_readme_documents_required_files_and_cli_flags(truth):
    readme = _read("README.md")
    assert_contains_all(readme, truth["required"], label="README REQUIRED")
    assert_contains_all(readme, truth["flags"], label="README CLI flags")
    assert_contains_all(readme, truth["scan_statuses"], label="README scan status")
    assert_contains_all(
        readme, truth["dialogue_statuses"], label="README dialogue status"
    )
    bad = unknown_flag_claims(readme, truth["known_flags"])
    assert not bad, bad


def test_readme_and_runtime_docs_match_ci_os_python_matrix(truth):
    matrix = truth["ci_matrix"]
    assert set(matrix) >= {"ubuntu", "macos", "windows"}
    for rel in (
        "README.md",
        "docs/runtime-support.md",
        "docs/architecture.md",
        "CONTRIBUTING.md",
    ):
        text = _read(rel)
        for version in sorted({v for versions in matrix.values() for v in versions}):
            assert version in text, f"{rel} missing Python {version}"
        assert re.search(r"Windows|windows", text), f"{rel} missing Windows"
        assert re.search(r"macOS|macos|Mac", text), f"{rel} missing macOS"
        # Windows だけ版が狭いときは「3.13 のみ」相当を要求（現行 CI の再発防止）
        if matrix["windows"] == frozenset({"3.13"}) and matrix["ubuntu"] == frozenset(
            {"3.11", "3.13"}
        ):
            assert re.search(
                r"(?i)windows[^\n]{0,80}3\.13", text
            ), f"{rel} must state Windows 3.13-only matrix"


def test_skill_documents_dialogue_contract(truth):
    skill = _read("SKILL.md")
    assert_contains_all(skill, truth["dialogue_statuses"], label="SKILL dialogue status")
    assert_contains_all(
        skill,
        (
            "--intent",
            "--repo",
            "--base-ref",
            "--human",
            "--record-dismissal",
            "--dismissal-mode",
        ),
        label="SKILL core flags",
    )
    assert_contains_all(skill, truth["intents"], label="SKILL intents")
    for intent in ("push", "open_pr", "merge"):
        assert intent in skill
    assert not unknown_flag_claims(skill, truth["known_flags"])
    for mode in sorted(truth["dismissal_modes"]):
        assert mode in skill, f"SKILL missing dismissal mode {mode}"


def test_runtime_adapters_match_base_ref_intents(truth):
    for rel in ("runtime/claude-code/SKILL.md", "runtime/grok/SKILL.md"):
        text = _read(rel)
        assert "--base-ref" in text
        for intent in ("push", "open_pr", "merge"):
            assert intent in text, f"{rel} missing base-ref intent {intent}"
        bad = unknown_flag_claims(text, truth["known_flags"])
        assert not bad, (rel, bad)


def test_changelog_does_not_claim_unknown_cli_flags(truth):
    bad = unknown_flag_claims(_read("CHANGELOG.md"), truth["known_flags"])
    assert not bad, bad


def test_target_diff_is_never_claimed_as_cli_flag():
    """再発防止の代表例: 内部 mode 名を CLI フラグとして売らない。"""
    for rel in (
        "README.md",
        "SKILL.md",
        "CHANGELOG.md",
        "runtime/claude-code/SKILL.md",
        "runtime/grok/SKILL.md",
        "docs/intent-dialogue-options.md",
    ):
        text = _read(rel)
        for match in re.finditer(r"--target-diff", text):
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 80)
            window = text[start:end]
            assert FLAG_NEGATION_RE.search(window), (
                f"{rel} claims --target-diff without negation: {window!r}"
            )


def test_guarantees_doc_names_both_status_vocabularies(truth):
    text = _read("docs/guarantees-and-limits.md")
    assert_contains_all(text, truth["scan_statuses"], label="guarantees scan")
    assert_contains_all(text, truth["dialogue_statuses"], label="guarantees dialogue")