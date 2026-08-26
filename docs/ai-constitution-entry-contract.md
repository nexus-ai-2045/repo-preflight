# repo-preflight AI憲法入口契約

## 目的

共通原則の正本と、各AIが実際に読む入口を混同しないための契約です。

正本は1つだけ持ち、runtimeごとの差は入口戦略として宣言します。

| 戦略 | 意味 | 適用例 |
|---|---|---|
| `pointer` | runtimeが文書内のsource pointer/importを解釈する | Claude Code、Gemini CLI |
| `materialized` | source本文を生成投影し、hashと本文を検査する | Codex、Grok |
| `manual` | 製品UIや未確認仕様に依存し、機械的に完了扱いしない | Cursor User Rules |

`@` は製品ごとに意味が異なります。Claude Code/Geminiのimportとして使える入口と、Grok/Cursorのファイル添付・rule参照を同じ構文として扱いません。

pointer検出は文字列の部分一致ではなく、`@<path>` トークンをパスとして解決してsourceとの同一性 (`samefile`) で判定します。絶対パス・`~/` 表記・entryファイルからの相対パスを受理し、コードフェンス内・インラインコード内・HTMLコメント内の例示はimportとして扱いません。大文字小文字はファイルシステムの実際の解決に従います (case-insensitiveなファイルシステムでは一致し、case-sensitiveでは一致しない)。

パスは `{HOME}` / `{PROJECT}` placeholder だけを解決します。`~` は `--home` の差し替えを迂回して実行ユーザーの実homeに解決されてしまうため、fail-closedで拒否します。

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
| 3 | `human_review` | requiredなmanual entryの人手確認だけが残っている |

同梱exampleはCursorのmanual entryを含むため、機械検証で到達できる最良は exit 3 です。CIでgateにする場合は 0 と 3 を許容するか、manual entryを `required: false` にした運用manifestを使います。`--entry-id` を `--apply` なしで渡すことはできません (entry単位のread-only検査は提供していないため、全件検査が黙って走って「1件だけ確認した」と誤読されるのを防ぎます)。

## 投影の更新

`materialized` targetの作成・更新は、対象を1件指定した明示操作だけ許可します。

```powershell
py -3.13 scripts/ai_entry_contract.py `
  --manifest <manifest.json> `
  --entry-id grok-global `
  --apply
```

成功時のレポートには `applied_entry` が付き、書き込みが完了したentryを他entryの状態やexit codeと独立に識別できます (manual entryが残るmanifestではapply成功でもexitは3になります)。

次の安全境界があります。

- `--apply` には `--entry-id` が必須
- `pointer` と `manual` は自動変更しない
- 既存の未生成ファイルは上書きしない
- 生成範囲はsource hashとbegin/end markerで検査する
- markerはファイル内で一意でなければならない。marker類似文字列を含むoverlay・複製ブロック・source自身にmarkerを含むケースは、破壊や見逃しを防ぐため `projection_markers_ambiguous` / `source_contains_projection_markers` としてfail-closedで停止する
- sourceとtargetが同一ファイルに解決される場合は `source_target_identical` で拒否する
- エラーメッセージに絶対パスを載せない (secret-safe)
- runtime設定、認証、Cursor/GrokのUI設定は変更しない

## 現在の判断

- Claude Codeはpointer戦略を使えるが、live checkoutがremoteのマージ結果へ同期済みかは別に検査する。
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
