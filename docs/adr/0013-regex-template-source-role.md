# ADR-0013: 検査用regexのusername classを実pathと区別する

- status: Accepted (親担当が差分と実反例を独立レビュー、2026-10-24)
- date: 2026-10-24
- scope: source roleの誤判定修正。private literal・具体pathの新規免除は追加しない。

## 問題

既存AST復元は、検査コードのregexを通常文字列と同じpath文法へ渡していた。その結果、usernameの汎用character classを実usernameと解釈し、正本の検査スクリプト自身を止めていた。

## 実装の境界

入力全体が有効なPython sourceで、トップレベルに明示的な`import re`があり、名前の再束縛、module属性の変更、module alias、動的名前空間操作が見つからない場合に限り、`re.compile`の単一位置引数をrole候補とする。sourceは実行しない。既存AST評価で完全な文字列へ復元し、標準regex compilerによる構文検証に成功する必要がある。文字列連結と、静的な文字列式だけのlist/tupleをjoinする形に対応する。公開`static_string_views`のvalue/source二要素tuple APIは維持する。role判定で新たに全体評価したregexは、URL表記を戻した値も含めてprivate literal・具体pathの追加照合へ渡す。list要素の境界をまたぐ検出を省略しない。

username位置が`[^/\s]+`、backslashを除外する同型class、`[A-Za-z0-9._-]+`のいずれかであるpath spanだけを検査用の文法として識別する。既存placeholder除外lookaheadの一形式だけも扱う。classの直後にusernameを構成するliteralや未対応構文が続く場合は識別しない。regex全体・file全体の免除は作らない。原文では当該引数のstring token内だけを区別し、コメントや別の文字列は維持する。

原文・静的復元・配列連結等の全viewについてprivate literal照合を従来どおり行う。具体usernameを持つregex branch、同じsource内の実path、その他のcall、動的引数、不正regex、shadowing、解析不能sourceは従来のpath検査を維持する。regexが65,536字を超える場合もroleを付けず、従来の全文検査へ戻す。検査を省略したり成功へ変えたりしない。

## 検証

元の正本スクリプトの三形式とNote validator形式を正例として確認し、具体username・別path・コメント・private literal・source role不在・名前や属性の変更・動的/不正regexを拒否例として確認する。Git配送・hook反映・最終採用はこの局所実装の検証とは別に扱う。画像、名称、既存の媒体方針、公式生成manifestはこの修正で変更しない。
