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
| private_remote_synced | private remoteとHEAD一致、PREFLIGHT | secret/PII/license問題 |
| pull_request_ready | PR、現在HEADのCI、review記録 | 未解決の重大指摘 |
| human_review_complete | reviewer、日時、対象HEAD/PR diff、確認範囲、判定、未解決事項 | review範囲不明 |
| merge_approved | exact PR/HEAD/方式の承認 | stale review/CI |
| merged | default branch上のmerge証拠 | merge未確認 |
| audience_expansion_approved | 広げる相手（audience）の明示、repository固有の承認、current HEADのREADME release gate、人間目視review | audienceが不明、README設計fail、包括的・過去承認のみ |
| audience_expanded | 実際に見えるようになった範囲の再実測 | 可視範囲が未確認 |
| expansion_checks_passed | 拡大後に見える内容、CI、securityの再確認 | 拡大後検査未実施 |
| cleanup_complete | branch/worktree参照なし | unmerged/unpushed work |

末尾3状態は「見せる相手を広げる」操作の一般形。`audience`の値ごとに必要な証拠が変わる。

| audience | audience_expanded の実測対象 | 備考 |
|---|---|---|
| Web全体（public化） | GitHub visibility、Webから見えるfilesと全commit history | 旧 `public_release_approved` / `public` / `public_checks_passed` はこの行に相当 |
| organization / team | collaborator・team権限、private repositoryのaccess一覧 | visibilityは変えずに到達する |
| 客先（納品） | 納品物の内容と受け渡し経路、含めた履歴の範囲 | repository全体ではなく成果物単位のことがある |
| 外部協力者 | 招待したaccount、付与した権限、期限 | 期限付きなら失効条件も記録する |

`TEST_FAILED`、`SECURITY_HOLD`、`IDENTITY_HOLD`、`LICENSE_HOLD`、`REVIEW_CHANGES_REQUIRED`、`EXTERNAL_DEPENDENCY_HOLD`を明示し、再実行条件を添える。

依頼された完了地点へ到達したら完了として閉じる。private保存、PRまで、mergeまで、およびpublic以外のaudienceへの`expansion_checks_passed`を正規の完了地点として認める。public化だけを終点として扱わない。
