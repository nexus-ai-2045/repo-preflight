import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readme_release_gate.py"
SPEC = importlib.util.spec_from_file_location("readme_release_gate", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def write_readme(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "README.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_concise_reader_first_readme_passes(tmp_path: Path):
    path = write_readme(
        tmp_path,
        """# Sample

Sampleは、release確認を短時間で行うためのread-onlyツールです。

## 目的

公開判断とテスト成功を分離します。

## できること

- 状態を検査する
- JSONで結果を返す

## クイックスタート

```powershell
python scripts/check.py --json
```

## 制約

公開や外部送信は行いません。
""",
    )
    report = MODULE.review(path)
    assert report["status"] == "pass"
    assert report["release_gate"] == "passed_readme_design"
    assert report["external_actions_performed"] is False


def test_missing_quickstart_and_limits_routes_to_design_tools(tmp_path: Path):
    path = write_readme(
        tmp_path,
        """# Sample

## Features

This repository contains a comprehensive next-generation solution.
""",
    )
    report = MODULE.review(path)
    assert report["status"] == "blocked"
    assert "Template Creator" in report["recommended_capabilities"]
    assert "Product Design" in report["recommended_capabilities"]


def test_heading_jump_and_excess_length_block_release_gate(tmp_path: Path):
    body = (
        "# Sample\n\nShort summary.\n\n## Why\n\nReason.\n\n#### Deep\n\n"
        + "\n".join(f"line {index}" for index in range(340))
    )
    report = MODULE.review(write_readme(tmp_path, body))
    codes = {finding["code"] for finding in report["findings"]}
    assert "heading_level_jump" in codes
    assert "readme_too_long" in codes


def test_visualize_is_suggested_only_for_complex_flow_without_diagram(tmp_path: Path):
    path = write_readme(
        tmp_path,
        """# Workflow

Workflowを説明するツールです。

## 目的
処理順序を共有します。

## できること
- 設計
- 実装

## クイックスタート
```text
run
```

## ワークフロー
1. Discover
2. Design
3. Build
4. Verify
5. Release

## 制約
外部操作は承認制です。
""",
    )
    report = MODULE.review(path)
    assert "Visualize" in report["recommended_capabilities"]


def test_public_word_does_not_false_positive_as_ui(tmp_path: Path):
    path = write_readme(
        tmp_path,
        """# Public Tool

Public Toolは公開準備を確認するread-onlyツールです。

## 目的
公開判断を支援します。
## できること
- 状態を検査します。
## クイックスタート
```text
run
```
## 判定の限界
公開操作は行いません。
""",
    )
    report = MODULE.review(path)
    assert report["status"] == "pass"
    assert "Figma or frontend-design" not in report["recommended_capabilities"]
