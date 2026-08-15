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


JAPANESE_BODY = """# サンプル

サンプルは、人へ見せる直前の確認を短時間で終える読み取り専用のツールです。

## 目的

公開してよいかの判断と、テストが通ったことを分けて扱います。

## できること

- リポジトリの状態を調べる
- 結果を決まった形式で返す

## クイックスタート

説明のとおりに実行します。

## 制約

公開や外部への送信そのものは行いません。
"""

LONG_COMMAND = (
    "python scripts/readiness_scan.py --repo PATH --intent publish "
    "--audience public --consistency-base-ref origin/BASE --human"
)


def _codes(report: dict) -> set[str]:
    return {str(item["code"]) for item in report["findings"]}


def test_wide_command_cell_in_japanese_table_is_flagged(tmp_path: Path):
    # 横スクロールの実因。日本語の表なのに右列だけ極端に長いコマンドが入る形
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 使う場面\n\n| これからすること | コマンド |\n|---|---|\n"
        + f"| 公開する | `{LONG_COMMAND}` |\n",
    )

    report = MODULE.review(path)

    assert "table_command_cell_too_wide" in _codes(report)
    assert report["status"] == "blocked"


def test_long_link_cell_is_not_flagged(tmp_path: Path):
    # 誤検知の防止。Markdownリンクはfile名を2度書くため長くなるが、読みにくさとは別
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 文書\n\n| 種別 | 入口 |\n|---|---|\n"
        + "| 確認記録 | [PUBLIC_RELEASE_CHECKLIST.md](PUBLIC_RELEASE_CHECKLIST.md)"
        + " / [PUBLIC_READY.md](PUBLIC_READY.md) |\n",
    )

    report = MODULE.review(path)

    assert "table_command_cell_too_wide" not in _codes(report)


def test_english_readme_skips_japanese_readability_checks(tmp_path: Path):
    # 英語のREADMEへ日本語向けの基準を適用しない
    path = write_readme(
        tmp_path,
        f"""# Sample

Sample is a read-only tool that inspects a repository before you widen its audience.

## Purpose

Separate what the machine verified from what a human must decide.

## Features

- Inspect repository state
- Return findings as structured output

## Quickstart

Run the command below.

| Action | Command |
|---|---|
| publish | `{LONG_COMMAND}` |

## Limitations

It never publishes or pushes anything.
""",
    )

    report = MODULE.review(path)

    assert "table_command_cell_too_wide" not in _codes(report)
    assert "diagram_labels_not_localized" not in _codes(report)


def test_diagram_with_english_only_labels_is_flagged(tmp_path: Path):
    # 日本語の文書なのに図のラベルだけ英語の識別子、という状態を拾う
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 流れ\n\n```mermaid\nflowchart LR\n"
        + "    R[repository] --> S[readiness scan]\n"
        + "    S --> A[publication decision]\n```\n",
    )

    report = MODULE.review(path)

    assert "diagram_labels_not_localized" in _codes(report)


def test_diagram_with_japanese_labels_passes(tmp_path: Path):
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 流れ\n\n```mermaid\nflowchart LR\n"
        + "    R[調べたいリポジトリ] --> S[検査]\n"
        + "    S --> A[人が判断する範囲]\n```\n",
    )

    report = MODULE.review(path)

    assert "diagram_labels_not_localized" not in _codes(report)
    assert report["status"] == "pass"


def test_diagram_with_english_edge_label_is_flagged(tmp_path: Path):
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 流れ\n\n```mermaid\nflowchart LR\n"
        + "    A -->|English decision| B\n```\n",
    )

    report = MODULE.review(path)

    assert "diagram_labels_not_localized" in _codes(report)
    assert report["metrics"]["diagram_label_count"] == 1


def test_sequence_diagram_with_english_message_is_flagged(tmp_path: Path):
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 流れ\n\n```mermaid\nsequenceDiagram\n"
        + "    User->>Gate: Check repository\n```\n",
    )

    report = MODULE.review(path)

    assert "diagram_labels_not_localized" in _codes(report)
    assert report["metrics"]["diagram_label_count"] == 1


def test_this_repository_readme_meets_japanese_readability():
    # 自分自身が模範であることを固定する。劣化したらここが落ちる
    readme = Path(__file__).resolve().parents[1] / "README.md"

    report = MODULE.review(readme)

    assert "table_command_cell_too_wide" not in _codes(report)
    assert "diagram_labels_not_localized" not in _codes(report)
    assert report["metrics"]["japanese_document"] is True
