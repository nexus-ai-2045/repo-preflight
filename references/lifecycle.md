# ライフサイクル

| 状態 | 必須証拠 | 次へ進めない条件 |
|---|---|---|
| discovered | repository root、Git top-level | 対象が曖昧 |
| requirements_defined | 目的、利用者、成功条件、対象外 | 重要判断が未確定 |
| research_complete | 一次資料、類似repository、license | 変更されやすい事実が未確認 |
| design_complete | architecture、threat model、E2E条件 | trust boundary不明 |
| implementation_in_progress | failing test、変更範囲 | 既存failure未分離 |
| local_checks_passed | test/build/lint/smoke/E2E | 必須check失敗 |
| operations_checked | 実環境smoke、alert delivery、restore/rollback test、owner、RTO/RPO、検査期限 | 文書のみ、外部運用未確認 |
| repository_recorded | repository catalog、公開名義、canonical path | 登録先不明 |
| private_remote_synced | private remoteとHEAD一致、PUBLIC_READY | secret/PII/license問題 |
| pull_request_ready | PR、現在HEADのCI、review記録 | 未解決の重大指摘 |
| human_review_complete | reviewer、日時、対象HEAD/PR diff、確認範囲、判定、未解決事項 | review範囲不明 |
| merge_approved | exact PR/HEAD/方式の承認 | stale review/CI |
| merged | default branch上のmerge証拠 | merge未確認 |
| public_release_approved | repository固有のvisibility承認、current HEADのREADME release gate、人間目視review | README設計fail、包括的・過去承認のみ |
| public | visibility再実測 | 設定不明 |
| public_checks_passed | 公開内容、CI、security再確認 | 公開後検査未実施 |
| cleanup_complete | branch/worktree参照なし | unmerged/unpushed work |

`TEST_FAILED`、`SECURITY_HOLD`、`IDENTITY_HOLD`、`LICENSE_HOLD`、`REVIEW_CHANGES_REQUIRED`、`EXTERNAL_DEPENDENCY_HOLD`を明示し、再実行条件を添える。

依頼された完了地点へ到達したら完了として閉じる。private保存、PRまで、mergeまでを正規の完了地点として認める。
