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
    # F10: 「図が無い」は Visualize、「図のラベルが英語」は Localize Diagram で分ける
    assert "Localize Diagram" not in report["recommended_capabilities"]


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


def _severities(report: dict, code: str) -> set[str]:
    return {
        str(item["severity"]) for item in report["findings"] if item["code"] == code
    }


def test_wide_command_cell_in_japanese_table_is_flagged(tmp_path: Path):
    # 横スクロールの実因。日本語の表なのに右列だけ極端に長いコマンドが入る形
    # F7: 検知はするが warning に留め、下流 repo を opt-out なしで止めない
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 使う場面\n\n| これからすること | コマンド |\n|---|---|\n"
        + f"| 公開する | `{LONG_COMMAND}` |\n",
    )

    report = MODULE.review(path)

    assert "table_command_cell_too_wide" in _codes(report)
    assert _severities(report, "table_command_cell_too_wide") == {"warning"}
    assert report["status"] == "pass"


def test_table_without_outer_pipes_and_escaped_pipe_is_flagged(tmp_path: Path):
    command = LONG_COMMAND + r" --filter left\|right"
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 使う場面\n\nこれからすること | コマンド\n--- | ---\n"
        + f"公開する | `{command}`\n",
    )

    report = MODULE.review(path)

    assert "table_command_cell_too_wide" in _codes(report)


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
    # F7: warning に留めて status は pass のまま
    # F10: 推薦語は "Localize Diagram"。"Visualize" は「図が無い」の意味で別用途
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 流れ\n\n```mermaid\nflowchart LR\n"
        + "    R[repository] --> S[readiness scan]\n"
        + "    S --> A[publication decision]\n```\n",
    )

    report = MODULE.review(path)

    assert "diagram_labels_not_localized" in _codes(report)
    assert _severities(report, "diagram_labels_not_localized") == {"warning"}
    assert report["status"] == "pass"
    assert "Localize Diagram" in report["recommended_capabilities"]
    assert "Visualize" not in report["recommended_capabilities"]


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


def test_each_mermaid_diagram_is_localized_independently(tmp_path: Path):
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n```mermaid\nflowchart LR\nA[日本語] --> B[確認]\n```\n"
        + "\n```mermaid\nflowchart LR\nC[English] --> D[Only]\n```\n",
    )

    assert "diagram_labels_not_localized" in _codes(MODULE.review(path))


def test_text_edge_and_asymmetric_node_labels_are_detected(tmp_path: Path):
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n```mermaid\nflowchart LR\nA>English node] -- English edge --> B\n```\n",
    )

    report = MODULE.review(path)

    assert "diagram_labels_not_localized" in _codes(report)
    assert report["metrics"]["diagram_label_count"] == 2


def test_er_cardinality_is_not_treated_as_an_english_label(tmp_path: Path):
    path = write_readme(
        tmp_path,
        JAPANESE_BODY + "\n```mermaid\nerDiagram\nA ||--|| B : 所有\n```\n",
    )

    assert "diagram_labels_not_localized" not in _codes(MODULE.review(path))


def test_inline_code_does_not_hide_japanese_document_classification(tmp_path: Path):
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 実行\n\n"
        + "説明です。 `"
        + ("x" * 600)
        + "`\n"
        + f"\n| 種別 | コマンド |\n|---|---|\n| 公開 | `{LONG_COMMAND}` |\n",
    )

    report = MODULE.review(path)

    assert report["metrics"]["japanese_document"] is True
    assert "table_command_cell_too_wide" in _codes(report)


def test_tilde_fenced_table_example_is_ignored(tmp_path: Path):
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n~~~markdown\n| 種別 | コマンド |\n|---|---|\n"
        + f"| 公開 | `{LONG_COMMAND}` |\n~~~\n",
    )

    assert "table_command_cell_too_wide" not in _codes(MODULE.review(path))


def test_this_repository_readme_meets_japanese_readability():
    # 自分自身が模範であることを固定する。劣化したらここが落ちる
    readme = Path(__file__).resolve().parents[1] / "README.md"

    report = MODULE.review(readme)

    assert "table_command_cell_too_wide" not in _codes(report)
    assert "diagram_labels_not_localized" not in _codes(report)
    assert report["metrics"]["japanese_document"] is True


# --- 2026-08-16 code review (11 findings) の残 6 件を固定する ---


def test_prose_cell_with_short_code_span_is_not_flagged(tmp_path: Path):
    # F2: 幅の実因は code span。折り返せる散文が長いだけのセルは止めない
    prose = (
        "比較元を指定すると、全体検査を保ったまま整合性の差分だけを絞り込みます。" * 2
    )
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 引数\n\n| 引数 | 説明 |\n|---|---|\n"
        + f"| 比較元 | {prose} `--consistency-base-ref` を使います。 |\n",
    )

    report = MODULE.review(path)

    assert "table_command_cell_too_wide" not in _codes(report)


def test_pipe_inside_mermaid_node_label_is_still_a_label(tmp_path: Path):
    # F4: {Yes|No} 形の分岐ラベルが消えて診断そのものが skip されないこと
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + '\n```mermaid\nflowchart TD\nA{Yes|No} --> B["Setting|Default"]\n```\n',
    )

    report = MODULE.review(path)

    assert report["metrics"]["diagram_label_count"] == 2
    assert "diagram_labels_not_localized" in _codes(report)


def test_flowchart_colon_in_node_label_does_not_leak_fragments(tmp_path: Path):
    # F5: sequence message の抽出が flowchart に当たってゴミ断片を作らないこと
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n```mermaid\nflowchart TD\nA[読込: file] --> B[書込: file]\n```\n",
    )

    report = MODULE.review(path)

    assert report["metrics"]["diagram_label_count"] == 2
    assert "diagram_labels_not_localized" not in _codes(report)


def test_indented_fence_after_h1_is_excluded_from_lead_summary(tmp_path: Path):
    # F6: 要約抽出とその他の検査で fence の判定規則がずれないこと
    body = (
        "# サンプル\n\n"
        "1. まず実行します。\n"
        "   ```bash\n" + ("   " + "長い説明が続きます。" * 30 + "\n") + "   ```\n"
        "2. 終わりです。\n\n"
        "## 目的\n\n公開判断を支援します。\n\n## できること\n\n- 状態を調べる\n\n"
        "## クイックスタート\n\n実行します。\n\n## 制約\n\n公開しません。\n"
    )
    report = MODULE.review(write_readme(tmp_path, body))

    assert "lead_summary_too_long" not in _codes(report)
    assert report["metrics"]["lead_summary_chars"] < 60


def test_click_and_subgraph_directives_are_not_labels(tmp_path: Path):
    # F8: click / subgraph / linkStyle の引用文字列をラベルとして数えない
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n```mermaid\nflowchart LR\n"
        + "subgraph Legend\n  A[開始] --> B[終了]\nend\n"
        + 'click A "https://example.invalid" "Open GitHub"\n'
        + "linkStyle 0 stroke:#f00\n```\n",
    )

    report = MODULE.review(path)

    assert report["metrics"]["diagram_label_count"] == 2
    assert "diagram_labels_not_localized" not in _codes(report)


def test_every_wide_command_cell_is_reported(tmp_path: Path):
    # F9: 1 件目で止まらず、全ての違反行を返す (エージェントの修正ループを 1 往復にする)
    path = write_readme(
        tmp_path,
        JAPANESE_BODY
        + "\n## 表1\n\n| 場面 | コマンド |\n|---|---|\n"
        + f"| 公開 | `{LONG_COMMAND}` |\n"
        + "\n## 表2\n\n| 場面 | コマンド |\n|---|---|\n"
        + f"| 公開 | `{LONG_COMMAND} --extra-flag-that-makes-it-longer` |\n",
    )

    report = MODULE.review(path)
    wide = [f for f in report["findings"] if f["code"] == "table_command_cell_too_wide"]

    assert len(wide) == 2
    assert [f["line"] for f in wide] == sorted(f["line"] for f in wide)
