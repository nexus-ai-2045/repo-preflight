---
name: repo-preflight
description: "repositoryを構想、登録、設計、リサーチ、TDD、実装、security review、test、運用準備、private GitHub、PR、人間目視review、merge、public化、release、告知、branch/worktree cleanupまで検査記録に基づいて進める。AIがリポジトリ作成・push・PR・merge・公開・releaseに進む直前に自動発火し、不足設定と推奨案を人間へ質問してから続行する。public化に限らずチーム共有、客先納品、外部協力者への受け渡しでも同じ手順を通す。『リポジトリを作る』『PRを作る』『公開する』『pushする』『公開前チェック』『共有前チェック』『納品前チェック』の依頼で使う。"
---

# Repo Preflight

repositoryを見せる相手を広げる前の手順を管理する。専門作業は既存skillへ委譲し、各状態を現在の検査結果で判定する。

**v0.3 の中心は「AI実装フローでの自動発火対話」** です。人がTTYでメニューを回すことが主目的ではありません。エージェントが外部操作に進む直前に preflight を走らせ、不足や未設定を「こう設定しますか？」「未設定ですが設定しますか？」と人間に聞く。

public化は到達点のひとつであり、唯一の終点ではない。private保存、PRまで、mergeまで、チーム共有、客先納品も正規の完了地点として扱う。

## 原則

- `ready` を単独で使わず、`local_checks_passed`、`operations_checked`、`private_remote_synced`、`ready_for_public_review`を分ける。
- 既定をread-onlyにする。診断と実行を混ぜない。
- **操作 intent の直前に必ず対話ゲートを発火**する。黙って repo 作成 / push / PR / merge / 公開しない。
- 保存済み判定を信用せず、現在のHEAD、remote、GitHub、CI、reviewを再測定する。
- 永久保証を主張しない。`status: pass` は公開承認ではない。

## 自動発火トリガー (MUST)

次の操作に進む**直前**に、エージェントは repo-preflight を発火する。ユーザーが「preflightして」と言わなくても発火する。

| これからやろうとしていること | `--intent` | 必須 |
|---|---|---|
| GitHub リポジトリを新規作成する | `create_repo` | MUST |
| remote へ push する | `push` | MUST |
| Pull Request を作る | `open_pr` | MUST |
| PR を merge する | `merge` | MUST |
| public化 / チーム共有 / 納品 / 外部協力者へ渡す | `publish` | MUST |
| release / tag / 告知準備 | `release` | MUST |

```bash
# 例: PR を作る直前
python scripts/readiness_scan.py --repo <path> --intent open_pr --human

# 例: リポジトリ新規作成の直前 (path 未作成でも可)
python scripts/readiness_scan.py --intent create_repo --human

# 例: 公開・共有の直前
python scripts/readiness_scan.py --repo <path> --intent publish --audience public --human
```

stdout は `schema: repo-preflight.dialogue/v3` の JSON。`--human` 時は stderr に番号付きの質問文も出る。

## 対話パケットの使い方 (エージェント手順)

1. **保証すること / 保証しないこと**を短くユーザーへ示す (`guarantees` / `non_guarantees`)。
2. `proposals` を番号付きで提示する。例:
   - 「SECURITY.md がありません。テンプレートから作成しますか？」
   - 「新しい repo は private で作りますか？」
   - 「作者名義の固定照合が未設定です。設定しますか？」
3. `confirmations` で intent 自体の最終確認を取る。default は cancel 寄り。
4. `status` が `needs_human_input` または `blocked` の間は **intent の外部操作を実行しない**。
5. ユーザーが yes した項目だけ直す / 設定する。no や stop なら中止または代替案を出す。
6. **「次から出さない」** (`dismiss_30d` / `dismiss_90d` / `dismiss_forever`) を選ばれたら、採用先 repo に記録する:
   ```bash
   python scripts/readiness_scan.py --repo <path> \
     --record-dismissal <proposal_id> --dismissal-mode 30d|90d|forever \
     --dismissal-reason "..."
   ```
   secret / 必須文書欠落 / 危険操作の最終確認は dismiss できない。`suppressed_proposals` に抑止中一覧が出る。
7. **GitHub 設定ガイドが stale** なら `references/github-settings.md` を公式 docs/changelog と突き合わせ、
   marker の `last_reviewed` を更新する。自動で GitHub 全変更を追従したことにはしない。
8. secret / personal path には「無視して進む」を出さない (`agent_instructions` を守る)。
9. approve 後も、push / PR / merge / visibility 変更 / 投稿は **別ゲート** として操作内容を再掲して確認する。
10. 完了後に再 scan し、結果を報告する。

### status の意味

| status | 意味 | エージェントの動き |
|---|---|---|
| `needs_human_input` | 未設定・不足があり質問が残っている | 質問して停止 |
| `blocked` | secret 等で fail-closed | 修復方針を聞き、無視して進まない |
| `ready_after_confirmation` | 機械不足は埋まっている | 最終確認 (confirmations) のあと intent 準備へ |

`publication_decision` は常に `blocked_human_review_required`。pass 相当でも公開承認にはならない。

## 状態遷移

```text
discovered -> requirements_defined -> research_complete -> design_complete
-> implementation_in_progress -> local_checks_passed -> operations_checked
-> repository_recorded -> private_remote_synced -> pull_request_ready
-> human_review_complete -> merge_approved -> merged
-> audience_expansion_approved -> audience_expanded -> expansion_checks_passed
-> cleanup_complete
```

各外部操作の境界で上記 intent 対話を挟む。検査記録の不足、`unknown`、失敗があれば先へ進めず、停止理由と再実行条件を返す。詳細は[状態一覧](references/lifecycle.md)。

## 実行 (実装中の通常ループ)

1. repo root、対象成果、owner、non-goals、成功条件を確定する。曖昧さが結果を変えない限り質問せず進める。
2. ローカル実装中は read-only scan で現状把握してよい:
   `python scripts/readiness_scan.py --repo <path>`
3. **外部操作の直前**は必ず `--intent ...` の対話パケットを出し、人間の回答を得る。
4. 必要な作業だけを選ぶ (README設計不足時は `recommended_capabilities` に出たものだけ)。
5. [成果物契約](references/artifacts.md) と [承認手順](references/gates.md) に従う。
6. 依頼された完了地点に到達したら閉じる。public化を促し続けない。

## 独立承認が必要な操作

以下を一つの包括承認に束ねない。intent 対話の yes も、これらの実行承認そのものにはしない。

- 公開名義への既存履歴書き換え
- GitHub repository作成、remote追加、push
- PR作成・comment・review依頼
- merge
- privateからpublicへのvisibility変更
- release、告知、投稿、共有
- remote/local branch、worktree削除

public化直前は `owner/name`、正確な操作、使用account、README/LICENSE/SECURITY/secret scan/personal path scan/PREFLIGHT、Webから見えるfilesと全commit history、review済み/未reviewを提示して対象repo固有のyesを待つ。

## コンソール補助 (任意)

人が手元で検査オプションだけ選びたいとき: `python scripts/readiness_scan.py --interactive`  
これは本体ではない。AI自動発火の代替にはしない。

## 完了報告

```text
lifecycle_state:
intent_gate:
  intent:
  dialogue_status:
  user_answers:
local_implementation:
local_verification:
project_registration:
github_status:
human_review:
operational_coverage:
cleanup:
remaining_blockers:
```

## Progressive Reads

- Claude Code / Grok 保証境界: [docs/runtime-support.md](docs/runtime-support.md)
- 状態判定: [references/lifecycle.md](references/lifecycle.md)
- 承認と安全停止条件: [references/gates.md](references/gates.md)
- GitHub設定の推奨値と選択理由: [references/github-settings.md](references/github-settings.md)
- repositoryへ残す成果物: [references/artifacts.md](references/artifacts.md)
- repository catalogへの登録: [references/repository-catalog.md](references/repository-catalog.md)
