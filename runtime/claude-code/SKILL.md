---
name: repo-preflight
description: >
  repository の公開・共有前チェックと AI 実装フローの intent 対話。
  Claude Code で PR 作成・push・merge・public 化・release・リポジトリ新規作成の直前に使う。
  トリガ: PR作る, pushする, 公開する, リポジトリ作成, preflight, 公開前チェック, 共有前チェック。
  Use before creating a PR, pushing, merging, publishing, releasing, or creating a GitHub repo.
---

# repo-preflight (Claude Code adapter)

## 正本

このファイルは runtime adapter。手順の正本はリポジトリ root の `SKILL.md`。

1. この skill ディレクトリから repo-preflight の clone を特定する  
   （`install_runtime_skills.py` が書いた `REPO_PREFLIGHT_ROOT` を優先）。
2. 正本を読む: `<REPO_PREFLIGHT_ROOT>/SKILL.md`
3. 検査・対話は必ず CLI で実行する（推測で pass にしない）:

```bash
python <REPO_PREFLIGHT_ROOT>/scripts/readiness_scan.py --repo <TARGET_REPO> --intent <intent> --human
```

`intent`: `create_repo` | `push` | `open_pr` | `merge` | `publish` | `release`

## Claude Code 固有の読み替え

| 正本の表現 | Claude Code |
|---|---|
| エージェント | このセッションの Claude Code |
| 外部操作 | Bash の `gh` / `git push` 等 |
| 人間へ質問 | AskUserQuestion または通常の確認ターン |
| 作業停止 | ツール実行を止め、回答を待つ |

## MUST

- `gh pr create` / `git push` / visibility 変更 / release の**前**に `--intent` を走らせる
- stdout の `status` が `needs_human_input` または `blocked` の間は外部操作しない
- secret 検出に ignore を提案しない
- 公開・投稿ボタンは押さない（別承認）

## REPO_PREFLIGHT_ROOT

<!-- repo-preflight:root -->
`REPO_PREFLIGHT_ROOT` は install 時に次行へ実 path が書かれる。未設定ならユーザーに clone path を確認する。

REPO_PREFLIGHT_ROOT=
