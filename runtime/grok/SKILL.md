---
name: repo-preflight
description: >
  repository preflight and AI intent dialogue before PR, push, merge, publish, release, GitHub settings, or new repo creation.
  Grok Build / agents で PR作る・pushする・公開する・リポジトリ作成・GitHub設定・preflight・公開前チェックのときに使う。
  Use before creating a PR, pushing, merging, publishing, releasing, changing GitHub settings, or creating a GitHub repo.
---

# repo-preflight (Grok adapter)

## Source of truth

This file is a runtime adapter. Canonical steps live in the clone root `SKILL.md`.

**No hardcoded absolute paths.** Resolve the root via `run_preflight.py` next to this skill.

1. Use `<THIS_SKILL_DIR>/run_preflight.py` (written by install)
2. Resolution order: env `REPO_PREFLIGHT_ROOT` → skill-local `checkout/` → walk from cwd
3. Canonical skill: `<resolved-root>/SKILL.md`
4. Always run the CLI; never invent pass/fail

```bash
# existing target repo
python "<THIS_SKILL_DIR>/run_preflight.py" --repo "<TARGET_REPO>" --intent open_pr --base-ref origin/<BASE> --human

# before creating a repo: omit --repo
python "<THIS_SKILL_DIR>/run_preflight.py" --intent create_repo --human
```

`THIS_SKILL_DIR` = directory containing this SKILL.md (e.g. `~/.grok/skills/repo-preflight`)

`intent`: `create_repo` | `push` | `open_pr` | `merge` | `configure_settings` | `publish` | `release`  
Use `--repo` for every intent except `create_repo`.
Use `--base-ref` only for target-diff checks before `push` / `open_pr` / `merge` in an existing private repo
(JSON `scan_scope.mode` is `target_diff`; there is no `--target-diff` CLI flag).
The base must be an ancestor of HEAD. Do not use target-diff mode for `publish` / `release` / `configure_settings`.
`configure_settings` is GET/compare/preview only. Do not change GitHub settings from this skill.

```bash
python "<THIS_SKILL_DIR>/run_preflight.py" --repo "<TARGET_REPO>" --intent configure_settings --github-settings-profile solo_public --human
```

## MUST

- Run `--intent` before PR / push / merge / publish / release / create_repo / configure_settings
- Do not perform external ops while dialogue status is `needs_human_input` or `blocked`
- Show guarantees / non_guarantees briefly
- Never offer ignore for secrets

## Install

```bash
git clone https://github.com/nexus-ai-2045/repo-preflight.git
cd repo-preflight
python scripts/install_runtime_skills.py --repo . --apply
```

Re-run `--apply` after moving the clone. Do not copy another user's skill folder.
