from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_development_lint.py"


@pytest.fixture
def installer():
    spec = importlib.util.spec_from_file_location("development_lint_installer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path, installer):
    root = tmp_path / "app"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    installer.install_development_lint(root)
    return root


def run_lint(root):
    return subprocess.run(
        [sys.executable, str(root / "tools/development_lint.py")],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_install_is_local_idempotent_and_preserves_documents(
    tmp_path, installer, capsys
):
    root = tmp_path / "new-app"
    root.mkdir()
    (root / "AGENTS.md").write_text(
        "# アプリの指示\n\n既存の指示。\n", encoding="utf-8"
    )
    assert installer.main(["--repo", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "READY"
    assert not (root / ".git").exists()
    assert (
        (root / "AGENTS.md")
        .read_text(encoding="utf-8")
        .startswith("# アプリの指示\n\n既存の指示。")
    )
    assert installer.install_development_lint(root) == []
    assert (root / "AGENTS.md").read_text(encoding="utf-8").count(
        installer.LINT_START
    ) == 1


def test_install_conflicts_stop_before_any_write(tmp_path, installer):
    root = tmp_path / "app"
    (root / "tools").mkdir(parents=True)
    existing = root / "tools/development_lint.py"
    existing.write_text("mine\n", encoding="utf-8")
    with pytest.raises(ValueError, match="既存の検査設定"):
        installer.install_development_lint(root)
    assert existing.read_text(encoding="utf-8") == "mine\n"
    assert not (root / "AGENTS.md").exists()
    assert not (root / ".github").exists()


def test_partial_install_can_resume(tmp_path, installer):
    root = tmp_path / "app"
    installer.install_development_lint(root)
    (root / ".github/workflows/development-lint.yml").unlink()
    assert installer.install_development_lint(root) == [
        ".github/workflows/development-lint.yml"
    ]


def test_generated_lint_accepts_strings_and_functional_comments(repo):
    (repo / "app.py").write_text(
        '#!/usr/bin/env python3\n# SPDX-License-Identifier: MIT\nvalue = "# text"\nvalue = 1  # type: ignore[assignment]\n',
        encoding="utf-8",
    )
    result = run_lint(repo)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["checked_files"] >= 2


@pytest.mark.skipif(
    sys.version_info < (3, 10), reason="JavaScript解析器はPython3.10以降"
)
def test_generated_lint_accepts_encoding_and_typed_jsdoc(repo):
    (repo / "app.py").write_text('# coding: utf-8\nvalue = "説明"\n', encoding="utf-8")
    (repo / "app.js").write_text(
        "/** @type {number} */\nconst value = 1;\n", encoding="utf-8"
    )
    result = run_lint(repo)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "source",
    ["# 説明\nvalue = 1\n", 'def calculate():\n    """説明"""\n    return 1\n'],
)
def test_generated_lint_rejects_python_explanations(repo, source):
    (repo / "app.py").write_text(source, encoding="utf-8")
    result = run_lint(repo)
    assert result.returncode == 1
    assert "app.py:" in result.stderr


@pytest.mark.skipif(
    sys.version_info < (3, 10), reason="JavaScript解析器はPython3.10以降"
)
@pytest.mark.parametrize("suffix", [".js", ".jsx", ".ts", ".tsx"])
def test_generated_lint_uses_javascript_syntax(repo, suffix):
    source = repo / f"app{suffix}"
    source.write_text(
        'const url = "https://example.invalid/#text";\nconst regex = /\\/\\//;\n',
        encoding="utf-8",
    )
    result = run_lint(repo)
    assert result.returncode == 0, result.stderr
    source.write_text(
        source.read_text(encoding="utf-8") + "// 説明\n", encoding="utf-8"
    )
    result = run_lint(repo)
    assert result.returncode == 1
    assert "説明コメント" in result.stderr


def test_generated_lint_reports_unsupported_language(repo):
    (repo / "app.rs").write_text("fn main() {}\n", encoding="utf-8")
    result = run_lint(repo)
    assert result.returncode == 1
    assert "未対応" in result.stderr


def test_regression_rule_checks_cases_and_actual_source(repo):
    rules = repo / "tools/lint_rules"
    rule = rules / "unsafe_eval.py"
    rule.write_text(
        "def check(path, source):\n"
        '    if path.name == "app.py" and "eval(" in source:\n'
        '        return ["入力は安全な解析器で読み取ってください"]\n'
        "    return []\n",
        encoding="utf-8",
    )
    cases = {
        "invalid": [{"path": "app.py", "source": 'eval("1")'}],
        "valid": [{"path": "app.py", "source": "int('1')"}],
    }
    (rules / "unsafe_eval.cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (repo / "app.py").write_text("int('1')\n", encoding="utf-8")
    result = run_lint(repo)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["regression_rules"] == 1
    (repo / "app.py").write_text("eval('1')\n", encoding="utf-8")
    result = run_lint(repo)
    assert result.returncode == 1
    assert "安全な解析器" in result.stderr
    rule.write_text("def check(path, source):\n    return []\n", encoding="utf-8")
    result = run_lint(repo)
    assert result.returncode == 1
    assert "invalid" in result.stderr


def test_runtime_launcher_installs_from_canonical_root(tmp_path):
    root = SCRIPT.parents[1]
    target = tmp_path / "app"
    environment = dict(os.environ, REPO_PREFLIGHT_ROOT=str(root))
    result = subprocess.run(
        [
            sys.executable,
            str(root / "runtime/shared/run_preflight.py"),
            "--install-development-lint",
            "--repo",
            str(target),
            "--json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "READY"
    assert (target / "tools/development_lint.py").read_bytes() == (
        root / "scripts/development_lint.py"
    ).read_bytes()
