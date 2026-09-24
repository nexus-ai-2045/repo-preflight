# repo-preflight AI憲法入口契約

## 目的

共通原則の正本と、各AIが実際に読む入口を混同しないための契約です。

正本は1つだけ持ち、runtimeごとの差は入口戦略として宣言します。

このmanifestは、利用環境が採用する入口の集合を宣言するものであり、
`repo-preflight` CLIの実行依存を意味しません。`required` は採用したmanifest内の
entry単位にだけ適用されます。未導入または今回の環境で使わないruntimeは、entryを
manifestから省略するか、`required: false` として任意entryにします。`assets/` の
exampleは対応runtimeを並べた全体例であり、全runtimeを自動的に有効化する設定ではありません。

| 戦略 | 意味 | 適用例 |
|---|---|---|
| `pointer` | runtime固有のsource pointer/importまたは明示的な読込指示を入口に置く | Codex、Claude Code、Gemini CLI |
| `materialized` | source本文を生成投影し、hashと本文を検査する | Grok |
| `manual` | 製品UIや未確認仕様に依存し、機械的に完了扱いしない | Cursor User Rules |

`@` は製品ごとに意味が異なります。manifestの`pointer_kind`で、`import`（`@` import）と
`instruction`（Codexのように入口が正本を先に読む明示指示）を区別します。Grok/Cursorの
ファイル添付・rule参照を同じ構文として扱いません。

`instruction` は正本パスがinline codeに現れるだけでは合格にしません。パスの同一行または
前後2行以内に、正本を示す主語、必須・先行条件、読む・参照する動詞を含む肯定的な読込指示が
必要です。`読まない`、`do not read` などの否定形はfail-closedで不合格にします。
`instruction` の判定はinline code内のパスを使うため、inline codeは除外しません。

`import` の判定は文字列の部分一致ではなく、`@<path>` トークンをパスとして解決し、sourceとの
同一性 (`samefile`) で比較します。絶対パス、`~/` 表記 (gateに渡した `--home` で展開し、実行
ユーザーの実homeは見ません)、entryファイルのディレクトリからの相対パスを受理します。行頭または
空白直後の `@` だけをimportとみなし、コードフェンス内・inline code内・HTMLコメント内の例示や
`@<source>.backup` のような別パスはimportとして扱いません。コードの範囲はCommonMarkに従います。
フェンスは開始と同じ文字・同じ以上の長さの行でだけ閉じ (`~~~` の中の ```` ``` ```` や、
```` ```` ```` の中の ```` ``` ```` では閉じない)、閉じないフェンスは文書末までコードとみなします。
段落の途中ではない4桁以上の字下げ行 (インデントコードブロック) と、同じ長さのbacktick列で
閉じるinline code (``` ``@~/AI.md`` ``` のような2連backtickを含む) も除外します。blockquoteの
`>` は剥がして中身を同じ規則で読みます。大文字小文字の同一視はOS名ではなく
ファイルシステムの実際の解決に従います。

manifestのパスは `{HOME}` / `{PROJECT}` placeholderだけを解決します。`~` で始まるパスは
`--home` の差し替えを迂回して実行ユーザーの実homeへ解決されるため
`tilde_unsupported_use_home_placeholder` で拒否します。未知のplaceholder (`{USERPROFILE}` 等) は
`template_placeholder_unresolved`、空のパスやNUL文字を含むパスは `path_value_invalid` です。entryのパス解決に
失敗した場合は、manifest自体の問題としてレポート全体を `tool_error` にします。

## 検査

```powershell
py -3.13 scripts/ai_entry_contract.py `
  --manifest assets/ai-entry-contract.example.json
```

既定は読み取り専用です。出力は常にJSONです。結果が `blocked` のときは、未確認のruntimeを「対応済み」と扱いません。

exit codeは結果の種類を区別します。

| exit | status | 意味 |
|---|---|---|
| 0 | `pass` | 全required entryが整合 |
| 1 | `blocked` | drift・stale・missing (行動が必要) |
| 2 | `tool_error` | manifest不正・実行失敗 (gate自体の問題) |
| 3 | `human_review` | requiredな失敗がmanual entryの人手確認待ちだけ |

required entryの失敗が全て `human_review` のときだけ全体を `human_review` にし、drift等が1件でも
混ざれば `blocked` です。manual entryの結果には確認先として `evidence` を載せます。同梱exampleは
Cursorのmanual entryを含むため、機械検証で到達できる最良は exit 3 です。CIでgateにする場合は 0 と 3 を
許容するか、manual entryを `required: false` にした運用manifestを使います。

`--entry-id` は `--apply` なしでは受け付けず `entry_id_requires_apply` を返します。entry単位の
read-only検査は提供していないため、全件検査が黙って走って「1件だけ確認した」と誤読されるのを防ぎます。

`required: false` のentryが `missing`、`stale`、または `human_review` でも、
そのentryだけを理由にmanifest全体を `blocked` にはしません。ただし、reportには
entryごとの状態を残すため、任意entryの未整備を見えない成功として扱うことはできません。

## 投影の更新

`materialized` targetの作成・更新は、対象を1件指定した明示操作だけ許可します。

```powershell
py -3.13 scripts/ai_entry_contract.py `
  --manifest <manifest.json> `
  --entry-id grok-global `
  --apply
```

成功時のレポートには `applied_entry` が付き、書き込みが完了したentryを他entryの状態やexit codeと
独立に識別できます (manual entryが残るmanifestではapply成功でもexitは3になります)。

次の安全境界があります。

- `--apply` には `--entry-id` が必須
- `pointer` と `manual` は自動変更しない
- 既存の未生成ファイルは上書きしない
- 生成範囲はsource hashとbegin/end markerで検査する
- header・begin・end markerは各1個・この順序で、headerとbeginの間は空白だけでなければならない。marker類似文字列を引用したoverlay、複製ブロック、headerとbeginの間の追記は、破壊や見逃しを防ぐため `projection_markers_ambiguous` として停止する (検査は `stale`、applyは書かずに `tool_error`)
- source自身がmarkerを含む場合は投影できないため `source_contains_projection_markers` で停止する
- markerと本文の区切り改行は1個だけ扱い、source先頭の空行やoverlay側の空行は保存する
- sourceとtargetが同一ファイルに解決される場合は `source_target_identical` で拒否する
- 既存targetを置き換えるときはファイルモードを引き継ぐ
- レポートにsource本文や絶対パスを載せない。自前定義のcode (gate内の専用例外型) 以外の例外は
  型名だけに丸める。自前codeかどうかは例外の型で判定するため、entry idに `/` や `\` を含む
  manifestでも `manifest_runtime_missing:<id>` などのfinding名は保たれる
- 書き込み時の例外 (`OSError` に加え、不正パスによる `ValueError`) は
  `target_write_failed:<型名>` にする。想定外の例外もtracebackを出さず
  `internal_error:<型名>` のJSONとexit 2で返す
- runtime設定、認証、Cursor/GrokのUI設定は変更しない

## 現在の判断

- Claude Codeはpointer戦略を使えるが、live checkoutがremoteのマージ結果へ同期済みかは別に検査する。
- Codexは階層入口の明示的な正本読込指示を`pointer_kind=instruction`で検査する。本文を複製する`materialized`とは区別する。
- Grokは認識済みの`AGENTS.md`/`CLAUDE.md`を読むが、Claude Codeと同じMarkdown importを前提にしない。Grokはmaterialized戦略で明示的に検証する。
- Cursorのglobal User Rulesは製品UI経路を含むため、ファイル存在だけで完了扱いせず、manual evidenceで止める。project `.cursor/rules` を採用する場合は、project scopeの別manifestを作る。

## 先行実装との関係

独自概念を作らないため、各要素の既存慣行を確認しています。

| 本契約の要素 | 対応する既存概念 | 差分 |
|---|---|---|
| marker で囲んだ生成ブロック | Ansible `blockinfile` の managed block (`# BEGIN ANSIBLE MANAGED BLOCK`) | 慣行どおり。Ansible は marker の一意性を呼び出し側責任にしているが、本契約は検査側で一意性を強制する |
| source hash をヘッダに埋めた drift 検出 | 先行なし (codegen の `DO NOT EDIT` は宣言のみで改変検知を持たない) | 本契約の拡張 |
| `pointer` / `materialized` | dotfile 管理の symlink strategy / copy strategy (chezmoi 等) | 対応する。ただし `materialized` はDBのmaterialized viewと違い自動再計算はしない |
| `manual` | 先行なし (dotfile 管理は常に機械書込可を前提とするため) | 本契約の区分 |
| exit code 0/1/2/3 | `terraform plan -detailed-exitcode` 等の「0=合否, 1=内容起因, 2=ツール起因」慣行 | 同系統。`sysexits.h` (64番台) とは別体系。3=human_review は本契約の拡張で、深刻度でなく対応主体で並べている |
| marker 曖昧時の停止 | fail-closed (fail-secure) | 標準用語どおり。可用性より破壊防止を優先する意味で fail-safe ではない |

## 保証境界

この契約が保証するのは、sourceと宣言された入口の存在・pointer・生成本文・hashの整合です。AI製品が毎回同じ推論をすること、UI設定が有効であること、mergeや公開を許可することは保証しません。
