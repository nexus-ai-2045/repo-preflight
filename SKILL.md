---
name: public-readiness
description: "repositoryを構想、登録、設計、リサーチ、TDD、実装、security review、test、運用準備、private GitHub、PR、人間目視review、merge、public化、告知、branch/worktree cleanupまで検査記録に基づいて進める。『publicを目指す』『公開前チェック』『公開repoとして完成させる』『PRからmerge後の後片付けまで』の依頼で使う。登録だけの依頼では登録手順だけを実行する。"
---

# Public Readiness

repositoryの公開準備手順を管理する。専門作業は既存skillへ委譲し、各状態を現在の検査結果で判定する。organization固有の台帳や名義ポリシーは公開コアの外で扱う。

## 原則

- `ready` を単独で使わず、`local_checks_passed`、`operations_checked`、`private_remote_synced`、`ready_for_public_review`を分ける。
- 既定をread-onlyにする。`scripts/readiness_scan.py --repo <path> --json`から始める。
- 保存済み判定を信用せず、現在のHEAD、remote、GitHub、CI、reviewを再測定する。
- 診断、ローカル修正、外部操作を分離する。
- 永久保証を主張しない。運用保証は検証日時、対象環境、監視、復旧、再検証条件が揃った状態とする。

## 状態遷移

```text
discovered -> requirements_defined -> research_complete -> design_complete
-> implementation_in_progress -> local_checks_passed -> operations_checked
-> repository_recorded -> private_remote_synced -> pull_request_ready
-> human_review_complete -> merge_approved -> merged
-> public_release_approved -> public -> public_checks_passed -> cleanup_complete
```

検査記録の不足、`unknown`、失敗があれば先へ進めず、停止理由と再実行条件を返す。詳細は[状態一覧](references/lifecycle.md)を読む。

## 実行

1. repo root、対象成果、owner、non-goals、成功条件を確定する。曖昧さが結果を変えない限り質問せず進める。
2. `readiness_scan.py`でrepo、文書、identity、history、secret候補、個人path、CIをread-only検査する。
3. 必要な作業だけを選ぶ。
   - 壁打ち・UX: Product Design / brainstorming
   - 公式仕様: OpenAI Developers / official docs
   - 視覚制作: Creative Production
   - TDD: test-driven-development / python-testing
   - security: Codex Security threat model / security scan
   - GitHub: github-cli-ops-guard / GitHub specialist / pr review
   - 完了: verification-before-completion / finishing-a-development-branch
4. [成果物契約](references/artifacts.md)に従い、必要なrepo文書と機械可読evidenceを作る。
5. [承認手順](references/gates.md)に従い、外部操作ごとに内容提示、承認、実行、結果確認を分離する。
6. 依頼された完了地点（登録のみ、private保存、PRまで、mergeまで、publicまで）に到達したら完了として扱う。public化を促し続けない。
7. merge後にdefault branchを再測定し、参照がないことを確認してからbranch/worktreeを片付ける。

## 独立承認が必要な操作

以下を一つの包括承認に束ねない。

- 公開名義への既存履歴書き換え
- GitHub repository作成、remote追加、push
- PR作成・comment・review依頼
- merge
- privateからpublicへのvisibility変更
- release、告知、投稿、共有
- remote/local branch、worktree削除
- 実home runtimeへのskill配布

public化直前は `owner/name`、正確な操作、使用account、README/LICENSE/SECURITY/secret scan/personal path scan/PUBLIC_READY、Webから見えるfilesと全commit history、review済み/未reviewを提示して対象repo固有のyesを待つ。

## 完了報告

次を分けて報告する。

```text
lifecycle_state:
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

- 状態判定: [references/lifecycle.md](references/lifecycle.md)
- 承認と安全停止条件: [references/gates.md](references/gates.md)
- repositoryへ残す成果物: [references/artifacts.md](references/artifacts.md)
- repository catalogへの登録: [references/repository-catalog.md](references/repository-catalog.md)
