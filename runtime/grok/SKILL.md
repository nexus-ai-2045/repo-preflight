---
name: repo-preflight
description: >
  repository preflight and AI intent dialogue before PR, push, merge, publish, release, or new repo creation.
  Grok Build / agents で PR作る・pushする・公開する・リポジトリ作成・preflight・公開前チェックのときに使う。
  Use before creating a PR, pushing, merging, publishing, releasing, or creating a GitHub repo.
---

# repo-preflight (Grok adapter)

## 正本

このファイルは runtime adapter。手順の正本はリポジトリ root の `SKILL.md`。

1. `REPO_PREFLIGHT_ROOT`（install が記入）を使う。無ければユーザーに path を確認する。
2. 正本: `<REPO_PREFLIGHT_ROOT>/SKILL.md`
3. CLI:

```bash
# existing target repo
python "<REPO_PREFLIGHT_ROOT>/scripts/readiness_scan.py" --repo "<TARGET_REPO>" --intent open_pr --human

# before creating a repo: omit --repo
python "<REPO_PREFLIGHT_ROOT>/scripts/readiness_scan.py" --intent create_repo --human
```

`intent`: `create_repo` | `push` | `open_pr` | `merge` | `publish` | `release`  
Use `--repo` for every intent except `create_repo`.

## Grok 固有

| 正本の表現 | Grok |
|---|---|
| エージェント | この Grok セッション |
| 外部操作 | shell / gh / git |
| 人間へ質問 | 通常の確認応答（操作前に停止） |

## MUST

- PR / push / merge / publish / release / create_repo の直前に `--intent` を実行
- dialogue `status` が `needs_human_input` / `blocked` なら外部操作しない
- guarantees / non_guarantees を短く示す
- secret に ignore を出さない

## REPO_PREFLIGHT_ROOT

<!-- repo-preflight:root -->
REPO_PREFLIGHT_ROOT=
