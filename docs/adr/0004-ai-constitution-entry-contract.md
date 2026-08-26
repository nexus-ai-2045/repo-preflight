# ADR 0004: AI憲法のruntime入口契約を分離する

- status: Proposed
- date: 2026-08-25
- owner: repo-preflight maintainers

## Context

共通AI憲法を1つの正本に集約しても、各AIが同じMarkdown importを解釈するとは限らない。
Claude Code/Gemini CLIのpointer入口、Grokの`AGENTS.md`/`CLAUDE.md`全文読込、Cursorのproject rules/User Rulesを同じ仕組みとして扱うと、入口が存在するだけで「共通原則が有効」と誤判定する。

## Decision

`repo-preflight`は共通正本とruntime入口を、次の戦略で宣言・検査する。

1. `pointer`: runtime固有のsource pointer/importまたは明示的な正本読込指示を置く入口。`pointer_kind=import|instruction`で構文を宣言する。`instruction`は正本パスのinline code出現だけでなく、同一行または前後2行以内の肯定的な読込指示（正本の主語、必須・先行条件、読む・参照する動詞）を要求し、否定形は拒否する。
2. `materialized`: source本文を生成投影し、source hashと共通本文を検査する入口
3. `manual`: UIや未確認仕様に依存し、機械的に完了扱いしない入口

検査はread-onlyを既定とする。materialized投影の更新は対象entryを1件指定した`--apply`だけで行い、既存の未生成ファイルは上書きしない。

## Rejected alternatives

- 全AI入口へ同じ`@<absolute-path>`を配る: runtimeごとの構文差を隠し、Grok/Cursorでfalse greenになる。
- 各AI入口へ手書きで共通原則を複製する: driftの検知・修復経路がなく、正本が複数化する。
- Grok/Cursorのglobal設定を自動編集する: product settings/auth境界を越え、今回の機械的PRスコープを超える。

## Consequences

- 共通正本は1つのまま、runtime差を明示的に検査できる。
- materialized投影には更新手順とdrift検査が必要になる。
- Cursor User Rulesのように機械証跡を取れない入口は、人間レビュー待ちとして残る。
- このADRのstatusは、実環境への投影適用とCursorの運用経路を人間が確認するまで`Proposed`とする。

## Evidence

- `scripts/ai_entry_contract.py`
- `tests/test_ai_entry_contract.py`
- `docs/ai-constitution-entry-contract.md`
