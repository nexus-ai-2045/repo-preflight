# repo-preflight AI憲法入口契約

## 目的

共通原則の正本と、各AIが実際に読む入口を混同しないための契約です。

正本は1つだけ持ち、runtimeごとの差は入口戦略として宣言します。

| 戦略 | 意味 | 適用例 |
|---|---|---|
| `pointer` | runtime固有のsource pointer/importまたは明示的な読込指示を入口に置く | Codex、Claude Code、Gemini CLI |
| `materialized` | source本文を生成投影し、hashと本文を検査する | Grok |
| `manual` | 製品UIや未確認仕様に依存し、機械的に完了扱いしない | Cursor User Rules |

`@` は製品ごとに意味が異なります。manifestの`pointer_kind`で、`import`（`@` import）と
`instruction`（Codexのように入口が正本を先に読む明示指示）を区別します。Grok/Cursorの
ファイル添付・rule参照を同じ構文として扱いません。

## 検査

```powershell
py -3.13 scripts/ai_entry_contract.py `
  --manifest assets/ai-entry-contract.example.json `
  --json
```

既定は読み取り専用です。結果が `blocked` のときは、未確認のruntimeを「対応済み」と扱いません。

## 投影の更新

`materialized` targetの作成・更新は、対象を1件指定した明示操作だけ許可します。

```powershell
py -3.13 scripts/ai_entry_contract.py `
  --manifest <manifest.json> `
  --entry-id grok-global `
  --apply `
  --json
```

次の安全境界があります。

- `--apply` には `--entry-id` が必須
- `pointer` と `manual` は自動変更しない
- 既存の未生成ファイルは上書きしない
- 生成範囲はsource hashとbegin/end markerで検査する
- runtime設定、認証、Cursor/GrokのUI設定は変更しない

## 現在の判断

- Claude Codeはpointer戦略を使えるが、live checkoutがremoteのマージ結果へ同期済みかは別に検査する。
- Codexは階層入口の明示的な正本読込指示を`pointer_kind=instruction`で検査する。本文を複製する`materialized`とは区別する。
- Grokは認識済みの`AGENTS.md`/`CLAUDE.md`を読むが、Claude Codeと同じMarkdown importを前提にしない。Grokはmaterialized戦略で明示的に検証する。
- Cursorのglobal User Rulesは製品UI経路を含むため、ファイル存在だけで完了扱いせず、manual evidenceで止める。project `.cursor/rules` を採用する場合は、project scopeの別manifestを作る。

## 保証境界

この契約が保証するのは、sourceと宣言された入口の存在・pointer・生成本文・hashの整合です。AI製品が毎回同じ推論をすること、UI設定が有効であること、mergeや公開を許可することは保証しません。
