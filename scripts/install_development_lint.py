from __future__ import annotations

import argparse
import json
from pathlib import Path

LINT_FILES = (
    "tools/development_lint.py",
    "tools/development-lint-requirements.txt",
    "tools/lint_rules/README.md",
    ".github/workflows/development-lint.yml",
)


LINT_START = "<!-- development-lint:start -->"


LINT_END = "<!-- development-lint:end -->"


CONTRIBUTING_BASE = (
    "# コントリビューション\n\n"
    "- 挙動を変える時は失敗する test を先に追加する。\n"
    "- secret / token / 個人の絶対 path を commit しない。\n"
    "- main へ直接 push しない。branch を切って PR を出す。\n"
)


LINT_INSTRUCTIONS = (
    LINT_START + "\n"
    "## 開発開始時とバグ修正時の検査\n\n"
    "説明コメント、コメントアウトした旧コード、説明専用の文書文字列は禁止します。"
    "命名・構造・テストで処理を表し、設計理由は文書へ移します。"
    "ライセンス・実行環境指定・型検査や静的検査の指示は用途を確認した例外です。\n\n"
    "開発開始時に `python3 -m pip install -r tools/development-lint-requirements.txt` を実行し、"
    "`python3 tools/development_lint.py` が通ることを確認します。ローカルと継続検査で同じ入口を使います。\n\n"
    "バグを修正したら、構造・型・既存の静的検査で同じ原因を防げるか確認します。"
    "静的に判定できる誤りには `tools/lint_rules/` へ対象を絞ったルールを追加し、"
    "元の違反例と近い正常例を同名の `.cases.json` に残します。"
    "実行順序や結果に依存するバグはアプリの回帰テストで再現します。\n" + LINT_END + "\n"
)


def development_lint_templates() -> dict[str, str]:
    script = Path(__file__).with_name("development_lint.py")
    return {
        "tools/development_lint.py": script.read_text(encoding="utf-8"),
        "tools/development-lint-requirements.txt": "tree-sitter-language-pack==0.13.0\ntree-sitter==0.25.2\n",
        "tools/lint_rules/README.md": (
            "# バグの再発防止ルール\n\n"
            "ルールは `rule_name.py` に `check(path, source)` を定義します。"
            "引数はリポジトリ相対の `pathlib.Path` とソース文字列です。"
            "違反には修正方法を含む診断文字列のリストを、正常時は空のリストを返します。\n\n"
            "同名の `rule_name.cases.json` に `invalid` と `valid` の配列を置きます。"
            "各要素は相対パスの文字列 `path` とソース文字列 `source` を持ちます。"
            "元のバグと近い正常コードを1件以上ずつ入れます。"
            "`python3 tools/development_lint.py` は毎回この両方を検証してからソースを検査します。\n\n"
            "初期のコメント検査は `Python`、`JavaScript`、`TypeScript`、`JSX`、`TSX` に対応します。"
            "その他の言語は、採用時に構文解析器と正常例・違反例を追加して確認します。"
            "実行時に読むヘルプ等の文書文字列が必要なら、その用途のテストと対象を限定した例外判定を同じ変更で加えます。\n"
        ),
        ".github/workflows/development-lint.yml": (
            "name: 開発規約の検査\non:\n  pull_request:\n  push:\n"
            "permissions:\n  contents: read\njobs:\n  development-lint:\n"
            "    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n"
            "      - run: python3 -m pip install -r tools/development-lint-requirements.txt\n"
            "      - run: python3 tools/development_lint.py\n"
        ),
    }


def install_development_lint(root: Path) -> list[str]:
    root = root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("導入先はディレクトリにしてください")
    templates = development_lint_templates()
    compile(templates["tools/development_lint.py"], "tools/development_lint.py", "exec")
    for filename, text in templates.items():
        target = root / filename
        for ancestor in (target, *target.parents):
            if ancestor == root:
                break
            if ancestor.is_symlink():
                raise ValueError(
                    f"検査設定の導入先がシンボリックリンクです: {filename}"
                )
            if ancestor != target and ancestor.exists() and not ancestor.is_dir():
                raise ValueError(
                    f"検査設定の親パスがディレクトリではありません: {filename}"
                )
        if target.exists() and (
            not target.is_file() or target.read_text(encoding="utf-8") != text
        ):
            raise ValueError(
                f"既存の検査設定を保持しました。差分をレビューして統合してください: {filename}"
            )
    for filename, heading in (
        ("AGENTS.md", "# 開発ルール"),
        ("CONTRIBUTING.md", "# コントリビューション"),
    ):
        target = root / filename
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"既存文書へ安全に追記できません: {filename}")
        initial = CONTRIBUTING_BASE if filename == "CONTRIBUTING.md" else heading + "\n"
        text = target.read_text(encoding="utf-8") if target.exists() else initial
        if LINT_START in text or LINT_END in text:
            if (
                text.count(LINT_START) != 1
                or text.count(LINT_END) != 1
                or LINT_INSTRUCTIONS not in text
            ):
                raise ValueError(
                    f"既存の開発ルールを保持しました。管理ブロックを確認してください: {filename}"
                )
        else:
            templates[filename] = text.rstrip() + "\n\n" + LINT_INSTRUCTIONS
    written = []
    for filename, text in templates.items():
        target = root / filename
        if target.exists() and target.read_text(encoding="utf-8") == text:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(filename)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="アプリのコメント禁止と再発防止の検査を導入する"
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        written = install_development_lint(args.repo)
        result = {"status": "READY", "written": written}
    except (OSError, ValueError) as exc:
        result = {"status": "BLOCKED", "cause": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
