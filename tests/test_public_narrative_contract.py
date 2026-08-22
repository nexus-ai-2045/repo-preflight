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
SCRIPTS_DIR = ROOT / "scripts"

FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")
# 存在しないフラグへの言及を許す否定・歴史記述の近傍語
FLAG_NEGATION_RE = re.compile(
    r"はない|ありません|受け付けない|there is no|does not (?:exist|have)|"
    r"no `--|フラグ名としての|CLI に|not (?:a |an )?(?:CLI )?flag|removed|削除",
    re.IGNORECASE,
)

OS_ALIASES = {
    "ubuntu": re.compile(r"ubuntu|Linux|linux", re.IGNORECASE),
    "macos": re.compile(r"macOS|macos|Mac", re.IGNORECASE),
    "windows": re.compile(r"Windows|windows"),
}

# 叙述面でコマンドと一緒に出る script 名 → その argparse 所有者
SCRIPT_OWNERS = (
    "readiness_scan.py",
    "run_preflight.py",
    "consistency_gate.py",
    "readme_release_gate.py",
    "install_runtime_skills.py",
    "runtime_smoke.py",
    "dialogue_gate.py",
    "preferences.py",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


SCAN = _load(SCAN_SCRIPT, "readiness_scan_narrative")
DIALOGUE = _load(DIALOGUE_SCRIPT, "dialogue_gate_narrative")
PREFS = _load(ROOT / "scripts" / "preferences.py", "preferences_narrative")


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


def script_cli_flags(script_name: str) -> frozenset[str]:
    """1 script の add_argument 長オプション（または readiness_scan の parser）。"""
    if script_name in {"readiness_scan.py", "run_preflight.py"}:
        return readiness_scan_flags(include_help=True)
    path = SCRIPTS_DIR / script_name
    if not path.is_file():
        return frozenset()
    src = path.read_text(encoding="utf-8")
    flags = set(re.findall(r'add_argument\(\s*"(--[a-z][a-z0-9-]*)"', src))
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
    """scan/v3 の status。GUARANTEES 列挙と report 合成式・exit 写像から導出する。"""
    blob = " ".join(SCAN.GUARANTEES)
    guarantee_match = re.search(r"status[^\n]*?\(([^)]+)\)", blob, re.IGNORECASE)
    assert guarantee_match, "GUARANTEES must list scan status vocabulary in parentheses"
    from_guarantees = {
        token.strip()
        for token in re.split(r"[/\s]+", guarantee_match.group(1))
        if token.strip() and re.fullmatch(r"[a-z_]+", token.strip())
    }
    src = SCAN_SCRIPT.read_text(encoding="utf-8")
    ternary = re.search(
        r'"status":\s*\(\s*'
        r'"(?P<a>[a-z_]+)"\s+if\s+tool_error\s+else\s+'
        r'"(?P<b>[a-z_]+)"\s+if\s+blocking\s+or\s+unknown\s+else\s+'
        r'"(?P<c>[a-z_]+)"\s*\)',
        src,
    )
    assert ternary, "scan status composition expression missing"
    from_code = {ternary.group("a"), ternary.group("b"), ternary.group("c")}
    from_exit = set(re.findall(r'report\["status"\]\s*==\s*"([a-z_]+)"', src))
    found = from_guarantees | from_code | from_exit
    assert found, "could not derive scan statuses"
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


def unknown_flag_claims(text: str, default_known: frozenset[str]) -> list[str]:
    """CLI に無い --flag を、否定文脈なしで主張している箇所を返す。

    近傍に script 名がある主張は、その script の parser だけで判定する
    （他ユーティリティの --json で readiness_scan の偽主張を通さない）。
    script 名が無い叙述は readiness_scan 正本を既定とし、CHANGELOG などでは
    少し広い範囲に所有者 script 名があればその parser を許す。
    """
    bad: list[str] = []
    for match in FLAG_RE.finditer(text):
        flag = match.group(0)
        start = max(0, match.start() - 120)
        end = min(len(text), match.end() + 120)
        window = text[start:end]
        if FLAG_NEGATION_RE.search(window):
            continue
        named = [name for name in SCRIPT_OWNERS if name in window]
        if named:
            known: set[str] = set()
            for name in named:
                known.update(script_cli_flags(name))
            if flag not in known:
                bad.append(flag)
            continue
        if flag in default_known:
            continue
        wide_start = max(0, match.start() - 500)
        wide_end = min(len(text), match.end() + 240)
        wide = text[wide_start:wide_end]
        owned = any(
            name in wide and flag in script_cli_flags(name) for name in SCRIPT_OWNERS
        )
        if not owned:
            bad.append(flag)
    return sorted(set(bad))


def assert_contains_all(
    text: str, items: list[str] | frozenset[str], *, label: str
) -> None:
    missing = [item for item in items if item not in text]
    assert not missing, f"{label} missing {missing}"


def readme_required_documents_section(readme: str) -> str:
    """必須文書を列挙している節（「必須の文書」箇条）だけを返す。"""
    match = re.search(r"(?m)^- \*\*必須の文書\*\*[^\n]*$", readme)
    assert match, "README missing required-documents bullet"
    return match.group(0)


def assert_os_python_versions_documented(
    text: str, matrix: dict[str, frozenset[str]], *, label: str
) -> None:
    """OS ごとの Python 版集合を、その OS 名の近傍で照合する。"""
    for os_name, versions in matrix.items():
        alias = OS_ALIASES[os_name]
        for version in sorted(versions):
            found = False
            for match in alias.finditer(text):
                start = max(0, match.start() - 100)
                end = min(len(text), match.end() + 120)
                if version in text[start:end]:
                    found = True
                    break
            assert found, f"{label} missing {os_name} Python {version} near OS name"


def base_ref_intent_window(text: str) -> str:
    """`--base-ref` がどの intent で使えるかを述べている行を返す。

    使用例の `--base-ref origin/...` ではなく、push/open_pr/merge を列挙した
    説明行を正とする。例行だけ残して説明を戻す退行を検知する。
    """
    for match in re.finditer(r"[^\n]*--base-ref[^\n]*", text):
        line = match.group(0)
        if all(intent in line for intent in ("push", "open_pr", "merge")):
            return line
    raise AssertionError(
        "missing --base-ref explanation that lists push / open_pr / merge"
    )


@pytest.fixture(scope="module")
def truth():
    readiness_flags = readiness_scan_flags()
    return {
        "flags": readiness_flags,
        # 主 CLI 叙述の既定 allowlist（script 近傍が無いとき）
        "known_flags": readiness_flags,
        "required": frozenset(SCAN.REQUIRED),
        "scan_statuses": scan_statuses(),
        "dialogue_statuses": dialogue_statuses(),
        "preferences_schema": PREFS.PREFERENCES_SCHEMA,
        "intents": frozenset(DIALOGUE.INTENTS),
        "dismissal_modes": dismissal_modes(),
        "ci_matrix": ci_os_python_matrix(_read(".github/workflows/ci.yml")),
    }


def test_readme_documents_required_files_and_cli_flags(truth):
    readme = _read("README.md")
    required_section = readme_required_documents_section(readme)
    assert_contains_all(
        required_section, truth["required"], label="README REQUIRED section"
    )
    assert_contains_all(readme, truth["flags"], label="README CLI flags")
    assert_contains_all(readme, truth["scan_statuses"], label="README scan status")
    assert_contains_all(
        readme, truth["dialogue_statuses"], label="README dialogue status"
    )
    assert truth["preferences_schema"] in readme
    assert "recorded" in readme
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
        assert_os_python_versions_documented(text, matrix, label=rel)
        assert re.search(r"Windows|windows", text), f"{rel} missing Windows"
        assert re.search(r"macOS|macos|Mac", text), f"{rel} missing macOS"


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
        assert intent in base_ref_intent_window(skill), intent
    assert not unknown_flag_claims(skill, truth["known_flags"])
    for mode in sorted(truth["dismissal_modes"]):
        assert mode in skill, f"SKILL missing dismissal mode {mode}"


def test_runtime_adapters_match_base_ref_intents(truth):
    for rel in ("runtime/claude-code/SKILL.md", "runtime/grok/SKILL.md"):
        text = _read(rel)
        window = base_ref_intent_window(text)
        for intent in ("push", "open_pr", "merge"):
            assert intent in window, f"{rel} base-ref window missing intent {intent}"
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
