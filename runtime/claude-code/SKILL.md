---
name: repo-preflight
description: >
  repository の公開・共有前チェックと AI 実装フローの intent 対話。
  Claude Code で PR 作成・push・merge・public 化・release・リポジトリ新規作成の直前に使う。
  トリガ: PR作る, pushする, 公開する, リポジトリ作成, GitHub設定, preflight, 公開前チェック, 共有前チェック。
  Use before creating a PR, pushing, merging, publishing, releasing, changing GitHub settings, or creating a GitHub repo.
---

# repo-preflight (Claude Code adapter)


## 新規リポジトリの作成

新規作成はこの入口で受け付ける。実行処理は既存の `new-repo-bootstrap` へ委譲し、開発規約の検査を同じ `repo-preflight` 正本から導入する。

```bash
python <THIS_SKILL_DIR>/run_preflight.py --create-repo --name <name> --visibility private --description "目的の説明" --json
```

既定は読取りのみ。対象と操作への有効な承認があれば、同じ引数に `--confirm` を付けて実行する。正本の作成手順と各段階の結果を確認する。

## アプリ開発開始時の検査

コメント禁止とバグ再発防止の検査は、解決した `repo-preflight` の正本から導入する。新規リポジトリ作成のスキルもこの同じ入口を呼ぶ。

```bash
python <THIS_SKILL_DIR>/run_preflight.py --install-development-lint --repo <app-repo> --json
```

導入後はアプリの `tools/lint_rules/README.md` を確認し、`python3 tools/development_lint.py` を実行する。既存ファイルの競合は差分を統合してから再実行する。


## 正本

このファイルは runtime adapter。手順の正本は clone 側 root の `SKILL.md`。

**絶対 path 固定はしない。** root 解決は隣の `run_preflight.py` に任せる。

1. この skill ディレクトリの `run_preflight.py` を使う（install が置く）
2. 解決順: 環境変数 `REPO_PREFLIGHT_ROOT` → skill 隣 `checkout/` → cwd 探索
3. 正本: `<resolved-root>/SKILL.md`
4. 検査は推測せず CLI で実行する

```bash
# 既存 repo
python "<THIS_SKILL_DIR>/run_preflight.py" --repo "<TARGET_REPO>" --intent open_pr --base-ref origin/<BASE> --private-submission --human

# 新規 repo 作成前は --repo を付けない
python "<THIS_SKILL_DIR>/run_preflight.py" --intent create_repo --human
```

`THIS_SKILL_DIR` = この SKILL.md があるディレクトリ（例: `~/.claude/skills/repo-preflight`）

`intent`: `create_repo` | `push` | `open_pr` | `merge` | `configure_settings` | `publish` | `release`  
`create_repo` 以外は `--repo <TARGET_REPO>` が必須。
既存private GitHub repoへの通常送信では `--private-submission` と `--base-ref` を併用する。
live確認とcommit済み内容の範囲はclone rootの正本に従い、未送信の作業file本文を対象にしない。
`--private-submission` なしではprivate identityの全在庫検査を維持する。
`--base-ref` は既存 private repo の `push` / `open_pr` / `merge` で今回差分だけを検査する時に使う。
base は HEAD の祖先であること。`publish` / `release` / `configure_settings` のrepo全体検査には使わない。
`configure_settings` は GET / 比較 / preview まで。この skill から Settings は変更しない。

```bash
python "<THIS_SKILL_DIR>/run_preflight.py" --repo "<TARGET_REPO>" --intent configure_settings --github-settings-profile solo_public --human
```

## Claude Code 固有の読み替え

| 正本の表現 | Claude Code |
|---|---|
| エージェント | このセッションの Claude Code |
| 外部操作 | Bash の `gh` / `git push` 等 |
| 人間へ質問 | AskUserQuestion または通常の確認ターン |
| 作業停止 | ツール実行を止め、回答を待つ |

## MUST

- `gh pr create` / `git push` / visibility 変更 / release / GitHub Settings 変更の**前**に `--intent` を走らせる
- stdout の `status` が `needs_human_input` または `blocked` の間は外部操作しない
- secret 検出に ignore を提案しない
- 公開・投稿ボタンは押さない（別承認）

## 導入

```bash
git clone https://github.com/nexus-ai-2045/repo-preflight.git
cd repo-preflight
python scripts/install_runtime_skills.py --repo . --apply
```

clone を移したら `--apply` をやり直す。他人の skill フォルダをコピーしない。
