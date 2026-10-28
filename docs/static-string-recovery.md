# 言語間で異なる文字列の静的検査

## 問題と採用方針

PowerShell の単一引用符内ではバックスラッシュは通常の文字になる。
これを Python の式として解析すると、Windows パスの `\u` が不完全な
Unicode エスケープと判定され、個人情報の検査が未完了になる場合がある。
反対に `\t` などは Python で解析に成功しても、元言語とは異なる値になる。
例外が出た時だけの対応では、この見逃しを防げない。

既存の Python 向けの検査を維持し、PowerShell の単一引用符で表せる
静的な文字列連結について、別の解釈で得られる値も検査する。
どちらかの解釈で個人情報が見つかれば拒否する。
原文の検査、出典との対応、動的な式と関数呼出しの境界を維持し、
解析不能な候補を無視して成功扱いにしない。

追加した言語の解釈が失敗した場合も、元のPython構文として有効かを先に
確かめる。有効なら既存のPythonの値・個人情報検査を継続する。
Pythonとしても解析できず、対応した追加解釈でも復元できない場合は停止する。
追加解析の失敗だけで、元から扱えていたPythonの検査を止めない。

追加解釈の候補は式ごとに保持する。後続の無関係な式で解析に失敗しても、
先行する式や静的な呼出しの内側で復元した値を破棄しない。
文書全体がPython構文にならない場合も、既存の式の回復結果と失敗位置を
出典の範囲で照合する。失敗位置を説明できない部分は未完了として停止し、
原文や正常に復元できた別の範囲を検査対象から除かない。
復元済みのPython式はASTで区切った範囲内で追加解釈も検査し、外側の
回復解析では追加文法と両立しない範囲だけを同じ長さの空白へ置換する。これにより、その式の引用符が
後続の無関係なPowerShell式を巻き込まず、出典の位置も維持する。

## 調べた一次資料

- [Microsoft: about_Quoting_Rules](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules?view=powershell-7.5)
  は単一引用符内で置換しないこと、引用符を二重にして表すこと、
  二重引用符では変数展開とバッククォートの規則があることを説明している。
- [Python: ast](https://docs.python.org/3/library/ast.html)
  は Python の構文木を扱う API であり、他言語の構文を解釈する API ではない。
  `literal_eval` も無制限な非信頼入力に安全な実行器とは扱えない。
- [Semgrep: generic pattern matching](https://docs.semgrep.dev/writing-rules/generic-pattern-matching)
  は言語非依存の字句照合が元言語の構文や符号化を理解せず、検出品質に限界が
  あることを説明している。汎用の文字列照合だけへの置換は採用しない。
- [Tree-sitter: ERROR / MISSING nodes](https://tree-sitter.github.io/tree-sitter/using-parsers/queries/1-syntax.html)
  は構文木に失敗位置も保持する設計を示している。
  [PowerShellのParseInput](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.parser.parseinput?view=powershellsdk-7.4.0)
  も構文木・トークン・エラーを別々に返す。今回も成功した候補を失敗位置と
  分けて保持する考え方を採用し、既存の静的検査へ集約する。

以上から、例外の握りつぶし、検査対象ファイルの除外、対象文字列の実行、
検査を通すためだけの対象コードの書換えは採用しない。
依存する解析エンジンを増やさず、既存の静的連結の検査へ修正を集約する。

## 回帰検査と保証の範囲

実際に失敗した Windows パスに加え、Python で別の値になるエスケープ、
引用符、括弧、コメント、分割された個人識別子、動的な式との境界を検査する。
不正な構文の拒否と、既存の計算量の回帰検査も維持する。

架空のホームパスの正例は、既存の `tests/synthetic_paths.py` で実行時に
組み立てる。検出対象の値を弱めず、公開するソースへホームパスの形を
直接追加しない。PR前にはCIと同じ `scripts/identity_gate.py --public`
で送信する全コミットを確認する。最終ファイルだけを修正しても、途中の
コミットに禁止された形があれば検査は停止する。

これは PowerShell 全体の意味解析ではない。変数展開、コマンド実行、
任意の動的な式の評価は行わない。既知の未対応形式を、検査済みの静的な値と
同一視しない。元言語の規則が必要な追加不具合は、再現ケースと検出漏れの
負例を既存のテストへ追加してから修復する。
