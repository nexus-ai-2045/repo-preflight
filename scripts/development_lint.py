from __future__ import annotations

import ast
import importlib.util
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRAMMARS = {
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
}
UNSUPPORTED = {
    ".go",
    ".rs",
    ".java",
    ".swift",
    ".rb",
    ".lua",
    ".c",
    ".h",
    ".cpp",
    ".cs",
    ".html",
    ".css",
    ".scss",
    ".vue",
    ".svelte",
    ".sh",
    ".ps1",
    ".ipynb",
}
DIRECTIVE = re.compile(
    r"^(?:type:\s*ignore(?:\[[^\]]+\])?|noqa(?:\s*:\s*[A-Z][A-Z0-9, ]*)?"
    r"|@ts-(?:check|nocheck|expect-error|ignore)\b[^\n]*"
    r"|eslint-(?:disable|enable)(?:-next-line|-line)?\b[^\n]*"
    r"|(?:istanbul|c8)\s+ignore\s+(?:next|file|start|stop|if|else)\b[^\n]*)$"
)
LICENSE = re.compile(r"^(?:SPDX-License-Identifier:|Copyright\b|@license\b)", re.I)


def functional_comment(text: str, line: int) -> bool:
    if line == 1 and text.startswith("#!"):
        return True
    if line <= 2 and re.fullmatch(r"#.*?coding[:=]\s*[-\w.]+\s*", text):
        return True
    if text.startswith("/**") and re.search(
        r"@(?:type|param|returns?|typedef|extends|implements)\s*\{", text
    ):
        return True
    content = re.sub(r"^(?:#|//|/\*+)\s*", "", text).removesuffix("*/").strip()
    return bool(DIRECTIVE.fullmatch(content) or LICENSE.match(content))


def python_comments(source: str) -> list[str]:
    errors = []
    tree = ast.parse(source)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and not functional_comment(
            token.string, token.start[0]
        ):
            errors.append(
                f"{token.start[0]}: 説明コメントを命名・構造・テストまたは文書へ移してください"
            )
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    errors.append(
                        f"{value.lineno}: 説明専用の文書文字列を文書へ移してください"
                    )
    return errors


def javascript_comments(source: str, grammar: str) -> list[str]:
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:
        raise ValueError(
            "JavaScript/TypeScriptの解析器が必要です。python3 -m pip install -r tools/development-lint-requirements.txt を実行してください"
        ) from exc
    raw = source.encode("utf-8")
    tree = get_parser(grammar).parse(raw)
    if tree.root_node.has_error:
        raise ValueError(
            "構文を解析できません。対象言語の構文検査と解析器の対応を確認してください"
        )
    errors = []
    pending = [tree.root_node]
    while pending:
        node = pending.pop()
        if node.type in {"comment", "hash_bang_line"}:
            text = raw[node.start_byte : node.end_byte].decode("utf-8")
            line = node.start_point.row + 1
            if not functional_comment(text, line):
                errors.append(
                    f"{line}: 説明コメントを命名・構造・テストまたは文書へ移してください"
                )
        pending.extend(reversed(node.children))
    return errors


def run_rule(rule, path: str, source: str) -> list[str]:
    result = rule.check(Path(path), source)
    if not isinstance(result, list) or any(
        not isinstance(item, str) or not item for item in result
    ):
        raise ValueError(
            f"{rule.__name__}: check(path, source) は空でない診断文字列のリストを返してください"
        )
    return result


def load_rules() -> list:
    rules = []
    for script in sorted((ROOT / "tools/lint_rules").glob("*.py")):
        spec = importlib.util.spec_from_file_location(
            f"development_rule_{script.stem}", script
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"{script.name}: ルールを読み込めません")
        rule = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rule)
        if not callable(getattr(rule, "check", None)):
            raise ValueError(f"{script.name}: check(path, source) が必要です")
        cases = json.loads(
            script.with_suffix(".cases.json").read_text(encoding="utf-8")
        )
        if not isinstance(cases, dict) or set(cases) != {"invalid", "valid"}:
            raise ValueError(f"{script.name}: invalid と valid の検証例が必要です")
        for name, expected in (("invalid", True), ("valid", False)):
            if not isinstance(cases[name], list) or not cases[name]:
                raise ValueError(f"{script.name}: {name} は1件以上必要です")
            for case in cases[name]:
                if not isinstance(case, dict) or set(case) != {"path", "source"}:
                    raise ValueError(
                        f"{script.name}: 検証例には path と source が必要です"
                    )
                if not all(isinstance(case[key], str) for key in ("path", "source")):
                    raise ValueError(
                        f"{script.name}: 検証例の path と source は文字列にしてください"
                    )
                if bool(run_rule(rule, case["path"], case["source"])) != expected:
                    raise ValueError(
                        f"{script.name}: {name} の検証結果が期待と異なります"
                    )
        rules.append(rule)
    return rules


def source_paths() -> list[Path]:
    repository = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    if Path(repository.stdout.strip()).resolve() != ROOT:
        raise ValueError(
            "対象ディレクトリを独立したGitリポジトリにしてから検査してください"
        )
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [
        Path(item.decode("utf-8"))
        for item in sorted(set(result.stdout.split(b"\0")))
        if item
    ]


def main() -> int:
    errors = []
    checked = 0
    rules = []
    try:
        rules = load_rules()
        for relative in source_paths():
            path = ROOT / relative
            if path.is_symlink():
                if relative.suffix in {".py", *GRAMMARS, *UNSUPPORTED}:
                    errors.append(
                        f"{relative}: ソースのシンボリックリンクは検査できません"
                    )
                continue
            if not path.is_file():
                continue
            suffix = relative.suffix.lower()
            if suffix in UNSUPPORTED:
                errors.append(
                    f"{relative}: コメント検査が未対応の言語です。構文解析器と正常例・違反例を追加してください"
                )
                continue
            if suffix not in {".py", *GRAMMARS}:
                continue
            checked += 1
            source = path.read_text(encoding="utf-8")
            try:
                findings = (
                    python_comments(source)
                    if suffix == ".py"
                    else javascript_comments(source, GRAMMARS[suffix])
                )
                errors.extend(f"{relative}:{finding}" for finding in findings)
                for rule in rules:
                    errors.extend(
                        f"{relative}: {finding}"
                        for finding in run_rule(rule, relative.as_posix(), source)
                    )
            except (SyntaxError, tokenize.TokenError, ValueError) as exc:
                errors.append(f"{relative}: {exc}")
    except Exception as exc:
        errors.append(f"検査を完了できませんでした。{type(exc).__name__}: {exc}")
    for error in errors:
        print(error, file=sys.stderr)
    print(
        json.dumps(
            {
                "ok": not errors,
                "checked_files": checked,
                "regression_rules": len(rules),
            },
            ensure_ascii=False,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
